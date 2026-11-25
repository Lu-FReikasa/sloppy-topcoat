"""The hub side of block programs: the ports on the gateway and the agent, and the runs.

A run has the rights of the person who started it (D20): the agent block is a task of that person
with the tools the application's ceiling and the person's grants allow (D103), and nothing a block
does adds to that. Runs are kept in memory while they go and for a while afterwards."""

import asyncio
import json
import logging
import uuid
from dataclasses import dataclass, field, replace
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from topcoat_hub import (
    model_facts,
    model_profiles,
    node_chain,
    report_splice,
    research_profile,
    tool_groups,
)
from topcoat_hub.agent_service import ORIGIN_BLOCK, AgentService
from topcoat_hub.auth import CurrentUser
from topcoat_hub.bench.service import latest_ratings
from topcoat_hub.blocks import builtin
from topcoat_hub.blocks import pick as picks
from topcoat_hub.blocks.engine import (
    AgentOutcome,
    BlockEngine,
    BlockFailure,
    EmptyAnswer,
    Progress,
    RunResult,
    ToolOutcome,
)
from topcoat_hub.chat import hub_language
from topcoat_hub.chat_tools import ToolCheck
from topcoat_hub.config import Settings
from topcoat_hub.gateway import CUT, Gateway, GatewayError
from topcoat_hub.report_store import ReportError, ReportStore
from topcoat_hub.tools import Caller, ToolError

log = logging.getLogger(__name__)

KEEP_FINISHED_SECONDS = 1800.0
# an agent block whose start is refused because the person's agent runs are full waits and tries
# again (agent_service: "too many tasks are running"); the limit only ends a wait that never ends
BUSY = "too_many_tasks"  # ToolCheck.result.reason of that refusal
AGENT_WAIT_SECONDS = 15.0
AGENT_WAIT_LIMIT_SECONDS = 3 * 3600.0
TOOL_WAIT_SECONDS = 5.0  # a tool block whose capability is not offered (yet) looks again
# the events of a task that show what the agent does now: its start, its plan and calls (tool_step
# carries the node), and the heartbeat while it waits
AGENT_PROGRESS = frozenset({"tools_start", "tool_step", "tools_wait"})
MAX_ACTIVE_PER_USER = 3
REPORT_WAIT_TRIES = 30  # a finished thorough research: how long the report may take to appear
REPORT_WAIT_SECONDS = 1.0
# models whose server refused ``response_format`` json_schema (400/422): asked without it until
# the hub restarts, the tolerant JSON reader is the net (as agent_service does for json_object)
_NO_SCHEMA: set[str] = set()
# families whose schema answers break with thinking on: a schema step runs there with thinking
# off. Bench 2026-11-25 (scripts/dev/schema_bench.py, a plan_v2-like schema): Qwen3-Coder on
# node-tower and gpt-oss-20b on the laptop answered in form with thinking on and off, 8 of 8; none
SCHEMA_WITHOUT_THINKING: frozenset[str] = frozenset()


class BlockError(Exception):
    def __init__(self, status: int, code: str, message: str) -> None:
        super().__init__(message)
        self.status, self.code, self.message = status, code, message


def findings_text(findings: Any) -> str:
    """The findings of an agent run as lines: tag, claim and the sources that back it."""
    lines = []
    for f in findings if isinstance(findings, list) else []:
        if not isinstance(f, dict) or not str(f.get("claim") or "").strip():
            continue
        refs = ", ".join(str(r) for r in f.get("evidence") or [])
        lines.append(f"- [{f.get('tag', '')}] {f['claim']}" + (f" ({refs})" if refs else ""))
    return "\n".join(lines)


