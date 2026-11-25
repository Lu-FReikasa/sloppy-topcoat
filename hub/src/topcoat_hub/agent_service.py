"""The agent's runner (E3-01a): starts, cancels and resumes a user's tasks in the background and
connects the loop to the real model (through the gateway) and the real tools (``ToolService``).

Tasks are per user (D20): every entry point takes the user, every read goes through that user's
``AgentStore``. A task survives a hub restart: :meth:`AgentService.recover` starts the ones that
were queued or running, and the loop continues from the last finished step.
"""

import asyncio
import contextlib
import logging
import uuid
from dataclasses import replace
from datetime import datetime
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from topcoat_hub import app_scope, chat, tool_groups
from topcoat_hub.agent_events import task_event
from topcoat_hub.agent_loop import (
    AgentLoop,
    Completion,
    Limits,
    ModelFailure,
    ModelPort,
    NativeUnsupported,
    Publish,
)
from topcoat_hub.agent_prompts import TOOL_MODES
from topcoat_hub.agent_store import AgentStore, unfinished
from topcoat_hub.agent_workshop import CHANGES as WORKSHOP_EDITS
from topcoat_hub.auth import CurrentUser
from topcoat_hub.chat_store import get_hub_setting
from topcoat_hub.config import Settings
from topcoat_hub.gateway import Gateway, GatewayError, background_work, split_model
from topcoat_hub.github_access import GITHUB_NODE_ID, GitHubService
from topcoat_hub.identity import IDENTITY_NODE_ID, IdentityService
from topcoat_hub.inventory import INVENTORY_NODE_ID, InventoryService
from topcoat_hub.job_log import job_context
from topcoat_hub.knowledge import KNOWLEDGE_NODE_ID, READ_CAPABILITY, KnowledgeService
from topcoat_hub.message_tools import MESSAGES_NODE_ID, MessageTools
from topcoat_hub.models import AgentTask
from topcoat_hub.project_store import APPLICATION
from topcoat_hub.project_tools import PROJECT_NODE_ID, ProjectTools
from topcoat_hub.tools import Caller, ToolError, ToolService
from topcoat_hub.topcoat_self import TOPCOAT_NODE_ID, TopcoatService
from topcoat_hub.wiki_tools import WIKI_NODE_ID, WikiTools

log = logging.getLogger(__name__)

# the origin of a run of a project task (project_runs.py): idle work in the node queue
ORIGIN_BATCH = "batch"
# the origin of an agent step of a block program (blocks/service.py): staged like a task the
# person started, but the program keeps the result; nothing goes to the person's Ablage
ORIGIN_BLOCK = "block"

CONTINUE_TEXT = (
    "The operator asks you to continue from here. Use what you have seen; look at more only if "
    'you need to, and then reply with a "finish" action.'
)


TOOL_MODES_KEY = "agent_tool_modes"


