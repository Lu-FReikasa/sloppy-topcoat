"""Large tasks: the hub coordinates the sub-tasks of a decomposition (D109, D110, issue #32).

Not the read-only coordinator of D44 (coordinator.py). A *large task* is an ordinary task node of
a project; its sub-tasks are child task nodes, a dependency is a ``blocked_by`` relation between
two of them. There is no store of its own: the tree, the relations with their loop check, the
fixes of the issue pipeline and the Akte hold everything, and the state of a sub-task is derived
from them (:func:`state_of`). The hub coordinates in code; a model is asked only to decompose
(decompose.py), to replan one branch, and to summarise.

* **Decompose** (:meth:`Coordinator.decompose`): one run of ``decompose`` with the task, the map
  of the files it names and the Development contract, checked by the hub. Under the project's
  grant ``project: {decompose}`` at level ``free`` the sub-tasks are filed at once; otherwise
  they are a change set the person confirms, as every other change of the project agent.
* **Run** (:meth:`Coordinator.tick`): every sub-task whose dependencies are done goes through
  the existing issue pipeline (investigate -> steps -> workshop). Reading and planning may run
  side by side, **writing runs one at a time** on the branch ``topcoat/v-<nr>`` (the workshop
  keeps one copy per repository). Before anything that writes, the workshop scope (D110, a
  mandate ``workshop`` for exactly that branch) is checked; without it the large task stops.
* **Failure**: the same failure twice (D120) stops the sub-task's drafts. The hub then asks
  ``decompose`` once to replan that branch only (the sub-task and what depends on it); the old
  nodes are closed (status ``dropped``, never deleted). A second stop goes to the person.
* **The end**: when every sub-task is done, the person gets one roll-out click for the whole
  branch (D104). The click is the person's; nothing here rolls out.
"""

from __future__ import annotations

import json
import logging
import re
import uuid
from collections.abc import Awaitable, Callable, Iterable, Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol

from topcoat_hub import decompose as dc
from topcoat_hub import project_scope
from topcoat_hub.project_mandates import WORKSHOP, active

log = logging.getLogger(__name__)

DAYS = 14  # the workshop scope (D110)
STATES = ("waiting", "running", "failed", "blocked", "done")
READING_LAYERS = ("research",)  # no file: may run beside a writer
PERSON_LAYERS = ("admin",)  # a setting the person makes in the Verwaltung
SAME_FAILURE = 2  # D120
SPEC_FENCE = "```teilaufgabe"
_SPEC = re.compile(r"```teilaufgabe\s*\n(.*?)\n```", re.S)
_LARGE = re.compile(r"^Vorhaben:\s*(\S+)\s*$", re.M)
REPLANNED = "Neu geplant:"  # a line in the body of a sub-task that came from a replan


def branch_of(number: str) -> str:
    """The branch of the large task ``number`` (D109): ``topcoat/v-<nr>``."""
    return "topcoat/v-" + (re.sub(r"[^0-9a-z]+", "-", number.lower()).strip("-") or "0")


def scope_text(number: str, title: str, repo: str) -> str:
    """What the card of the workshop scope says, word for word what the hub enforces (D110)."""
    return (
        f"Vorhaben {number} „{title}“: Der Hub darf in der Werkstatt ({repo}) "
        "auf genau dem Zweig "
        f"{branch_of(number)} die Entwürfe seiner Teilaufgaben anwenden (Änderungen und neue "
        "Dateien), dort committen, Tests, Lint, Typprüfung und Builds laufen lassen, Screenshots "
        "des Werkstatt-Builds mit Testdaten machen und Theme- und Persona-Entwürfe ändern, ohne "
        "Klick je Fix. Jeder Versuch wird geprüft (Umfang, Inhalt, Pfade, keine Geheimnisse, "
        "saubere Werkstatt, Stopp nach gleichem Fehler zweimal) und steht in der Akte. Nicht "
        "abgedeckt: Ausrollen auf den Hub (bleibt ein Klick), Themes oder Personas speichern oder "
        "aktivieren, andere Zweige oder Repositories, topcoat/live und main, Push, Merge, Deploy, "
        f"Löschen. Gilt {DAYS} Tage oder bis zum Widerruf."
    )


# --- the sub-task in its node -------------------------------------------------------------------