class HubBlockPorts:
    """Models through the gateway (the pool only), the agent for one person, the Ablage."""

    def __init__(
        self,
        gateway: Gateway,
        agents: AgentService,
        sessionmaker: async_sessionmaker[AsyncSession],
        settings: Settings,
        user: CurrentUser,
    ) -> None:
        self.gateway, self.agents, self.sessionmaker = gateway, agents, sessionmaker
        self.settings, self.user = settings, user
        self.report_id: uuid.UUID | None = None  # the report the agent block's task filed

    async def candidates(self) -> list[picks.Candidate]:
        out: list[picks.Candidate] = []
        manual = self.gateway.policy.model_kinds
        proven = self.gateway.proven_kinds
        rated: dict[tuple[str, str], dict[str, float]] = {}
        for row in await latest_ratings(self.sessionmaker):
            if row["current"] and not row["variant"]:  # an older suite version or a tried
                # setting does not rank
                rated.setdefault((row["model"], row["node"]), {})[row["suite"]] = row["score"]
        policy = self.gateway.policy
        for view in await self.gateway.node_views():  # the pool only
            if not view.connected:
                continue
            for name, loaded in view.models.items():
                # with house models a node offers its house model and nothing else (a model the
                # node merely holds is not what the admin chose for it)
                if policy.house and policy.house.get(view.name) != name:
                    continue
                out.append(
                    picks.Candidate(
                        name,
                        view.name,
                        model_facts.kinds_of(name, manual, proven, view.gguf.get(name)),
                        view.sizes_mb.get(name),
                        view.speeds.get(name),
                        loaded,
                        rated.get((name, view.name), {}),
                    )
                )
        if policy.house and any(c.node not in policy.workers for c in out):
            # a worker keeps its small model for background work, as in the resolver
            out = [c for c in out if c.node not in policy.workers]
        return out

    async def _house_target(self, without: str = "") -> tuple[str, str] | None:
        """``(model@node, model)`` for a step no pick decides, from the house models of the nodes
        that are no worker, the first with a free place first; ``None`` without house models (the
        bare model name then goes to the gateway as before). Issue 2.24 (live 2026-11-23): only the
        name went to the gateway, which sent it to any node holding that model, the laptop worker
        too, and a foreground step waited for a CPU."""
        chain = [
            (n, m) for n, m in self.gateway.resolver.house_entries("answer", None) if n != without
        ]
        if not chain:
            return None
        found = await node_chain.pick(self.gateway, chain)
        if found is None:
            return None
        return f"{found.model}@{found.node}", found.model

    async def _retry_target(
        self, requested: str, model: str, picked: picks.Picked | None
    ) -> tuple[str, str] | None:
        """Where a step whose answer broke off is tried once more: the same model on another node
        (a pick keeps its model, so its rules, e.g. "other than the author", still hold); without a
        pick the next house model of the nodes that are no worker. ``None``: nowhere else, the
        request itself once more when it named no node (the gateway may route it elsewhere)."""
        node = requested.partition("@")[2]
        if not node:
            return requested, model
        if picked is None:
            return await self._house_target(without=node)
        for c in await self.candidates():
            if c.model == model and c.node != node:
                return f"{c.model}@{c.node}", c.model
        return None

    async def _call(self, requested: str, model: str, payload: dict[str, Any]) -> dict[str, Any]:
        """One request of a block step. A server that refuses the enforced form (400/422 while
        ``response_format`` is set) is asked again without it, and the model is remembered."""
        try:
            return await self.gateway.call_streamed(
                requested, "/v1/chat/completions", payload, label="block"
            )
        except GatewayError as exc:
            if "response_format" not in payload or exc.status not in (400, 422) or exc.reason:
                raise
            _NO_SCHEMA.add(model)
            payload.pop("response_format")
            log.warning("block step: %s refused json_schema (%s), asked without it", model, exc)
            return await self.gateway.call_streamed(
                requested, "/v1/chat/completions", payload, label="block"
            )

    async def ask(
        self,
        messages: list[dict[str, str]],
        picked: picks.Picked | None,
        max_tokens: int,
        temperature: float,
        think: bool | None = None,
        schema: dict[str, Any] | None = None,
    ) -> tuple[str, str]:
        if picked is not None:
            requested, model = picked.requested, picked.model
        elif (target := await self._house_target()) is not None:
            requested, model = target
        else:
            model = await self.gateway.resolver.default_model()
            requested = model
        payload: dict[str, Any] = {
            "stream": False,
            "temperature": temperature,
            "max_tokens": max_tokens,
            "messages": messages,
        }
        if schema is not None and model_profiles.family_of(model) in SCHEMA_WITHOUT_THINKING:
            think = False
        if think is not None:  # the program's own word on reasoning (a check that must answer JSON)
            payload["chat_template_kwargs"] = {"enable_thinking": think}
        if schema is not None and model not in _NO_SCHEMA:
            payload["response_format"] = {
                "type": "json_schema",
                "json_schema": {"name": "answer", "schema": schema},
            }
        if picked is not None and picked.exclusive and picked.node:
            await self.gateway.unload_others(picked.node, picked.model)
        try:
            data = await self._call(requested, model, payload)
        except GatewayError as exc:
            if exc.reason != CUT:
                raise BlockFailure(exc.message) from exc
            # the answer broke off on the node (live 2026-11-22: gpt-oss was unloaded on node-desk
            # and two documentation runs lost 45 and 55 finished paragraphs): once more on another
            # place, then the run ends as before (issue 2.18)
            again = await self._retry_target(requested, model, picked)
            if again is None:
                raise BlockFailure(exc.message) from exc
            log.warning("block step: %s broke off (%s), once more on %s", requested, exc, again[0])
            requested, model = again
            try:
                data = await self._call(requested, model, payload)
            except GatewayError as exc2:
                raise BlockFailure(
                    f"{exc.message}; once more on {requested}: {exc2.message}"
                ) from exc2
        choices = data.get("choices")
        message = choices[0].get("message") if isinstance(choices, list) and choices else None
        text = message.get("content") if isinstance(message, dict) else None
        if not isinstance(text, str) or not text.strip():
            finish = (
                choices[0].get("finish_reason") if isinstance(choices, list) and choices else None
            )
            used = (data.get("usage") or {}).get("completion_tokens")
            thought = message.get("reasoning_content") if isinstance(message, dict) else None
            raise EmptyAnswer(
                f"{model} answered with nothing (finish: {finish}, tokens: {used}, "
                f"max_tokens: {max_tokens})",
                thought if isinstance(thought, str) else "",
            )
        return text, model

    async def agent(
        self,
        prompt: str,
        application: str,
        depth: str,
        picked: picks.Picked | None,
        hooks: dict[str, Any] | None = None,
        progress: Progress | None = None,
    ) -> AgentOutcome:
        if application == research_profile.APPLICATION:
            prompt = f"{research_profile.depth_line(depth)}\n{prompt}"
        waited = 0.0
        while True:
            check = ToolCheck(
                self.agents,
                self.sessionmaker,
                self.user,
                prompt,
                picked.requested if picked else None,
                limits=None,  # type: ignore[arg-type]  # the application's own frame decides
                origin=ORIGIN_BLOCK,
                application=application,
                hooks=hooks,
            )
            # the task's own progress (its tool calls with the node they run on, its waits) goes to
            # the live view of the block; the transcript itself is the task's (not copied here)
            async for event, data in check.events():
                if progress is not None and event in AGENT_PROGRESS:
                    progress({"what": event, "data": data})
            full = check.result.task is None and check.result.reason == BUSY
            if not full or waited >= AGENT_WAIT_LIMIT_SECONDS:
                break
            # the person's agent runs are all taken: wait for a free one instead of giving up
            # (a page of a documentation came out empty when its readings were refused)
            if progress is not None:
                progress(
                    {"what": "tools_wait", "data": {"reason": "agent runs full", "waited": waited}}
                )
            await asyncio.sleep(AGENT_WAIT_SECONDS)
            waited += AGENT_WAIT_SECONDS
        task = check.result.task
        if task is None or task.status != "done":
            err = check.result.error or (task.error if task else None) or "the task did not finish"
            return AgentOutcome(False, error=err, task_id=str(task.id) if task else "")
        text = task.summary or ""
        table = (task.diagnosis or {}).get("table")
        if "{" not in text and isinstance(table, list) and table and isinstance(table[0], dict):
            # asked for one result object, a model may finish with it as a table row instead
            text = json.dumps(table[0], ensure_ascii=False)
        found = findings_text((task.diagnosis or {}).get("findings"))
        if found:  # what the run found, each with the sources (E1 ...) it kept for it
            text = f"{text}\n\nFindings:\n{found}"
        # a thorough research files its report by itself, right after the task ends: wait for it
        # (the task is seen as done a moment before the report is written)
        wanted = application == research_profile.APPLICATION and depth == "thorough"
        for attempt in range(REPORT_WAIT_TRIES if wanted else 1):
            async with self.sessionmaker() as db:
                store = ReportStore(db, self.user.uuid)
                found = await store.by_task(task.id)
                if found is not None:
                    self.report_id = found.id
                    text = (await store.version(found.id, found.current_version)).body or text
                    break
            if wanted and attempt + 1 < REPORT_WAIT_TRIES:
                await asyncio.sleep(REPORT_WAIT_SECONDS)
        return AgentOutcome(True, text, model=picked.model if picked else "", task_id=str(task.id))

    async def tool(
        self, capability: str, tool_input: dict[str, Any], node: str | None, application: str
    ) -> ToolOutcome:
        """One capability, offered and called exactly as for an agent task of ``application``:
        the person's grants (D20), the application's tool ceiling (app_scope) and the node's
        whitelist and schema (tools.invoke). Only what the agent could read, nothing more."""
        caller = replace(Caller.of(self.user, "agent"), application=application)
        scope = await self.agents._within_application(self.user, application, None)
        waited = 0.0
        while True:
            usable = tool_groups.apply_scope(await self.agents.tools.available(caller), scope)
            offers = [
                t
                for t in usable
                if t.get("capability") == capability and (node is None or t.get("node") == node)
            ]
            limit = float(getattr(self.settings, "agent_tools_wait_seconds", 60.0)) * 2
            if offers or waited >= limit:
                break
            # right after a hub restart the nodes connect again: wait for them before giving up
            # (a documentation batch lost 19 pages in seconds to "repo.list is not offered")
            await asyncio.sleep(TOOL_WAIT_SECONDS)
            waited += TOOL_WAIT_SECONDS
        if not offers:
            where = f" on {node}" if node else ""
            raise BlockFailure(
                f"{capability}{where} is not offered here (the person's grants, a connected node "
                f"and the tool ceiling of {application} decide)"
            )
        if node is None and len({t.get("node") for t in offers}) > 1:
            names = ", ".join(sorted(str(t.get("node")) for t in offers))
            raise BlockFailure(f"{capability} is offered on {names}: the block must name the node")
        entry = offers[0]
        try:
            res = await self.agents.tools.invoke(
                caller, uuid.UUID(str(entry["node_id"])), capability, tool_input
            )
        except ToolError as exc:
            raise BlockFailure(f"{capability}: {exc.message}") from exc
        if not res.get("ok", True):
            err = res.get("error") or {}
            message = err.get("message") if isinstance(err, dict) else str(err)
            raise BlockFailure(f"{capability}: {message or 'the call failed'}")
        output = res.get("output", res)
        text = output if isinstance(output, str) else json.dumps(output, ensure_ascii=False)
        call = {"capability": capability, "node": entry.get("node"), "input": tool_input}
        return ToolOutcome(text, str(entry.get("node") or ""), call)

    async def file_report(self, title: str, body: str) -> str | None:
        async with self.sessionmaker() as db:
            store = ReportStore(db, self.user.uuid)
            try:
                if self.report_id is not None:
                    # the model revised the whole document: only its own parts are taken, the
                    # code's parts (title page, facts, sources, origin) stay (report_splice.py)
                    report = await store.get(self.report_id)
                    current = await store.version(report.id, report.current_version)
                    spliced = report_splice.splice(
                        current.body or "", body, report.language, report.current_version + 1
                    )
                    if spliced is None:
                        raise ReportError("the revision has no foreword and no chapter")
                    body = spliced
                    await store.add_version(
                        self.report_id, body=body, meta={"by": "block program"}, kind="revision"
                    )
                    report_id = self.report_id
                else:
                    created = await store.create(
                        title=title or "Report",
                        question=title,
                        language=hub_language("", title),
                        depth="thorough",
                        body=body,
                        meta={"by": "block program"},
                    )
                    report_id = created.id
            except ReportError as exc:  # the text did not change: the report stays as it is
                log.info("blocks: nothing filed: %s", exc)
                return str(self.report_id) if self.report_id else None
            await db.commit()
        return str(report_id)


