"""The coordinator (E3-08, ARCHITECTURE D44): one question, one read-only check per machine, one
merged report.

A coordinator task never looks at anything itself and has no tool at all — least of all one that
changes something. It (1) asks the model once how to split the question across the machines the
user may look at (the answer is validated against the real machines; anything unusable falls back
to "one check per machine"), (2) starts each check as an ordinary agent task of the same user with
a scope of that one machine, no internet and tighter limits, a few at a time, (3) waits, (4) merges
the results: the children's evidence is copied into the coordinator's own so every finding keeps
pointing at what it was based on, and one model call writes the combined summary. Depth is 1:
only this module starts children, and a child is a plain ``single`` task. Rights: a child runs as
the same user with a scope that only narrows (D36) — never wider. Changes are not possible here:
children get no proposals (they are not ``tasks``-origin) and the coordinator has no tools.

Limits: ``coordinator_parallel`` checks at a time (a node has one model slot, more only queue),
``coordinator_max_children``, ``coordinator_time_budget_seconds`` for the whole run; canceling the
coordinator cancels every check it started. A hub restart leaves the children to the normal
recovery and the coordinator picks them up again from the stored plan.
"""

import asyncio
import dataclasses
import logging
import uuid
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from topcoat_hub import tool_groups, web_access
from topcoat_hub.agent_loop import Limits
from topcoat_hub.agent_store import AgentStore
from topcoat_hub.auth import CurrentUser
from topcoat_hub.config import Settings
from topcoat_hub.github_access import GITHUB_NODE
from topcoat_hub.inventory import INVENTORY_NODE
from topcoat_hub.knowledge import KNOWLEDGE_NODE
from topcoat_hub.memory import parse_json_object
from topcoat_hub.models import User
from topcoat_hub.tools import Caller
from topcoat_hub.topcoat_self import TOPCOAT_NODE

log = logging.getLogger(__name__)

FINAL = ("done", "failed", "canceled")
PLAN_SYSTEM = (
    "You split a question about a home lab into checks, one per machine. Only the machines "
    "listed below can be looked at. Reply with ONE JSON object and nothing else:\n"
    '{"checks":[{"node":"<machine from the list>",'
    '"question":"<what to find out on that machine, one sentence>"}]}\n'
    "Leave a machine out when the question cannot concern it. At most %d checks. Write the "
    "questions in the language of the question."
)
MERGE_SYSTEM = (
    "You merge the reports of several read-only server checks into one. Use only what the "
    "reports say, name the machine for each statement, add no facts, and say which machines "
    "could not be checked. Write in the language of the original question. Reply with ONE JSON "
    'object and nothing else:\n{"summary":"<2-4 sentences>","next":"<advice only, or empty>"}'
)