def body_of(sub: dc.Subtask, large_number: str, *, replanned: str = "") -> str:
    """The text of a sub-task's node: what a run of the issue pipeline needs (application,
    program, form), the goal, a readable list and the spec the hub reads back (:func:`spec_of`)."""
    if sub.layer == "research":
        head = ["Zielanwendung: research", "Ergebnisform: Bericht"]
    elif sub.layer in PERSON_LAYERS:
        head = ["Zielanwendung: admin", "Ergebnisform: Bericht"]
    else:
        head = ["Zielanwendung: dev", "Programm: issue_investigate", "Ergebnisform: Diagnose",
                "Art: issue"]  # fmt: skip
    lines = [*head, f"Vorhaben: {large_number}", f"Ebene: {sub.layer}"]
    if replanned:
        lines.append(f"{REPLANNED} {replanned}")
    lines += ["", sub.goal, ""]
    if sub.files:
        lines.append("Dateien: " + ", ".join(f"`{f}`" for f in sub.files))
    if sub.symbols:
        lines.append("Stellen: " + ", ".join(f"`{s}`" for s in sub.symbols))
    if sub.new_files:
        lines.append("Neue Dateien: " + ", ".join(f"`{f}`" for f in sub.new_files))
    lines.append("Fertig, wenn:")
    lines += [f"- {a}" for a in sub.accept]
    spec = {k: v for k, v in sub.__dict__.items() if k not in ("title", "goal")}
    lines += ["", SPEC_FENCE, json.dumps(spec, ensure_ascii=False), "```"]
    return "\n".join(lines)


def spec_of(title: str, body: str) -> dc.Subtask | None:
    """The sub-task a node's text holds, or ``None`` (not a sub-task of a large task)."""
    m = _SPEC.search(body or "")
    if not m:
        return None
    try:
        raw = json.loads(m.group(1))
    except ValueError:
        return None
    if not isinstance(raw, dict) or not isinstance(raw.get("id"), str):
        return None

    def strings(key: str) -> list[str]:
        value = raw.get(key)
        return [str(v) for v in value] if isinstance(value, list) else []

    goal = body.split("\n\n", 2)[1].strip() if body.count("\n\n") >= 2 else ""
    return dc.Subtask(
        id=raw["id"],
        title=title,
        goal=goal,
        layer=str(raw.get("layer") or ""),
        files=strings("files"),
        symbols=strings("symbols"),
        new_files=strings("new_files"),
        accept=strings("accept"),
        depends_on=strings("depends_on"),
    )


def large_of(body: str) -> str | None:
    """The number of the large task a sub-task's text names: only a text that holds a sub-task's
    spec, so a line "Vorhaben: x" in a free issue text makes no sub-task."""
    if spec_of("", body) is None:
        return None
    m = _LARGE.search(body or "")
    return m.group(1) if m else None


# --- states and the view --------------------------------------------------------------------------


@dataclass
class Item:
    """A sub-task as the coordinator and the view see it."""

    node: Any  # ProjectNode
    spec: dc.Subtask
    state: str = "waiting"
    fix: dict[str, Any] | None = None  # the latest fix of the issue pipeline, if any
    repeats: int = 0  # how often the newest failure fingerprint came in a row (D120)


def state_of(status: str | None, deps_done: bool, fix_state: str | None) -> str:
    """The state of a sub-task (D109) from the node's status, its dependencies and its newest fix.

    ``waiting``: dependencies not done, or ready but not started; ``running``: in progress or a
    fix in the workshop; ``failed``: its newest fix failed and nothing runs; ``blocked``: needs the
    person; ``done``."""
    if status == "done":
        return "done"
    if status == "blocked":
        return "blocked"
    if fix_state in ("applying", "testing"):
        return "running"
    if fix_state == "failed":  # before the node's status: a failed fix leaves it in_progress
        return "failed"
    if status in ("in_progress", "in_review"):
        return "running"
    return "waiting" if not deps_done or status == "todo" else "running"