@dataclass
class LiveBlockRun:
    run_id: uuid.UUID
    user_id: uuid.UUID
    program: dict[str, Any]
    engine: BlockEngine
    task: asyncio.Task[Any] | None = None
    events: list[tuple[str, dict[str, Any]]] = field(default_factory=list)
    # what it was started with (the question of a research, for its list in the application)
    inputs: dict[str, Any] = field(default_factory=dict)

    @property
    def result(self) -> RunResult:
        return self.engine.result

    @property
    def finished(self) -> bool:
        return self.task is not None and self.task.done()


class BlockService:
    def __init__(
        self,
        gateway: Gateway,
        agents: AgentService,
        sessionmaker: async_sessionmaker[AsyncSession],
        settings: Settings,
    ) -> None:
        self.gateway, self.agents, self.sessionmaker, self.settings = (
            gateway,
            agents,
            sessionmaker,
            settings,
        )
        self._runs: dict[uuid.UUID, LiveBlockRun] = {}

    def programs(self) -> dict[str, dict[str, Any]]:
        return builtin.PROGRAMS

    def runs_of(self, user_id: uuid.UUID, program: str | None = None) -> list[LiveBlockRun]:
        """This person's runs the hub still holds (running, or finished a little while ago),
        of one program if named; the newest first."""
        mine = [
            r
            for r in self._runs.values()
            if r.user_id == user_id and (program is None or r.program.get("name") == program)
        ]
        return list(reversed(mine))

    def get(self, user_id: uuid.UUID, run_id: uuid.UUID) -> LiveBlockRun | None:
        run = self._runs.get(run_id)
        return run if run is not None and run.user_id == user_id else None

    def has_room(self, user_id: uuid.UUID) -> bool:
        """May this person start one more run now (MAX_ACTIVE_PER_USER)?"""
        active = sum(1 for r in self._runs.values() if r.user_id == user_id and not r.finished)
        return active < MAX_ACTIVE_PER_USER

    def start(
        self, user: CurrentUser, program: dict[str, Any], inputs: dict[str, Any]
    ) -> LiveBlockRun:
        from topcoat_hub.blocks import program as prog

        try:
            checked = prog.validate(program)  # what runs is always valid
        except prog.ProgramError as exc:
            raise BlockError(422, "invalid_program", str(exc)) from exc
        active = sum(1 for r in self._runs.values() if r.user_id == user.uuid and not r.finished)
        if active >= MAX_ACTIVE_PER_USER:
            raise BlockError(429, "too_many_runs", "too many runs are going; wait or stop one")
        ports = HubBlockPorts(self.gateway, self.agents, self.sessionmaker, self.settings, user)
        engine = BlockEngine(ports, seed=uuid.uuid4().int & 0xFFFFFFFF)
        run = LiveBlockRun(uuid.uuid4(), user.uuid, checked, engine, inputs=dict(inputs))
        self._runs[run.run_id] = run

        async def drive() -> None:
            try:
                await engine.run(checked, inputs, lambda e, d: run.events.append((e, d)))
            except asyncio.CancelledError:
                pass
            except Exception as exc:  # BlockFailure is handled in the engine; this is a bug
                log.exception("blocks: run %s crashed", run.run_id)
                engine.result.status, engine.result.error = "failed", f"{type(exc).__name__}: {exc}"
            finally:
                asyncio.get_running_loop().call_later(
                    KEEP_FINISHED_SECONDS, self._runs.pop, run.run_id, None
                )

        run.task = asyncio.ensure_future(drive())
        return run

    async def stop(self, user_id: uuid.UUID, run_id: uuid.UUID, wait: float = 10.0) -> bool:
        run = self.get(user_id, run_id)
        if run is None or run.finished or run.task is None:
            return False
        run.task.cancel()
        await asyncio.wait({run.task}, timeout=wait)
        return True

    async def shutdown(self, grace: float = 5.0) -> None:
        tasks = [r.task for r in self._runs.values() if r.task is not None and not r.task.done()]
        for t in tasks:
            t.cancel()
        if tasks:
            await asyncio.wait(tasks, timeout=grace)