class Coordinator:
    def __init__(
        self,
        sessionmaker: async_sessionmaker[AsyncSession],
        service: Any,  # AgentService (circular import)
        settings: Settings,
        sleep: Any = asyncio.sleep,
    ) -> None:
        self.sessionmaker = sessionmaker
        self.service = service
        self.settings = settings
        self._sleep = sleep
        # the checks run side by side and each writes a note to the coordinator's transcript: the
        # next step number is read and written, so two of them at once would take the same number
        self._step_lock = asyncio.Lock()

    # --- helpers --------------------------------------------------------------------------

    async def _step(self, task_id: uuid.UUID, user_id: uuid.UUID, text: str, **extra: Any) -> None:
        async with self._step_lock, self.sessionmaker() as db:
            store = AgentStore(db, user_id)
            task = await store.get(task_id)
            if task is not None:
                step = await store.add_step(task, "note", {"text": text, **extra})
                self.service.publish(
                    "agent_step",
                    {"task_id": str(task_id), "seq": step.seq, "_user": str(user_id)},
                )

    def child_limits(self) -> Limits:
        s = self.settings
        return dataclasses.replace(
            self.service.loop.limits,
            max_turns=s.coordinator_child_turns,
            max_tool_calls=s.coordinator_child_tool_calls,
            time_budget_seconds=s.coordinator_child_time_seconds,
        )

    async def _machines(self, caller: Caller, scope: dict[str, Any] | None) -> list[str]:
        """The machines the task's user may look at (with at least one capability of their own:
        the notes and the web are not machines)."""
        available = tool_groups.apply_scope(await self.service.tools.available(caller), scope)
        return sorted(
            {
                t["node"]
                for t in available
                if t.get("node") not in (KNOWLEDGE_NODE, INVENTORY_NODE, TOPCOAT_NODE, GITHUB_NODE)
                and not web_access.is_web(str(t["capability"]))
            }
        )

    async def _plan(
        self, prompt: str, machines: list[str], model: str | None
    ) -> tuple[list[dict[str, str]], str]:
        """[{node, question}] and a note on where it came from. The model proposes; the hub only
        keeps entries for machines that exist, and falls back to one check per machine."""
        cap = max(1, self.settings.coordinator_max_children)
        fallback = [{"node": n, "question": prompt} for n in machines[:cap]]
        if len(machines) == 1:
            return fallback, "one machine: no split needed"
        try:
            c = await asyncio.wait_for(
                self.service.loop.model.complete(
                    [
                        {"role": "system", "content": PLAN_SYSTEM % cap},
                        {
                            "role": "user",
                            "content": f"Machines: {', '.join(machines)}\n\nQuestion: {prompt}",
                        },
                    ],
                    model,
                    600,
                ),
                self.settings.agent_model_timeout_seconds,
            )
            obj = parse_json_object(c.text) or {}
        except Exception as exc:  # a model that is busy or unreachable must not stop the check
            log.info("coordinator: no plan from the model (%s)", exc)
            return fallback, "the model gave no plan: one check per machine"
        plan: list[dict[str, str]] = []
        seen: set[str] = set()
        for item in obj.get("checks") or []:
            if not isinstance(item, dict):
                continue
            node, question = item.get("node"), item.get("question")
            ok = node in machines and node not in seen
            if ok and isinstance(question, str) and question.strip():
                seen.add(node)
                plan.append({"node": node, "question": question.strip()[:500]})
        if not plan:
            return fallback, "the model's plan named no known machine: one check per machine"
        return plan[:cap], "planned by the model"

    # --- the run --------------------------------------------------------------------------

    async def run(self, task_id: uuid.UUID, user_id: uuid.UUID) -> None:
        started = asyncio.get_running_loop().time()
        deadline = started + self.settings.coordinator_time_budget_seconds
        async with self.sessionmaker() as db:
            store = AgentStore(db, user_id)
            task = await store.get(task_id)
            if task is None or task.status in FINAL:
                return
            user = await db.get(User, user_id)
            await store.set_status(
                task, "running", runs=task.runs + 1, started_at=task.started_at or _now()
            )
            self.service._announce(task)
            prompt, model, scope, via = task.prompt, task.model, task.scope, task.via
            steps = list(await store.steps(task_id))
            existing = list(await store.children(task_id))
        current = CurrentUser(
            id=str(user_id),
            name=user.name if user else "",
            role=user.role if user else "user",
            via_key=via == "api",
        )
        admin = bool(user and user.role == "admin" and via == "gui")
        caller = Caller(user_id, admin, "agent")
        child_scope = {"internet": False}

        plan: list[dict[str, str]] = next(
            (s.data["plan"] for s in steps if isinstance(s.data.get("plan"), list)), []
        )
        if not plan and not existing:
            machines = await self._machines(caller, scope)
            if not machines:
                await self._fail(
                    task_id,
                    user_id,
                    "no_tools",
                    "no capability this user may run is available (is a tool node connected?)",
                )
                return
            plan, why = await self._plan(prompt, machines, model)
            await self._step(task_id, user_id, f"Plan: {why}", plan=plan)
        by_node = {c.scope["nodes"][0]: c for c in existing if c.scope and c.scope.get("nodes")}

        sem = asyncio.Semaphore(max(1, self.settings.coordinator_parallel))
        timed_out = False

        async def check(item: dict[str, str]) -> None:
            child = by_node.get(item["node"])
            async with sem:
                if child is None:
                    child = await self.service.start(
                        current,
                        item["question"],
                        model,
                        origin="child",
                        limits=self.child_limits(),
                        scope={**child_scope, "nodes": [item["node"]]},
                        parent_id=task_id,
                    )
                    by_node[item["node"]] = child
                    await self._step(
                        task_id,
                        user_id,
                        f"Started the check of {item['node']}",
                        child=str(child.id),
                        node=item["node"],
                    )
                while True:
                    state = await self._status(user_id, child.id)
                    if state in FINAL or state is None:
                        return
                    await self._sleep(self.settings.coordinator_poll_seconds)

        runs = [asyncio.create_task(check(i)) for i in plan]
        try:
            remaining = max(1.0, deadline - asyncio.get_running_loop().time())
            done, pending = await asyncio.wait(runs, timeout=remaining)
            if pending:
                timed_out = True
                for p in pending:
                    p.cancel()
                await asyncio.gather(*pending, return_exceptions=True)
                for child in list(by_node.values()):
                    await self.service.cancel(user_id, child.id)
            for d in done:
                if d.exception() is not None:
                    log.warning("coordinator %s: a check failed: %s", task_id, d.exception())
        except asyncio.CancelledError:
            for r in runs:
                r.cancel()
            await asyncio.gather(*runs, return_exceptions=True)
            if task_id in self.service._canceled:  # the user stopped it: stop every check too
                for child in list(by_node.values()):
                    await self.service.cancel(user_id, child.id)
            raise
        await self._merge(task_id, user_id, prompt, model, timed_out, [i["node"] for i in plan])

    async def _status(self, user_id: uuid.UUID, child_id: uuid.UUID) -> str | None:
        async with self.sessionmaker() as db:
            t = await AgentStore(db, user_id).get(child_id)
            return t.status if t else None

    async def _fail(
        self, task_id: uuid.UUID, user_id: uuid.UUID, reason: str, message: str
    ) -> None:
        async with self.sessionmaker() as db:
            store = AgentStore(db, user_id)
            task = await store.get(task_id)
            if task is not None and task.status != "canceled":
                await store.set_status(task, "failed", stop_reason=reason, error=message)
                self.service._announce(task)

    # --- the report -----------------------------------------------------------------------

    async def _merge(
        self,
        task_id: uuid.UUID,
        user_id: uuid.UUID,
        prompt: str,
        model: str | None,
        timed_out: bool,
        order: list[str],
    ) -> None:
        async with self.sessionmaker() as db:
            store = AgentStore(db, user_id)
            task = await store.get(task_id)
            if task is None or task.status == "canceled":
                return
            children = {
                c.scope["nodes"][0]: c
                for c in await store.children(task_id)
                if c.scope and c.scope.get("nodes")
            }
            findings: list[dict[str, Any]] = []
            notes: list[str] = []
            reports: list[str] = []
            nexts: list[str] = []
            ok = 0
            for node in order:
                c = children.get(node)
                if c is None:
                    notes.append(f"{node}: the check was not started")
                    continue
                if c.status != "done" or not c.diagnosis:
                    why = c.stop_reason or c.error or c.status
                    notes.append(f"{node}: no result ({c.status}: {why})")
                    reports.append(f"[{node}] no result ({why})")
                    continue
                ok += 1
                mapping: dict[str, str] = {}
                for e in await store.evidence(c.id):
                    ev = await store.add_evidence(
                        task, e.tag, {**e.source, "child": str(c.id)}, e.excerpt, e.bytes
                    )
                    mapping[e.ref] = ev.ref
                d = c.diagnosis
                lines = [f"[{node}] {d.get('summary', '')}"]
                for f in d.get("findings", []):
                    refs = [mapping[r] for r in f.get("evidence", []) if r in mapping]
                    nf = {**f, "evidence": refs, "node": node}
                    if f.get("contradicts"):
                        nf["contradicts"] = [mapping[r] for r in f["contradicts"] if r in mapping]
                    findings.append(nf)
                    lines.append(f"- ({f.get('tag')}) {f.get('claim')}")
                reports.append("\n".join(lines))
                if d.get("next"):
                    nexts.append(f"{node}: {d['next']}")
                notes += [f"{node}: {n}" for n in d.get("notes", [])]
            if timed_out:
                notes.append("the time budget ended: unfinished checks were stopped")
            summary, nxt = await self._summary(prompt, model, reports, nexts, ok, len(order))
            diagnosis = {"summary": summary, "findings": findings, "next": nxt, "notes": notes}
            if ok == 0:
                await store.set_status(
                    task,
                    "failed",
                    stop_reason="children_failed",
                    error="no check delivered a result",
                    diagnosis=diagnosis,
                    summary=summary,
                )
            else:
                await store.set_status(
                    task,
                    "done",
                    summary=summary,
                    diagnosis=diagnosis,
                    stop_reason="time_budget" if timed_out else None,
                )
            await store.add_step(task, "note", {"text": "diagnosis written"})
            self.service._announce(task)

    async def _summary(
        self,
        prompt: str,
        model: str | None,
        reports: list[str],
        nexts: list[str],
        ok: int,
        total: int,
    ) -> tuple[str, str]:
        joined = "\n\n".join(reports)
        text = joined.replace("\n- ", " ")[:1800] or "No check delivered a result."
        fallback = (text, "; ".join(nexts)[:900])
        if ok == 0:
            return fallback
        try:
            c = await asyncio.wait_for(
                self.service.loop.model.complete(
                    [
                        {"role": "system", "content": MERGE_SYSTEM},
                        {
                            "role": "user",
                            "content": f"Question: {prompt}\n\n"
                            f"Reports ({ok} of {total} machines):\n\n{joined}",
                        },
                    ],
                    model,
                    700,
                ),
                self.settings.agent_model_timeout_seconds,
            )
            obj = parse_json_object(c.text) or {}
            summary = obj.get("summary")
            if isinstance(summary, str) and summary.strip():
                nxt = obj.get("next")
                return summary.strip()[:2000], (nxt.strip()[:1000] if isinstance(nxt, str) else "")
        except Exception as exc:
            log.info("coordinator: no merged summary from the model (%s)", exc)
        return fallback


def _now() -> Any:
    from datetime import UTC, datetime

    return datetime.now(UTC)