def items_of(
    large: Any, nodes: Sequence[Any], fixes: dict[str, list[Any]] | None = None
) -> list[Item]:
    """The open sub-tasks of ``large`` (its child task nodes that hold a spec; dropped ones are
    left out), each with its state."""
    from topcoat_hub import failure_print

    fixes = fixes or {}
    children = [
        n
        for n in nodes
        if n.parent_id == large.id and n.kind == "task" and n.status != "dropped"
        and spec_of(n.title, n.body) is not None
    ]  # fmt: skip
    done = {str(n.id) for n in children if n.status == "done"}
    out: list[Item] = []
    for n in children:
        spec = spec_of(n.title, n.body)
        if spec is None:  # filtered above
            continue
        own = fixes.get(str(n.id)) or []
        newest = own[-1] if own else None
        failed: list[Any] = []
        for f in reversed(own):
            if getattr(f, "state", "") == "failed":
                failed.append(f)
            elif getattr(f, "state", "") != "discarded":
                break
        prints = [failure_print.of_fix(f) for f in failed]
        deps = [d for d in (getattr(n, "depends_on", None) or [])]
        deps_done = all(d in done for d in deps)
        fix_view = None
        if newest is not None:
            fix_view = {
                "id": str(newest.id),
                "state": newest.state,
                "summary": getattr(newest, "summary", "") or "",
                "error": getattr(newest, "error", None),
            }
        item = Item(n, spec, fix=fix_view, repeats=failure_print.repeats(prints) if prints else 0)
        item.state = state_of(n.status, deps_done, newest.state if newest else None)
        out.append(item)
    return out


def next_step(item: Item) -> str:
    """What happens next with a sub-task, in words for the view (German, like the issue cards)."""
    if item.state == "done":
        return "fertig"
    if item.state == "blocked":
        return "wartet auf dich"
    if item.state == "running":
        return "läuft"
    if item.state == "failed":
        return "wird neu geplant" if item.repeats >= SAME_FAILURE else "nächster Entwurf"
    if item.spec.layer in PERSON_LAYERS:
        return "eine Einstellung durch dich"
    return "startet, wenn die Abhängigkeiten fertig sind"


def view(
    large: Any, project_title: str, items: Sequence[Item], mandate: Any | None, repo: str
) -> dict:
    """The API shape the view of issue #33 reads (agreed in the PR of #32). ``scope_text`` is the
    card of the workshop scope in the hub's own words (:func:`scope_text`, the same text the click
    stores in the mandate), so the card cannot drift from what the click grants (issue #190)."""
    by_node = {str(i.node.id): i.spec.id for i in items}
    subtasks = [
        {
            "id": i.spec.id,
            "node_id": str(i.node.id),
            "number": i.node.number,
            "title": i.node.title,
            "goal": i.spec.goal,
            "layer": i.spec.layer,
            "files": i.spec.files,
            "new_files": i.spec.new_files,
            "accept": i.spec.accept,
            "depends_on": [by_node[d] for d in (i.node.depends_on or []) if d in by_node],
            "state": i.state,
            "status": i.node.status,
            "result": i.fix,
            "next": next_step(i),
        }
        for i in items
    ]
    states = [i.state for i in items]
    if not items:
        overall = "planning"
    elif all(s == "done" for s in states):
        overall = "done"
    elif "blocked" in states:
        overall = "blocked"
    else:
        overall = "running"
    scope = None
    if mandate is not None:
        scope = {
            "id": str(mandate.id),
            "state": "active" if active(mandate) else "ended",
            "expires_at": mandate.expires_at.isoformat(),
        }
    return {
        "large_task": {
            "id": str(large.id),
            "project_id": str(large.project_id),
            "project": project_title,
            "number": large.number,
            "title": large.title,
            "branch": branch_of(large.number),
            "state": overall,
            "scope": scope,
            "scope_text": scope_text(large.number, large.title, repo),
        },
        "subtasks": subtasks,
        "rollout": {"ready": overall == "done"},
    }


# --- choosing what runs ---------------------------------------------------------------------------


def to_start(items: Sequence[Item]) -> list[Item]:
    """The sub-tasks to start now: ready ones (every dependency done, not started, no fix yet), in
    the order of their numbers; at most one that writes, and none while another writer runs."""
    done = {str(i.node.id) for i in items if i.state == "done"}
    writing = any(
        i.state == "running" and i.spec.layer not in READING_LAYERS + PERSON_LAYERS for i in items
    )
    out: list[Item] = []
    for i in items:
        if i.state != "waiting" or i.node.status != "todo" or i.fix is not None:
            continue
        if not all(d in done for d in (i.node.depends_on or [])):
            continue
        if i.spec.layer in READING_LAYERS + PERSON_LAYERS:
            out.append(i)
        elif not writing:
            out.append(i)
            writing = True
    return out