def without_blocking_test(tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """``repo.test`` waits for the test inside one call and a call has a short limit; a node that
    offers ``repo.test_start`` lets the agent start the run, do other things and ask for it. The
    agent is offered the second and not the first (the GUI button keeps using ``repo.test``)."""
    background = {t["node"] for t in tools if t["capability"] == "repo.test_start"}
    return [t for t in tools if not (t["capability"] == "repo.test" and t["node"] in background)]


def without_shot(tools: list[dict[str, Any]], allowed: bool) -> list[dict[str, Any]]:
    """``browser.shot`` returns a picture, which only a model that reads images can use. The hub has
    no notion of what a model can read, so the admin says so with ``TOPCOAT_BROWSER_SHOT_ALLOWED``;
    until then the agent is not offered it and works with the tree, the audit and the animations,
    which are data (brief E6-02-workshop-browser)."""
    return tools if allowed else [t for t in tools if t["capability"] != "browser.shot"]


class AgentError(Exception):
    """A request the agent service refuses; ``status`` is the HTTP status."""

    def __init__(self, status: int, code: str, message: str) -> None:
        super().__init__(message)
        self.status, self.code, self.message = status, code, message


class GatewayModel:
    """The model behind the gateway: one non-streaming chat completion per turn."""

    def __init__(
        self,
        gateway: Gateway,
        sessionmaker: async_sessionmaker[AsyncSession],
        settings: Settings,
    ) -> None:
        self.gateway = gateway
        self.sessionmaker = sessionmaker
        self.settings = settings
        # models that refused ``response_format`` (asked about once, then left out)
        self._no_json_mode: set[str] = set()
        # models that refused tool_choice "required" / cannot do native calls at all
        self._no_required: set[str] = set()
        self._json_models: set[str] = set()
        # One generation at a time per model: several long answers on one model share its
        # window and its speed (a chat on the same model keeps its turn in between).
        self._turns: dict[str, asyncio.Lock] = {}

    async def _model(self, requested: str | None) -> str:
        if requested:
            return requested
        # the row of the application the task works for (Research has its own models and nodes);
        # a reviewer run asks for the check row instead (its own role, so its node may differ)
        context = job_context()
        role = context.role or "agent"
        return (
            await self.gateway.resolver.resolve(role, application=context.application)
        ).requested

    async def _modes(self) -> dict[str, str]:
        async with self.sessionmaker() as db:
            value = await get_hub_setting(db, TOOL_MODES_KEY)
        if not isinstance(value, dict):
            return {}
        return {k: v for k, v in value.items() if isinstance(k, str) and v in TOOL_MODES}

    async def tool_mode(self, requested: str | None) -> str:
        """How this model is talked to: its setting (per model, admin), else the hub default
        (``auto``: native tool calls, the JSON protocol where the model cannot)."""
        name = split_model(await self._model(requested))[0]  # the setting is per model, not node
        mode = (await self._modes()).get(name) or self.settings.agent_tool_mode
        if mode == "auto" and name in self._json_models:
            return "json"
        return mode if mode in TOOL_MODES else "auto"

    async def prefer_json(self, requested: str | None) -> None:
        """Native calls did not work with this model: stay with JSON until the hub restarts."""
        self._json_models.add(split_model(await self._model(requested))[0])

    async def complete(
        self,
        messages: list[dict[str, Any]],
        model: str | None,
        max_tokens: int,
        tools: list[dict[str, Any]] | None = None,
    ) -> Completion:
        name = await self._model(model)
        payload: dict[str, Any] = {
            "stream": False,
            "temperature": 0.1,
            "max_tokens": max_tokens,
            "messages": messages,
            # the server keeps the KV cache of the slot's last prompt and only computes what
            # differs; the prompt of a run only grows at its end (see _Run.clock)
            "cache_prompt": True,
        }
        # the thinking switch of the model family, as in the chat (a thinking model spent minutes
        # on one tool call without it)
        payload.update(
            chat.effort_params(
                name,
                getattr(self.settings, "agent_effort", "fast"),
                getattr(self.settings, "chat_effort", None),
            )
        )
        if tools:
            # native tool calls (llama-server --jinja). "required": the model must call a function
            # — a tool or finish — instead of talking about it (gpt-oss does not call tools
            # unasked). A server that rejects that gets "auto" once, then the JSON protocol.
            payload["tools"] = tools
            payload["parallel_tool_calls"] = True  # see Limits.max_parallel_calls
            if name not in self._no_required:
                payload["tool_choice"] = "required"
        elif name not in self._no_json_mode:
            # llama.cpp: keeps the answer a JSON object. UNVERIFIED on the pinned build; a model
            # that answers 400/422 is asked again without it.
            payload["response_format"] = {"type": "json_object"}
        try:
            async with self._turns.setdefault(name, asyncio.Lock()):
                data = await self._call(name, payload, tools is not None)
            context_length, node = await self._loaded_context(name)
        except GatewayError as exc:
            # 503 (no node right now, context_full) and 504 are worth asking again later
            raise ModelFailure(
                exc.message, retry=exc.status in (503, 504) and not exc.reason, reason=exc.reason
            ) from exc
        text, tokens, calls, finish_reason, prompt_tokens = "", 0, None, "", 0
        choices = data.get("choices")
        if isinstance(choices, list) and choices and isinstance(choices[0], dict):
            if isinstance(choices[0].get("finish_reason"), str):
                finish_reason = choices[0]["finish_reason"]
            message = choices[0].get("message")
            if isinstance(message, dict):
                if isinstance(message.get("content"), str):
                    text = message["content"]
                raw_calls = message.get("tool_calls")
                if isinstance(raw_calls, list):
                    calls = [c for c in raw_calls if isinstance(c, dict)] or None
        usage = data.get("usage")
        if isinstance(usage, dict):
            if isinstance(usage.get("prompt_tokens"), int):
                prompt_tokens = usage["prompt_tokens"]
            tokens = sum(
                v
                for v in (usage.get("prompt_tokens"), usage.get("completion_tokens"))
                if isinstance(v, int)
            )
        return Completion(
            text=text,
            tokens=tokens,
            model=split_model(name)[0],
            tool_calls=calls,
            finish_reason=finish_reason,
            context_length=context_length,
            node=node,
            prompt_tokens=prompt_tokens,
        )

    async def _loaded_context(self, name: str) -> tuple[int | None, str | None]:
        """The context (of one slot) the model is loaded with and where; (None, None) when the hub
        does not know yet (the node reports it in its next heartbeat) or no node has it."""
        try:
            route = await self.gateway.route(name)
        except GatewayError:
            return None, None
        if not route.candidates:
            return None, None
        best = route.candidates[0]
        return best.context_length, best.name

    def _gateway_call(self, name: str, payload: dict[str, Any]) -> Any:
        if self.settings.agent_stream:
            return self.gateway.call_streamed(
                name,
                "/v1/chat/completions",
                payload,
                min_context=self.settings.agent_min_context,
                label="agent",
            )
        return self.gateway.call(
            name,
            "/v1/chat/completions",
            payload,
            min_context=self.settings.agent_min_context,
            label="agent",
        )

    async def _call(self, name: str, payload: dict[str, Any], native: bool) -> dict[str, Any]:
        """One request; a 400/422 about an option the server does not know is asked again without
        that option (JSON mode, then ``tool_choice``), and for native calls without a way
        forward it is :class:`NativeUnsupported`."""
        try:
            return await self._gateway_call(name, payload)
        except GatewayError as exc:
            refused = "answered 400" in exc.message or "answered 422" in exc.message
            if not refused:
                raise
            if "response_format" in payload:
                self._no_json_mode.add(name)
                payload.pop("response_format")
                return await self._gateway_call(name, payload)
            if native and "tool_choice" in payload:
                self._no_required.add(name)
                payload.pop("tool_choice")
                try:
                    return await self._gateway_call(name, payload)
                except GatewayError as again:
                    if "answered 400" in again.message or "answered 422" in again.message:
                        raise NativeUnsupported(again.message) from again
                    raise
            if native:
                raise NativeUnsupported(exc.message) from exc
            raise


class ToolsAdapter:
    """``ToolService`` as the loop sees it."""

    def __init__(
        self,
        tools: ToolService,
        knowledge: KnowledgeService | None = None,
        inventory: InventoryService | None = None,
        identity: IdentityService | None = None,
        topcoat: TopcoatService | None = None,
        github: GitHubService | None = None,
        project: ProjectTools | None = None,
        messages: MessageTools | None = None,
        browser_shot: bool = False,
        wiki: WikiTools | None = None,
    ) -> None:
        self.browser_shot = browser_shot
        self.tools = tools
        self.knowledge = knowledge
        self.inventory = inventory
        self.identity = identity
        self.topcoat = topcoat
        self.github = github
        self.project = project
        self.messages = messages
        self.wiki = wiki  # read-only, released pages, Projekte and Verwaltung only (wiki_tools.py)

    async def available(self, caller: Caller) -> list[dict[str, Any]]:
        """What the caller is offered; a run with an allow-list is offered nothing outside it."""
        offered = await self._available(caller)
        if caller.allowed_capabilities is None:
            return offered
        return [t for t in offered if t["capability"] in caller.allowed_capabilities]

    async def _available(self, caller: Caller) -> list[dict[str, Any]]:
        """The machines' capabilities the caller may run and — only next to them, so a task never
        starts on the notes alone — ``knowledge.search`` when the caller has notes to search."""
        if caller.application == APPLICATION and self.project is not None:
            # a project run has the project tools, the notes (the wiki, read only: the project agent
            # compares it with the repository) and no machine, no web
            tools = await self.project.offer(caller)
            if self.messages is not None:
                tools = [*tools, *await self.messages.offer(caller)]
            if self.knowledge is not None:
                tools = [*tools, *await self.knowledge.offer(caller.user_id)]
            if self.wiki is not None:
                tools = [*tools, *await self.wiki.offer(caller)]
            return tools
        # D99: a file change is never a direct call of the agent; it comes back as a proposal the
        # user confirms one by one (ToolService.writable), so it is not in this list
        usable = without_blocking_test(await self.tools.usable_tools(caller))
        usable = [t for t in usable if t["capability"] not in WORKSHOP_EDITS]
        if usable and self.knowledge is not None:
            usable = [*usable, *await self.knowledge.offer(caller.user_id)]
        if usable and self.inventory is not None:
            usable = [*usable, *await self.inventory.offer(caller)]
        if usable and self.identity is not None:
            usable = [*usable, *await self.identity.offer(caller)]
        if self.topcoat is not None:  # Topcoat explains itself even with no node connected
            usable = [*usable, *await self.topcoat.offer(caller)]
        if self.github is not None:  # read-only GitHub, admin and configured only
            usable = [*usable, *await self.github.offer(caller)]
        if self.project is not None:  # the project agent's own tools, only for its runs
            usable = [*usable, *await self.project.offer(caller)]
        if self.messages is not None:  # the answer of an application to a request (PPH-05)
            usable = [*usable, *await self.messages.offer(caller)]
        if self.wiki is not None:  # the released wiki pages, for Verwaltung (and Projekte above)
            usable = [*usable, *await self.wiki.offer(caller)]
        return without_shot(await self._name_local_repos(caller, usable), self.browser_shot)

    async def _name_local_repos(
        self, caller: Caller, usable: list[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        """D76: the local repositories of a dev node are named in its ``repo.sync``, so the model
        knows what it may edit and test there (an external one is read with github.read)."""
        repos = getattr(self.github, "repos", None) if self.github is not None else None
        if repos is None:
            return usable
        local: dict[str, list[str]] = {}
        for row in await repos.list(caller.user_id):
            if row.kind == "local" and row.node_id is not None:
                local.setdefault(str(row.node_id), []).append(row.name)
        if not local:
            return usable
        out = []
        for entry in usable:
            names = local.get(str(entry.get("node_id")), [])
            if entry.get("capability") == "repo.sync" and names:
                entry = {
                    **entry,
                    "description": f"{entry.get('description', '')} Local repositories here, "
                    f"which you edit and test on work branches and keep with repo.commit: "
                    f"{', '.join(sorted(names))}.",
                }
            out.append(entry)
        return out

    async def proposable(self, caller: Caller) -> list[dict[str, Any]]:
        """The state-changing actions the agent may *propose* for this caller (E3-01b)."""
        if caller.allowed_capabilities is not None:
            return []  # a run with an allow-list proposes nothing
        return await self.tools.proposable_tools(caller)

    async def invoke(
        self,
        caller: Caller,
        node_id: uuid.UUID,
        capability: str,
        tool_input: dict[str, Any],
        task_id: uuid.UUID | None = None,
    ) -> dict[str, Any]:
        if (
            caller.allowed_capabilities is not None
            and capability not in caller.allowed_capabilities
        ):
            raise ToolError(
                403, "not_allowed", f"{capability} is not among the capabilities of this run"
            )
        if node_id == KNOWLEDGE_NODE_ID and self.knowledge is not None:
            if capability == READ_CAPABILITY:
                return await self.knowledge.read(caller.user_id, tool_input)
            return await self.knowledge.run_tool(caller.user_id, tool_input)
        if node_id == INVENTORY_NODE_ID and self.inventory is not None:
            return await self.inventory.run_tool(caller, tool_input)
        if node_id == IDENTITY_NODE_ID and self.identity is not None:
            return await self.identity.run_tool(caller, tool_input)
        if node_id == TOPCOAT_NODE_ID and self.topcoat is not None:
            return await self.topcoat.run_tool(caller, capability, tool_input)
        if node_id == GITHUB_NODE_ID and self.github is not None:
            return await self.github.run_tool(caller, capability, tool_input)
        if node_id == PROJECT_NODE_ID and self.project is not None:
            return await self.project.run_tool(caller, capability, tool_input)
        if node_id == MESSAGES_NODE_ID and self.messages is not None:
            return await self.messages.run_tool(caller, capability, tool_input)
        if node_id == WIKI_NODE_ID and self.wiki is not None:
            return await self.wiki.run_tool(caller, capability, tool_input)
        return await self.tools.invoke(caller, node_id, capability, tool_input, task_id)


# the programs that have a template with a tool ceiling (ui/seeds.py)
CEILING_APPLICATIONS = ("chat", "research", "dev", "admin", "project")


class AgentService:
    def __init__(
        self,
        sessionmaker: async_sessionmaker[AsyncSession],
        model: ModelPort,
        tools: ToolsAdapter,
        publish: Publish,
        settings: Settings,
        limits: Limits | None = None,
    ) -> None:
        self.sessionmaker = sessionmaker
        self.tools = tools
        self.settings = settings
        self.publish = publish
        self.loop = AgentLoop(sessionmaker, model, tools, publish, limits or Limits.of(settings))
        self.loop.auto_groups = settings.agent_auto_tool_groups
        self.loop.settings = settings
        self._running: dict[uuid.UUID, asyncio.Task[None]] = {}
        self._canceled: set[uuid.UUID] = set()
        self._limits: dict[uuid.UUID, Limits] = {}  # per-task limits of this process's lifetime
        self._hooks: dict[uuid.UUID, dict[str, Any]] = {}  # the hooks a block program set
        self.coordinator: Any = None  # set by main (E3-08)

    # --- entry points, all per user --------------------------------------------------------

    async def start(
        self,
        user: CurrentUser,
        prompt: str,
        model: str | None,
        origin: str = "tasks",
        limits: Limits | None = None,
        scope: dict[str, Any] | None = None,
        kind: str = "single",
        parent_id: uuid.UUID | None = None,
        conversation_id: uuid.UUID | None = None,
        deadline_at: datetime | None = None,
        application: str | None = None,
        capabilities: list[str] | None = None,
        hooks: dict[str, Any] | None = None,
    ) -> AgentTask:
        """Queue a task. ``origin="chat"``: a check that a chat answer is based on (kept out of the
        Tasks list); ``limits``: this task's own limits (a chat check is short); ``scope``: the
        nodes and tool groups it may use on top of the user's grants (E3-04)."""
        if not self.settings.agent_enabled:
            raise AgentError(503, "agent_disabled", "the agent is switched off on this hub")
        if kind == "coordinator" and not self.settings.coordinator_enabled:
            raise AgentError(503, "coordinator_disabled", "the coordinator is switched off")
        scope = await self._within_application(user, application, scope)
        caller = replace(
            Caller.of(user, "agent"),
            application=application,
            allowed_capabilities=frozenset(capabilities) if capabilities is not None else None,
        )
        if not tool_groups.apply_scope(await self.tools.available(caller), scope):
            raise AgentError(
                409,
                "no_capabilities",
                "there is no capability you may run on a connected tool node "
                "(is a tool node connected, and did an admin grant you its capabilities?)",
            )
        async with self.sessionmaker() as db:
            store = AgentStore(db, user.uuid)
            # the checks of a coordinator are bounded by the coordinator's own parallelism
            if parent_id is None and (
                await store.count_active() >= self.settings.agent_max_active_per_user
            ):
                raise AgentError(
                    429, "too_many_tasks", "too many tasks are running; wait or cancel one"
                )
            task = await store.create_task(
                prompt,
                model,
                "api" if user.via_key else "gui",
                origin,
                tool_groups.clean_scope(scope),
                kind,
                parent_id,
                conversation_id,
                deadline_at,
                application=application,
                capabilities=capabilities,
            )
            await db.refresh(task)  # server-side columns (updated_at) before the session is gone
        if hooks:
            self._hooks[task.id] = hooks
        if limits is not None:
            self._limits[task.id] = limits
        self._announce(task)
        self._schedule(task.id, user.uuid)
        return task

    async def _within_application(
        self, user: CurrentUser, application: str | None, scope: dict[str, Any] | None
    ) -> dict[str, Any] | None:
        """The scope of a task that works for a program, narrowed to that program's tool ceiling
        (app_scope.py). Done here, where every task starts, so that no way of starting one
        (a request from a project, the planner, a revision, the API) can forget it: a Research
        task that got every tool of every machine filled a 16k context with tool definitions.
        The ceiling only removes; a scope that is narrower stays as it is."""
        if application not in CEILING_APPLICATIONS:
            return scope
        async with self.sessionmaker() as db:
            limit = await app_scope.ceiling(db, user, application)
        settled = tool_groups.clean_scope(scope) or {
            "nodes": None,
            "groups": None,
            "internet": None,
        }
        return app_scope.cap(settled, limit)

    async def cancel(self, user_id: uuid.UUID, task_id: uuid.UUID) -> AgentTask | None:
        async with self.sessionmaker() as db:
            store = AgentStore(db, user_id)
            task = await store.get(task_id)
            if task is None:
                return None
            if task.status not in ("queued", "running"):
                return task
            self._canceled.add(task_id)
            await store.set_status(task, "canceled", stop_reason="canceled")
            await store.add_step(task, "note", {"text": "canceled by the user"})
            await db.refresh(task)
            self._announce(task)
        running = self._running.get(task_id)
        if running is not None:
            running.cancel()
            await asyncio.gather(running, return_exceptions=True)
        self._canceled.discard(task_id)
        await self.loop.tell(user_id, task_id, "canceled")
        return task

    async def resume(self, user: CurrentUser, task_id: uuid.UUID) -> AgentTask | None:
        async with self.sessionmaker() as db:
            store = AgentStore(db, user.uuid)
            task = await store.get(task_id)
            if task is None:
                return None
            resumable = task.status in ("canceled", "failed") or (
                task.status == "done" and task.stop_reason
            )
            if not resumable or task_id in self._running:
                raise AgentError(
                    409, "not_resumable", "only a stopped, failed or cut-off task can continue"
                )
            if await store.count_active() >= self.settings.agent_max_active_per_user:
                raise AgentError(
                    429, "too_many_tasks", "too many tasks are running; wait or cancel one"
                )
            await store.add_step(
                task,
                "observation",
                {"kind": "note", "text": CONTINUE_TEXT, "evidence": None, "tool": None},
            )
            await store.set_status(
                task, "queued", stop_reason=None, error=None, summary=None, diagnosis=None
            )
            task.finished_at = None
            await db.commit()
            await db.refresh(task)
            self._announce(task)
        self._schedule(task_id, user.uuid)
        return task

    async def recover(self) -> int:
        """Start the tasks a restart interrupted; returns how many."""
        async with self.sessionmaker() as db:
            pending = await unfinished(db)
        for task_id, user_id in pending:
            self._schedule(task_id, user_id)
        return len(pending)

    async def shutdown(self) -> None:
        """Stop the runs without touching the tasks: they are picked up again after a restart."""
        running = list(self._running.values())
        for t in running:
            t.cancel()
        await asyncio.gather(*running, return_exceptions=True)

    # --- internals -------------------------------------------------------------------------

    def _announce(self, task: AgentTask) -> None:
        self.publish("agent_task", task_event(task))

    async def _is_coordinator(self, task_id: uuid.UUID, user_id: uuid.UUID) -> bool:
        async with self.sessionmaker() as db:
            task = await AgentStore(db, user_id).get(task_id)
            return task is not None and task.kind == "coordinator"

    async def _is_origin(self, task_id: uuid.UUID, user_id: uuid.UUID, origin: str) -> bool:
        async with self.sessionmaker() as db:
            task = await AgentStore(db, user_id).get(task_id)
            return task is not None and task.origin == origin

    def _schedule(self, task_id: uuid.UUID, user_id: uuid.UUID) -> None:
        if task_id in self._running:
            return
        self._running[task_id] = asyncio.create_task(
            self._run(task_id, user_id), name=f"agent-{task_id}"
        )

    async def _run(self, task_id: uuid.UUID, user_id: uuid.UUID) -> None:
        try:
            if await self._is_coordinator(task_id, user_id) and self.coordinator is not None:
                await self.coordinator.run(task_id, user_id)
            else:
                run = self.loop.run
                if await self._is_origin(task_id, user_id, ORIGIN_BATCH):
                    # a batch run is idle work on its node: a chat request never waits for it
                    # longer than the model call that is running (project_runs.py)
                    run = background_work(run)
                await run(task_id, user_id, self._limits.get(task_id), self._hooks.get(task_id))
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # the loop's own bug must not leave a task "running" forever
            log.exception("agent task %s crashed", task_id)
            with contextlib.suppress(Exception):
                async with self.sessionmaker() as db:
                    store = AgentStore(db, user_id)
                    task = await store.get(task_id)
                    if task is not None and task.status in ("queued", "running"):
                        await store.set_status(
                            task,
                            "failed",
                            stop_reason="internal",
                            error=f"{type(exc).__name__}: {exc}",
                        )
                        self._announce(task)
        finally:
            self._running.pop(task_id, None)
            self._limits.pop(task_id, None)
            self._hooks.pop(task_id, None)