def branch_ids(items: Sequence[Item], failed: str) -> list[str]:
    """The spec ids of the branch below the failed sub-task: it and everything that depends on it
    (directly or not) and is not done, in the order of the items."""
    node_of = {i.spec.id: str(i.node.id) for i in items}
    spec_of_node = {str(i.node.id): i.spec.id for i in items}
    branch = {failed}
    changed = True
    while changed:
        changed = False
        for i in items:
            if i.spec.id in branch or i.state == "done":
                continue
            deps = {spec_of_node.get(d) for d in (i.node.depends_on or [])}
            if deps & branch:
                branch.add(i.spec.id)
                changed = True
    return [i.spec.id for i in items if i.spec.id in branch and i.spec.id in node_of]


def replan_hint(items: Sequence[Item], branch: Sequence[str], why: str) -> str:
    """What ``decompose`` is told when it replans one branch."""
    kept = [i for i in items if i.spec.id not in branch]
    lines = [
        "REPLAN ONE BRANCH. The large task is already cut; these sub-tasks stay as they are "
        "(their ids may be named in depends_on):",
        *(f"- {i.spec.id} ({i.state}): {i.node.title}" for i in kept),
        "",
        f"These sub-tasks are replaced: {', '.join(branch)}. The first of them failed twice the "
        f"same way: {why}",
        "Answer with the sub-tasks that replace them only, with NEW ids (not "
        + ", ".join(i.spec.id for i in items)
        + "); cut the failed work smaller or another way.",
    ]
    return "\n".join(lines)


# --- the coordinator -----------------------------------------------------------------------------


class Pipeline(Protocol):
    """The issue pipeline as the coordinator drives it."""

    async def start(self, user_id: uuid.UUID, node_id: uuid.UUID) -> None:
        """Start the sub-task's investigation (reads and plans; writes nothing)."""

    async def fixes(self, user_id: uuid.UUID, node_ids: Iterable[uuid.UUID]) -> dict[str, list]:
        """The fixes of each node, oldest first."""


@dataclass
class Tick:
    """What one tick did, for the log and the tests."""

    started: list[str] = field(default_factory=list)
    replanned: list[str] = field(default_factory=list)
    blocked: list[str] = field(default_factory=list)
    stopped: str = ""  # why nothing that writes was started (no workshop scope)
    done: bool = False


class DecomposeError(Exception):
    def __init__(self, code: str, message: str, problems: list[str] | None = None) -> None:
        super().__init__(message)
        self.code, self.message, self.problems = code, message, problems or []


Ask = dc.Ask
Read = dc.Read
Outline = Callable[[str], Awaitable[list[dict[str, Any]]]]


class Coordinator:
    """Decompose, file, run and replan the sub-tasks of large tasks. ``store`` is the project
    store (with mandates), ``changesets`` the change set store; the model, the live code and the
    pipeline come in as functions so the tests can give fakes."""

    def __init__(
        self,
        store: Any,
        changesets: Any,
        pipeline: Pipeline,
        *,
        repo: str,
        contract: str = "",
        bus: Any = None,
    ) -> None:
        self.store = store
        self.changesets = changesets
        self.pipeline = pipeline
        self.repo = repo
        self.contract = contract
        self.bus = bus  # "a sub-task is blocked" reaches the inbox at once

    # -- decompose ---------------------------------------------------------------------------

    async def repo_map(self, text: str, outline: Outline | None) -> str:
        """The map ``decompose`` gets: the names and lines of the files the task text names."""
        from topcoat_hub import issue_investigation as ii

        if outline is None:
            return ""
        files: list[dict[str, Any]] = []
        for path in ii.named_files(text, limit=8):
            try:
                files += [f for f in await outline(path) if isinstance(f, dict)]
            except Exception:  # the map is a help, never a reason to fail
                log.info("vorhaben: no outline of %s", path, exc_info=True)
        return ii.outline_text(files, limit=dc.MAP_CHARS)

    async def decompose(
        self,
        user_id: uuid.UUID,
        large_id: uuid.UUID,
        ask: Ask,
        read: Read,
        outline: Outline | None = None,
    ) -> dict[str, Any]:
        """Decompose the large task and file its sub-tasks: at once under the grant ``project:
        {decompose}`` at level free, else as a change set the person confirms."""
        large = await self.store.get_node(user_id, large_id)
        if large.kind != "task":
            raise DecomposeError("not_task", "only a task can be a large task")
        project = await self.store.get_project(user_id, large.project_id)
        nodes = await self.store.tree(user_id, large.project_id)
        if any(n.parent_id == large.id and n.status != "dropped" for n in nodes):
            raise DecomposeError("has_children", "this task has sub-tasks already")
        level = project_scope.allowed(project.scope, "project", "decompose")
        task = f"{large.title}\n\n{large.body}"
        out = await dc.decompose(ask, read, task, await self.repo_map(task, outline), self.contract)
        if out.decomposition is None:
            raise DecomposeError(
                "refused", "the decomposition did not pass the hub's check", out.problems
            )
        dec = out.decomposition
        if level == "free":
            created = await self.file(user_id, large, dec.subtasks, {})
            return {"filed": [str(n.id) for n in created], "change_set": None,
                    "summary": dec.summary, "model": out.model}  # fmt: skip
        row = await self.changesets.propose(
            user_id,
            large.project_id,
            operations(large, dec.subtasks, {}),
            title=f"Teilaufgaben von {large.number}",
            summary=dec.summary,
        )
        return {"filed": [], "change_set": str(row.id), "summary": dec.summary,
                "model": out.model}  # fmt: skip

    async def file(
        self,
        user_id: uuid.UUID,
        large: Any,
        subs: Sequence[dc.Subtask],
        known: dict[str, uuid.UUID],
        *,
        replanned: str = "",
    ) -> list[Any]:
        """File ``subs`` as child task nodes of ``large``, in an order that files a dependency
        first; ``known`` maps spec ids outside ``subs`` to their nodes."""
        ids = dict(known)
        by_id = {s.id: s for s in subs}
        order = dc.topological([(s.id, s.depends_on) for s in subs]) or [s.id for s in subs]
        created: list[Any] = []
        for sid in order:
            s = by_id[sid]
            node = await self.store.create_node(
                user_id,
                large.project_id,
                kind="task",
                title=s.title or s.id,
                body=body_of(s, large.number, replanned=replanned),
                parent_id=large.id,
                depends_on=[ids[d] for d in s.depends_on if d in ids],
            )
            ids[sid] = node.id
            created.append(node)
        return created

    # -- one tick ----------------------------------------------------------------------------

    async def items(self, user_id: uuid.UUID, large: Any) -> list[Item]:
        nodes = await self.store.tree(user_id, large.project_id)
        children = [n.id for n in nodes if n.parent_id == large.id]
        fixes = await self.pipeline.fixes(user_id, children) if children else {}
        return items_of(large, nodes, fixes)

    async def scope(self, user_id: uuid.UUID, large: Any) -> Any | None:
        """The active workshop scope of exactly this large task's branch, or ``None``."""
        return await self.store.find_mandate(
            user_id, large.project_id, self.repo, branch_of(large.number), WORKSHOP
        )

    async def view(self, user_id: uuid.UUID, large_id: uuid.UUID) -> dict[str, Any]:
        from topcoat_hub.issue_fixes import LIVE_REPO  # the repository the scope's click names

        large = await self.store.get_node(user_id, large_id)
        project = await self.store.get_project(user_id, large.project_id)
        items = await self.items(user_id, large)
        mandates = [
            m
            for m in await self.store.mandates_of(user_id, large.project_id)
            if m.branch == branch_of(large.number) and WORKSHOP in (m.actions or [])
        ]
        return view(large, project.title, items, mandates[0] if mandates else None, LIVE_REPO)

    async def tick(
        self,
        user_id: uuid.UUID,
        large_id: uuid.UUID,
        ask: Ask | None = None,
        read: Read | None = None,
    ) -> Tick:
        """One step of a large task: replan a branch whose sub-task stopped, block what failed
        twice, start what is ready. Writing sub-tasks start only under the workshop scope; the
        scope is checked here, on every tick, never remembered."""
        out = Tick()
        large = await self.store.get_node(user_id, large_id)
        items = await self.items(user_id, large)
        if items and all(i.state == "done" for i in items):
            out.done = True
            return out
        for item in [i for i in items if i.state == "failed" and i.repeats >= SAME_FAILURE]:
            why = str((item.fix or {}).get("error") or "the same failure twice")
            if REPLANNED in (item.node.body or "") or ask is None or read is None:
                await self._block(user_id, item, why)
                out.blocked.append(item.spec.id)
                continue
            if await self.replan(user_id, large, items, item, why, ask, read):
                out.replanned.append(item.spec.id)
            else:
                await self._block(user_id, item, why)
                out.blocked.append(item.spec.id)
            return out  # the tree changed: the next tick sees the new one
        ready = to_start(items)
        writers = [i for i in ready if i.spec.layer not in READING_LAYERS + PERSON_LAYERS]
        if writers and await self.scope(user_id, large) is None:
            out.stopped = "no active workshop scope for " + branch_of(large.number)
            ready = [i for i in ready if i not in writers]
        for item in ready:
            if item.spec.layer in PERSON_LAYERS:
                await self._block(user_id, item, "eine Einstellung in der Verwaltung")
                out.blocked.append(item.spec.id)
                continue
            await self.pipeline.start(user_id, item.node.id)
            out.started.append(item.spec.id)
        return out

    async def replan(
        self,
        user_id: uuid.UUID,
        large: Any,
        items: Sequence[Item],
        failed: Item,
        why: str,
        ask: Ask,
        read: Read,
    ) -> bool:
        """Ask ``decompose`` once for the branch below ``failed``; close the old nodes and file the
        new ones. False when the answer does not pass the check."""
        branch = branch_ids(items, failed.spec.id)
        kept = {i.spec.id: i.node.id for i in items if i.spec.id not in branch}
        hint = replan_hint(items, branch, why)
        task = f"{large.title}\n\n{large.body}"
        out = await dc.decompose(ask, read, task, "", self.contract, hint=hint,
                                 outside=set(kept), taken={i.spec.id for i in items})  # fmt: skip
        if out.decomposition is None:
            return False
        for i in items:
            if i.spec.id in branch:
                await self.store.update_node(
                    user_id, i.node.id, revision=i.node.revision, status="dropped"
                )
        await self.file(user_id, large, out.decomposition.subtasks, kept,
                        replanned=", ".join(branch))  # fmt: skip
        return True

    async def _block(self, user_id: uuid.UUID, item: Item, why: str) -> None:
        if item.node.status == "blocked":
            return
        await self.store.update_node(
            user_id, item.node.id, revision=item.node.revision, status="blocked"
        )
        log.info("vorhaben: sub-task %s blocked (%s)", item.node.number, why)
        await self._tell(user_id, item, why)

    async def _tell(self, user_id: uuid.UUID, item: Item, why: str) -> None:
        """The inbox item that sends a blocked sub-task to the person (D109). The issue pipeline
        leaves its own ``fix_blocked`` out for sub-tasks, so the replan comes first."""
        from topcoat_hub.inbox import Candidate, InboxStore
        from topcoat_hub.issue_fixes import KIND_BLOCKED
        from topcoat_hub.project_base import APPLICATION

        node = item.node
        async with self.store._sessionmaker() as db:
            await InboxStore(db, user_id).add(
                Candidate(
                    kind=KIND_BLOCKED,
                    source_type="large_task",
                    source_ref=str(node.id),
                    title=f"Teilaufgabe blockiert: {node.number} {node.title}",
                    summary=why,
                    application=APPLICATION,
                    ref={
                        "module": "page.project",
                        "props": {"project": str(node.project_id), "node": str(node.id)},
                        "project_id": str(node.project_id),
                        "node_id": str(node.id),
                    },
                )
            )
            await db.commit()
        if self.bus is not None:
            self.bus.publish("inbox", {"_user": str(user_id)})


def operations(
    large: Any, subs: Sequence[dc.Subtask], known: dict[str, uuid.UUID]
) -> list[dict[str, Any]]:
    """The sub-tasks as ``create_node`` operations of a change set (local names for the new ones,
    so a dependency is named before it is used)."""
    order = dc.topological([(s.id, s.depends_on) for s in subs]) or [s.id for s in subs]
    by_id = {s.id: s for s in subs}
    local = {sid: f"t{n}" for n, sid in enumerate(order, 1)}
    ops: list[dict[str, Any]] = []
    for sid in order:
        s = by_id[sid]
        deps = [
            local[d] if d in local else str(known[d])
            for d in s.depends_on
            if d in local or d in known
        ]
        ops.append({
            "op": "create_node", "local": local[sid], "kind": "task", "title": s.title or s.id,
            "body": body_of(s, large.number), "parent": str(large.id), "depends_on": deps,
        })  # fmt: skip
    return ops
