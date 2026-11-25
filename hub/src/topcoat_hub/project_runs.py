"""Runs of project tasks in the applications, and batches of them (PPH-04, plan of 2026-11-08).

A task node says where its work is done (``Zielanwendung: dev``) and what its result must look like
(``Ergebnisform: Artikel | Aussage | Befund``). Starting it starts an agent task in that application
with the node's text as the request; the node shows ``in_progress`` and points at the task
(``task_id``). When the task ends, the hub checks the result table the finish named against the form
and the checklist of the node: a fitting result sets ``in_review``, anything else
``blocked`` with the
reason. The check is code, never the model's own opinion.

A batch is an approval of several task nodes. As many ready tasks run at once as the nodes of their
applications' rows have request places (:func:`batch_places`: the slots of each connected node,
``n_parallel``); which node and model take a run is the gateway's choice, as for any request of that
application. A task waits for its blockers; a ``blocked`` one holds back its dependants, and the
limit of runs the approval named ends the batch. Batch runs go to the idle place of the node queue
(``agent_service``), so a chat request waits at most for the model call that is running.

Every write is a line of the project's Akte in the same transaction (the store's ``_record``).
"""

import asyncio
import json
import logging
import re
import uuid
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import replace
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from topcoat_hub import (
    build_info,
    clock,
    model_facts,
    model_profiles,
    report_class,
    theme_preview,
)
from topcoat_hub import project_programs as programs
from topcoat_hub import project_revise as revise
from topcoat_hub.agent_store import AgentStore
from topcoat_hub.auth import CurrentUser
from topcoat_hub.blocks.program import with_roles
from topcoat_hub.chat import question_language
from topcoat_hub.knowledge_store import KnowledgeStore
from topcoat_hub.models import (
    AgentEvidence,
    AgentStep,
    AgentTask,
    Project,
    ProjectBatch,
    ProjectEvent,
    ProjectNode,
    User,
)
from topcoat_hub.project_base import (
    NotFound,
    StoreBase,
    blocker_graph,
    bump,
    get_node,
    get_project,
)
from topcoat_hub.project_events import Actor
from topcoat_hub.project_iterations import IterationStore, results_of
from topcoat_hub.project_programs import page_of, program_of
from topcoat_hub.project_progress import checklist
from topcoat_hub.ui import themes

log = logging.getLogger(__name__)

APPLICATIONS = ("research", "dev", "project", "admin")
# the applications only an administrator's task may use: the administration has the machines
ADMIN_ONLY = ("admin",)
RETRY_MARK = "[retry]"  # opens the request of a run that asks again for a missing table
# why a run ended on a node that did not run it: the task goes back into the queue
REQUEUE_REASONS = ("model_unavailable", "node_offline", "node_busy", "timeout", "hub_restart")
ORIGIN = "batch"  # the agent task's origin: idle in the node queue (agent_service.ORIGIN_BATCH)
END_STATES = ("done", "failed", "canceled")
MAX_RUNS = 50
PREVIOUS_REPORT_CHARS = 8000  # of the issue's previous investigation, at the start of the next

_TARGET = re.compile(r"^\s*Zielanwendung:\s*([A-Za-z]+)\s*$", re.MULTILINE)
_FORM = re.compile(r"^\s*Ergebnisform:\s*(.+?)\s*$", re.MULTILINE)
# "lesen" (read only, the default) or "schreiben" (the run may propose changes, each confirmed)
MODES = ("lesen", "schreiben")
_MODE = re.compile(r"^\s*Modus:\s*(lesen|schreiben)\s*$", re.MULTILINE | re.IGNORECASE)


async def unfinished_blockers(
    db: AsyncSession, user_id: uuid.UUID, node_ids: list[uuid.UUID]
) -> dict[str, list[dict[str, str]]]:
    """For each node: the tasks it waits for that are not finished yet (number, title, status).
    A task that is done or dropped no longer holds anything back."""
    graph = await blocker_graph(db, user_id)
    wanted = {b for n in node_ids for b in graph.get(str(n), set())}
    info: dict[str, Any] = {}
    if wanted:
        rows = await db.execute(
            select(ProjectNode.id, ProjectNode.number, ProjectNode.title, ProjectNode.status).where(
                ProjectNode.user_id == user_id,
                ProjectNode.id.in_([uuid.UUID(b) for b in wanted]),
            )
        )
        info = {str(r.id): r for r in rows}
    out: dict[str, list[dict[str, str]]] = {}
    for n in node_ids:
        out[str(n)] = [
            {"number": info[b].number, "title": info[b].title, "status": info[b].status}
            for b in sorted(graph.get(str(n), set()))
            if b in info and info[b].status not in ("done", "dropped")
        ]
    return out


class RunError(ValueError):
    """A run or batch that the rules refuse. The message names the rule."""


class ProgramsBusy(RuntimeError):
    """The person's block programs fill every place now: the task waits, the batch goes on."""


def check_right(target: str, is_admin: bool) -> None:
    """A task for the administration runs only for an administrator (ADMIN_ONLY); the others are
    refused with the rule named, when the task is planned and again when it starts."""
    if target in ADMIN_ONLY and not is_admin:
        raise RunError(f"a task for {target} runs only for an administrator")


# --- pure rules -----------------------------------------------------------------------------------


def mode_of(body: str) -> str:
    """The task's mode: "schreiben" only when its body says so, "lesen" otherwise."""
    found = _MODE.search(body or "")
    return found.group(1).lower() if found else "lesen"


def target_of(body: str) -> str | None:
    """The application a task's work is done in; None when the body names none or an unknown one."""
    found = _TARGET.search(body or "")
    if not found:
        return None
    name = found.group(1).lower()
    return name if name in APPLICATIONS else None


def form_of(body: str) -> list[str]:
    """The columns of the result table the body asks for, in order; empty when it names none."""
    found = _FORM.search(body or "")
    if not found:
        return []
    columns = [c.strip() for c in found.group(1).split("|")]
    return [c for c in columns if c]


def _key(name: str) -> str:
    """A column name as the form and the table compare it: no case; "_", "-" and spaces alike; a
    hint in brackets ("Ergebnis (stimmt / falsch)") does not count, the name is what matters."""
    bare = re.sub(r"\([^)]*\)", " ", name)
    return re.sub(r"[\s_\-]+", " ", bare).strip().casefold()


EVIDENCE = "Beleg"  # every result row names the reads that show it: a column of every form
_REF = re.compile(r"\bE\d+\b")
# the run a filed result names at its end ("_Lauf: <id>_", _file_result)
_RUN_REF = re.compile(r"_Lauf: ([0-9a-f-]{36})_\s*$")


def _is_evidence(key: str) -> bool:
    """A column that names the reads of a row: "Beleg", "Beleg (Pfad/Zeile)", "Beleg Pfad" ..."""
    return key.startswith(_key(EVIDENCE))


def with_evidence(columns: list[str]) -> list[str]:
    """The form plus the evidence column: no row of a result is accepted without a source. A form
    that already names one (any column that starts with Beleg) gets no second one."""
    if any(_is_evidence(_key(c)) for c in columns):
        return columns
    return [*columns, EVIDENCE]


def secret_problem(table: Any) -> str | None:
    """A result that holds a secret (password, key, token) does not go on to review: it waits for
    a person's approval (report_class.py). Sensitive facts (host names, addresses) pass."""
    if report_class.classify(json.dumps(table, ensure_ascii=False)) == report_class.CRITICAL:
        return "the result holds a secret: it waits for a person's approval, not for review"
    return None


def result_problem(
    columns: list[str], table: Any, items: int, evidence: set[str] | None = None
) -> str | None:
    """Why a result does not fit its form, or None when it fits.

    ``table`` is the list of rows the finish named (one dict per checked item). It fits when it has
    at least one row per checklist item of the task and every row carries a non-empty value for
    every column of the form (keys compared without case and extra spaces). ``evidence`` is the set
    of reference names (E1, E2 ...) of the reads this run kept with an excerpt: a row must name at
    least one of them, so a row without a source that the run actually read is refused."""
    if not isinstance(table, list) or not table:
        return "the result has no table"
    if items and len(table) < items:
        return f"the table has {len(table)} rows, the checklist has {items} items"
    if not columns:
        return None
    wanted = {_key(c): c for c in columns}
    for number, row in enumerate(table, start=1):
        if not isinstance(row, dict):
            return f"row {number} is not an object"
        have = {_key(str(k)): v for k, v in row.items()}
        missing = [
            wanted[k] for k in wanted if not _is_evidence(k) and not str(have.get(k) or "").strip()
        ]
        # the evidence columns: any of them may carry the reads, and it must name one the run kept
        evidence_cells = [str(v or "") for k, v in have.items() if _is_evidence(k)]
        if any(_is_evidence(k) for k in wanted) and not any(c.strip() for c in evidence_cells):
            missing += [wanted[k] for k in wanted if _is_evidence(k)]
        if missing:
            return f"row {number} has no value for {', '.join(missing)}"
        if evidence is not None:
            refs = set(_REF.findall(" ".join(evidence_cells)))
            if not refs & evidence:
                # said so that a person (and the project agent) can act on it: the run's result
                # table, not a file, is what failed (live 2026-11-10: the agent searched the notes
                # for "row 2 names no read" and stopped without progress)
                return (
                    f"result check: row {number} of the run's result table cites no evidence "
                    "(E1, E2 ...) the run actually read in its Beleg column, so the row is not "
                    "backed; start the task again for a new run"
                )
    return None


_FORM_LINE = re.compile(r"^\s*(Zielanwendung|Ergebnisform|Programm|Modus):.*$", re.M | re.I)


def report_question(title: str, body: str) -> str:
    """What a report program is asked: the task's title and text without the lines that only
    steer the run (application, result form); the checklist stays, as the points to cover."""
    text = _FORM_LINE.sub("", body or "").strip()
    points = re.sub(r"^\s*- \[[ xX]\]\s*", "- ", text, flags=re.M)
    return f"{title}\n\n{points}".strip()


def _number_key(number: str) -> tuple[int, ...]:
    return tuple(int(p) if p.isdigit() else 0 for p in str(number).split("."))


def theme_result(text: str) -> tuple[str, list[str]]:
    """The theme a program wrote, as the text of its result (a JSON code block, which the GUI shows
    as a card to try), and what the hub's own check of the format finds wrong with it."""
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        return text, ["the answer holds no JSON document"]
    raw = text[start : end + 1]
    try:
        doc = json.loads(raw)
    except ValueError as exc:
        return f"```json\n{raw}\n```", [f"not valid JSON: {exc}"]
    doc, _ = themes.normalise(doc)  # the shapes small models write ("layout": "tile")
    problems = themes.theme_problems(doc)
    shown = f"```json\n{json.dumps(doc, ensure_ascii=False, indent=2)}\n```"
    # what the revisions left of the hub's lint stands under the theme (it is still drawn: the
    # pictures show what it costs)
    hints = [] if problems else themes.theme_lint(doc)
    if hints:
        shown += "\n\n## Hinweise\n\n" + "\n".join(f"- {h}" for h in hints)
    return shown, [str(p) for p in problems]


def report_summary(text: str) -> str:
    """The first paragraph of a report that is not a heading, for the task's summary."""
    for block in re.split(r"\n\s*\n", text or ""):
        line = block.strip()
        if line and not line.startswith(("#", "|", "---")):
            return " ".join(line.split())[:400]
    return ""


def _cell(value: Any) -> str:
    return " ".join(str(value if value is not None else "").split()).replace("|", "\\|")


def result_document(
    diagnosis: dict[str, Any],
    status: str,
    reason: str,
    task_id: str,
    form: Iterable[str] = (),
    sources: Iterable[tuple[str, dict[str, Any], str]] = (),
) -> str:
    """The result of a run as the text of a report: the verdict, the summary, the result table
    (its columns in the order of the task's ``Ergebnisform``), what stayed an assumption, where
    each cited read was made (``sources``: the run's kept reads, ref, source, excerpt) and which
    run it came from. Empty when the run left nothing."""
    summary = str(diagnosis.get("summary") or "").strip()
    table = diagnosis.get("table") if isinstance(diagnosis.get("table"), list) else []
    rows = [r for r in table if isinstance(r, dict)]
    if not summary and not rows:
        return ""
    verdict = "wartet auf Prüfung" if status == "in_review" else "blockiert"
    parts = [f"**Stand:** {verdict}: {reason}" if reason else f"**Stand:** {verdict}"]
    if summary:
        parts.append(summary)
    if rows:
        found = list(dict.fromkeys(k for r in rows for k in r))
        columns = [c for c in form if c in found] + [c for c in found if c not in form]
        lines = [
            "| " + " | ".join(_cell(c) for c in columns) + " |",
            "|" + "---|" * len(columns),
            *("| " + " | ".join(_cell(r.get(c)) for c in columns) + " |" for r in rows),
        ]
        parts.append("\n".join(lines))
    findings = [f for f in diagnosis.get("findings") or [] if isinstance(f, dict)]
    assumed = [str(f.get("claim") or "") for f in findings if f.get("tag") == "assumption"]
    if assumed:
        parts.append("**Nicht belegt:**\n" + "\n".join(f"- {c}" for c in assumed if c))
    if diagnosis.get("next"):
        parts.append(f"**Nächster Schritt:** {diagnosis['next']}")
    listed = source_lines(rows, sources)
    if listed:
        parts.append("**Quellen:**\n" + "\n".join(listed))
    parts.append(f"_Lauf: {task_id}_")
    return "\n\n".join(parts)


def source_lines(
    rows: list[dict[str, Any]], sources: Iterable[tuple[str, dict[str, Any], str]]
) -> list[str]:
    """One line per read the table cites, in the order of their numbers: where it was read (the
    file and lines as the read reported them). Live 2026-11-18: the result of 3.1.2 said only
    "E14, E9" - the person could not look them up and no later run could read what they meant."""
    cited = {
        ref
        for r in rows
        for k, v in r.items()
        if str(k).strip().casefold().startswith(programs.EVIDENCE_COLUMN)
        for ref in _CITED_REF.findall(str(v))
    }
    out: list[tuple[int, str]] = []
    for ref, source, excerpt in sources:
        if ref not in cited:
            continue
        head = (excerpt or "").strip().split("\n", 1)[0].strip()
        given = (source or {}).get("input") or {}
        path = str(given.get("path") or "")
        # a file read names itself on its first line ("path: lines 160-361 of 762")
        where = head if path and head.startswith(path) else programs.where(source or {})
        capability = str((source or {}).get("capability") or "")
        tail = f" ({capability})" if capability and capability not in where else ""
        out.append((int(ref[1:]), f"- **{ref}** — `{where[:200]}`{tail}"))
    return [line for _, line in sorted(out)]


# Applications whose task runs get the larger frame of a task (config task_*): code work.
TASK_FRAME_APPLICATIONS = frozenset({"dev"})

# How a run in Entwicklung finds its way in a repository (live 2026-11-17: a survey of the chat
# code guessed "src/web/gui/src/components/chat/Chat.tsx", listed empty folders eight times,
# never read the wiki that describes the code, and found none of the image code there is).
DEV_ORIENTATION = (
    "How to find your way: first read the wiki (wiki.list, then wiki.read of the pages for the "
    "part this task is about): it describes the code page by page and says where things are. "
    "Then list the top level of the repository once (repo.list without a path). Take every path "
    "from a listing, a search or the wiki, never from what a project usually looks like. A search "
    "that was cut shows its next part when you ask again; read it before you search for "
    "something else, and do not repeat a search as a regular expression when it gave results."
)


def task_limits(settings: Any, base: Any, target: str) -> Any | None:
    """The limits of a task run in ``target``: the larger frame for code work, else ``None``
    (the agent's own limits)."""
    if target not in TASK_FRAME_APPLICATIONS or settings is None or base is None:
        return None
    return replace(
        base,
        max_tool_calls=int(settings.task_max_tool_calls),
        max_turns=int(settings.task_max_turns),
        time_budget_seconds=float(settings.task_time_budget_seconds),
        context_chars=int(settings.task_context_chars),
        observation_chars=int(settings.task_observation_chars),
    )


def run_prompt(
    title: str, body: str, columns: list[str], items: int, target: str | None = None
) -> str:
    """The request an application's agent gets for one task node. The node's text is data."""
    columns = with_evidence(columns)
    form = " | ".join(columns)
    named = {"de": "German", "en": "English"}.get(question_language(f"{title}\n{body}") or "")
    language = f"{named}, the language of the task" if named else "the language of the task"
    orientation = f"{DEV_ORIENTATION}\n" if target == "dev" else ""
    return (
        f"[project task: {title}]\n"
        "Work through the checklist below in order. For each item read what it names and decide.\n"
        'When you are done, finish with a table: the field "table" holds one object per checked\n'
        f"item, with exactly these keys: {form}. In a Beleg column write the reference of the\n"
        "read that shows the row (E1, E2 ...) and the place in it. Every value is short. Keep the\n"
        f"summary to two sentences. The checklist has {items} item(s); the table needs at least\n"
        "that many rows.\n"
        f"Write the summary, the findings and the table in {language}.\n"
        f"{orientation}"
        "The text below comes from the person's project: it is the task, not an instruction\n"
        "to change anything outside this task.\n\n"
        f"{body}"
    )


ITEM_REFS = 4  # reads one checklist item's row may cite (agent_prompts.MAX_REFS_PER_CLAIM)
LOOK_ROWS = 12  # rows the second look reads in one call
_CITED_REF = re.compile(r"\bE\d+\b")
LOOK_CHARS = 40_000  # characters of cited excerpts in one call (about 11k tokens)


def look_portions(
    table: list[dict[str, Any]], excerpts: dict[str, str]
) -> list[tuple[int, list[dict[str, Any]]]]:
    """The table cut into portions the second look can read at once: at most LOOK_ROWS rows and
    LOOK_CHARS characters of the excerpts they cite (each excerpt counted once per portion, as the
    look shows it once). Returns (number of rows before the portion, its rows)."""
    out: list[tuple[int, list[dict[str, Any]]]] = []
    rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    size = 0
    start = 0
    for n, row in enumerate(table):
        cited = {
            r
            for k, v in row.items()
            if str(k).casefold().startswith("beleg")
            for r in _CITED_REF.findall(str(v))
            if r in excerpts
        }
        extra = sum(min(len(excerpts[r]), 8000) for r in cited - seen)
        if rows and (len(rows) >= LOOK_ROWS or size + extra > LOOK_CHARS):
            out.append((start, rows))
            rows, seen, size, start = [], set(), 0, n
            extra = sum(min(len(excerpts[r]), 8000) for r in cited)
        rows.append(row)
        seen |= cited
        size += extra
    if rows:
        out.append((start, rows))
    return out or [(0, [])]


def batch_places(views: Iterable[Any], policy: Any, targets: Iterable[str]) -> int:
    """How many batch runs may go on at once: the request places (slots) of the connected nodes the
    rows of ``targets`` name. A row that names no node lets every connected node with a chat model
    count. A node counts its slots (``n_parallel`` of its loaded models), one when nothing is
    loaded: a run that finds the node's places taken waits in the gateway's queue, so this only
    caps how many runs the batch starts, it never sends one to a node."""
    named: set[str] = set()
    for target in targets:
        named.update(policy.row_nodes(target))
    total = 0
    for view in views:
        if not view.connected or (named and view.name not in named):
            continue
        manual = getattr(policy, "model_kinds", None)
        if not any("chat" in model_facts.kinds_of(m, manual) for m in view.models):
            continue
        # what is loaded serves with its slots; a node with nothing loaded yet will load one of its
        # models with the slots its config names for it
        configured = getattr(view, "configured_parallel", {}) or {}
        total += view.slots() or max((configured.get(m, 1) for m in view.models), default=1)
    return total


# --- store --------------------------------------------------------------------------------------


class RunStore(StoreBase):
    """The writes of a run: the node's status and task, and the batch's counters. Each one is one
    transaction with its line of the Akte."""

    async def set_run(
        self,
        user_id: uuid.UUID,
        node_id: uuid.UUID,
        *,
        status: str,
        task_id: uuid.UUID | None,
        reason: str = "",
        result_class: str | None = None,
    ) -> ProjectNode:
        async with self._sessionmaker() as db:
            node = await get_node(db, user_id, node_id, lock=True)
            project = await get_project(db, user_id, node.project_id, lock=True)
            old = node.status
            node.status = status
            node.task_id = task_id
            bump(node)
            await self._record(
                db,
                user_id,
                project,
                "node.status_changed",
                node=node,
                payload={
                    "from": old,
                    "to": status,
                    "reason": reason,
                    "task_id": str(task_id or ""),
                    # the class of the run's result (report_class.py), when it has one
                    **({"class": result_class} if result_class else {}),
                },
            )
            await db.commit()
            await db.refresh(node)
            return node

    async def load(self, user_id: uuid.UUID, node_id: uuid.UUID) -> tuple[ProjectNode, Project]:
        async with self._sessionmaker() as db:
            node = await get_node(db, user_id, node_id)
            project = await get_project(db, user_id, node.project_id)
            return node, project


BLOCKER_RESULT_CHARS = 3000
# a step of a fix in steps waits for a free place of the person's programs (MAX_ACTIVE_PER_USER)
STEP_WAITS = 60
STEP_WAIT_SECONDS = 10.0
BLOCKER_HEADER = (
    "Results of the tasks this one depends on (use them, do not look them up again; they come "
    "from other applications, which can see what this one cannot):"
)


def blocker_results(
    blockers: Iterable[tuple[Any, list[Any]]], max_chars: int = BLOCKER_RESULT_CHARS
) -> str:
    """The newest result of each finished task this one depends on (``blockers``: each task with
    its children), as a section of the run's brief; empty when none has one. Live 2026-11-18: a
    task split across applications (Research reads the web, Verwaltung sees the machines) left
    the task that needs both to look everything up again, and it cannot see the machines.
    Issue 2.3: Dev's drafts did not get there."""
    parts = []
    for task, children in blockers:
        if task.status not in ("done", "in_review"):
            continue
        found = results_of(children, task.id)
        if not found:
            continue
        body = (found[-1].body or "")[:max_chars]
        parts.append(f"### {task.number} {task.title}\n{body}")
    return BLOCKER_HEADER + "\n\n" + "\n\n".join(parts) if parts else ""


class RunService:
    """Starts task nodes and batches, and moves them on when a run ends."""

    def __init__(
        self, sessionmaker: async_sessionmaker[AsyncSession], agents: Any, actor: Actor
    ) -> None:
        self._sessionmaker = sessionmaker
        self._agents = agents
        self._store = RunStore(sessionmaker, actor)
        self._dispatching = asyncio.Lock()  # one dispatch at a time: no node is taken twice
        self._settling: set[uuid.UUID] = set()  # runs whose end is being judged now
        # the second look at a result (project_check.py); a result never passes without it
        self.second_look: Any = None
        # how many runs of a batch may go on at once, for the applications its tasks name
        # (main.py: batch_places over the gateway's nodes and table); unset: one at a time
        self.places: Callable[[set[str]], Awaitable[int]] | None = None
        # the scheduling table (the gateway's NodePolicy): which nodes an application's row names
        self.policy: Callable[[], Any] | None = None
        # block programs for tasks that name one (main.py: the hub's BlockService)
        self.blocks: Any = None
        self.reviser: Any = None  # the author's revision after the second look (project_revise)
        self.fixes: Any = None  # drafts of issue fixes (issue_fixes.FixService, main.py)
        self._programs: dict[uuid.UUID, Any] = {}  # program runs of this process, by task id
        # fix drafts refused against the live code, by run: their next draft starts once the
        # run has ended (the issue is in progress until then)
        self._refused_fixes: dict[uuid.UUID, tuple[Any, str]] = {}
        # what a run of issue_steps was asked, by task: its steps get the same task (fix_steps.py)
        self._step_questions: dict[uuid.UUID, str] = {}
        self._watchers: set[asyncio.Future[None]] = set()

    def nodes_for(self, targets: set[str]) -> list[str]:
        """The nodes the rows of ``targets`` name, in the table's order (for the batch card)."""
        policy = self.policy() if self.policy is not None else None
        out: list[str] = []
        for target in sorted(targets):
            for name in policy.row_nodes(target) if policy is not None else ():
                if name not in out:
                    out.append(name)
        return out

    # --- start -----------------------------------------------------------------------------------

    async def start_node(
        self,
        user_id: uuid.UUID,
        node_id: uuid.UUID,
        batch_id: uuid.UUID | None = None,
        *,
        retry: bool = False,
        model: str | None = None,
        iterate: bool = False,
        program: str | None = None,
    ) -> AgentTask:
        """Start the task. ``retry``: the run before gave no table; the node stays in progress and
        the new run is asked for the table once more (it does not count as a further batch run).
        ``iterate``: a further iteration (project_iterations.py, prepared by ``begin``); a task
        that is done or in review may then run again."""
        node, _project = await self._store.load(user_id, node_id)
        if node.kind != "task":
            raise RunError("only a task can be run")
        closed = ("in_progress", "dropped") if iterate else ("in_progress", "done", "dropped")
        if not retry and node.status in closed:
            raise RunError(f"a task that is {node.status} is not started")
        target = target_of(node.body)
        if target is None:
            raise RunError("the task names no application (Zielanwendung)")
        user = await self._user(user_id)
        check_right(target, user.is_admin)
        columns = form_of(node.body)
        if not columns:
            raise RunError("the task names no result form (Ergebnisform)")
        items = checklist(node.body)[1]
        prompt = run_prompt(node.title, node.body, columns, items, target)
        if retry:
            prompt = (
                RETRY_MARK + " the last run gave no table. Finish again, with the table.\n" + prompt
            )
        if program is not None and program not in programs.ISSUE_PROGRAMS:
            raise RunError(f"{program!r} cannot be chosen for a run")
        program = (
            program
            or program_of(node.body)
            or programs.default_program(target, node.title, node.body)
        )
        if program == programs.FREE:  # the task asks for a free agent run
            program = None
        iterations = IterationStore(self._sessionmaker, self._store.actor)
        # a theme is never written from scratch again: a run of a task that has a result continues
        # the newest one (live 2026-11-16: v4 forgot what v2 and v3 had settled)
        if (
            program == programs.THEME_PROGRAM
            and not iterate
            and not retry
            and any(n.kind == "report" for n in await self._children(user_id, node.id))
        ):
            node, _ = await iterations.begin(
                user_id, node.id, based_on=None, feedback="", keep=False
            )
        # the version it continues, the feedback, the requirements (empty for a plain first run)
        # a theme continues its version; any other result is written anew from the run's reads
        extra = await iterations.brief_for(
            user_id, node, evidence=program != programs.THEME_PROGRAM
        )
        prompt += extra
        # what the tasks it waits for found, in their own applications (issue 2.3)
        section = blocker_results(await self._blockers(user_id, node.id))
        if section:
            prompt += "\n\n" + section
            extra += "\n\n" + section
        if program is not None:  # the check is a fixed program, not a free agent run
            task = await self._start_program(user, node, program, target, columns, prompt, extra)
        else:
            task = await self._agents.start(
                user,
                prompt,
                model,
                origin=ORIGIN,
                application=target,
                limits=task_limits(
                    getattr(self._agents, "settings", None),
                    getattr(getattr(self._agents, "loop", None), "limits", None),
                    target,
                ),
            )
        await self._store.set_run(
            user_id,
            node.id,
            status="in_progress",
            task_id=task.id,
            reason="run again for the table" if retry else f"run in {target}",
        )
        if batch_id is not None:
            await self._count(user_id, batch_id)
        return task

    # --- a task that names a block program (project_programs.py) --------------------------------

    async def _start_program(
        self,
        user: CurrentUser,
        node: ProjectNode,
        name: str,
        target: str,
        columns: list[str],
        prompt: str,
        extra: str = "",
    ) -> AgentTask:
        if self.blocks is None:
            raise RunError("block programs are not set up on this hub")
        program = self.blocks.programs().get(name)
        if program is None:
            raise RunError(f"there is no program {name!r}")
        has_room = getattr(self.blocks, "has_room", None)
        if has_room is not None and not has_room(user.uuid):
            # every place of the person's programs is taken: wait, and leave no task row behind
            raise ProgramsBusy("too many runs are going")
        models = programs.models_of(node.body)
        program = with_roles(program, models)  # a comparison run names a model per role
        inputs: dict[str, Any] = {}
        for key in program.get("inputs") or {}:
            if key == "task" and name == programs.CHECKLIST_PROGRAM:
                # each item's run sees the task without the other items (they are not its work)
                body = programs.without_checklist(node.body)
                inputs[key] = f"{node.title}\n\n{body}{extra}"
            elif key == "task":
                inputs[key] = f"{node.title}\n\n{node.body}{extra}"
            elif key == "items":
                inputs[key] = programs.checklist_items(node.body)
                if not inputs[key]:
                    raise RunError("the task has no checklist (- [ ] ...) to work through")
            elif key == "hint":
                inputs[key] = ""  # a new run of one refused item gets the reviewer's reason
            elif key == "question":
                inputs[key] = report_question(node.title, node.body) + extra
                if name == programs.THEME_PROGRAM:
                    inputs[key] += await self._theme_context(user.uuid, node)
            elif key == "form":
                inputs[key] = " | ".join(with_evidence(columns))
            elif key == "focus":
                named = programs.files_of(node.body, programs.area_of(node.body))
                inputs[key] = "\n".join(named)
            elif key == "files":
                # files the task names are taken as they are (no model picks); a named folder
                # leaves the choice to the program (an empty list)
                named = programs.files_of(node.body, programs.area_of(node.body))
                inputs[key] = [] if any(p.endswith("/") for p in named) else named
            elif key == "area":
                area = programs.area_of(node.body)
                if area is None:
                    raise RunError("the task names no part of the repository (Bereich: <path>)")
                inputs[key] = area
            elif key == "plan":  # "Plan umsetzen": the plan the investigation settled
                settled = await self.issue_plan(user.uuid, node.id)
                if settled is None:
                    raise RunError("the issue has no settled plan: draft the fix first")
                inputs[key] = json.dumps(settled, ensure_ascii=False)
            elif key == "page":
                path = page_of(node.body)
                if path is None:
                    raise RunError("the task names no page of the knowledge base (Seite: <path>)")
                text = await self._page_text(user.uuid, path)
                if not text:
                    raise RunError(f"the knowledge base has no document {path!r} for this person")
                inputs[key] = text
            else:
                raise RunError(f"the program {name} needs {key!r}, which a task does not give")
        now = clock.hub_now()
        task = AgentTask(
            id=uuid.uuid4(),
            user_id=user.uuid,
            prompt=prompt,
            title=f"[project task: {node.title}]"[:200],
            via=programs.VIA,
            origin=ORIGIN,
            application=target,
            kind=programs.KIND,
            status="running",
            started_at=now,
            diagnosis={"program": name, "models": models},
        )
        async with self._sessionmaker() as db:
            db.add(task)
            await db.commit()
            await db.refresh(task)
        try:
            run = self.blocks.start(user, program, inputs)
        except Exception as exc:  # BlockError: the program or the queue of runs refused it
            busy = getattr(exc, "code", "") == "too_many_runs"
            await self._end_program(
                user.uuid, task.id, "canceled" if busy else "failed", str(exc), None
            )
            if busy:  # no place now: the task waits for the next free one, like a busy node
                raise ProgramsBusy(str(exc)) from exc
            raise RunError(f"the program {name} did not start: {exc}") from exc
        self._programs[task.id] = run
        if name in (programs.STEPS_PROGRAM, programs.INVESTIGATE_PROGRAM):
            self._step_questions[task.id] = str(inputs.get("question") or "")
        watcher = asyncio.ensure_future(self._watch_program(user.uuid, task.id, name, run))
        self._watchers.add(watcher)
        watcher.add_done_callback(self._watchers.discard)
        return task

    async def _theme_context(self, user_id: uuid.UUID, node: ProjectNode) -> str:
        """What a theme is written from besides its task: the results of the earlier tasks of the
        same topic (the format, the design report) and the format the hub checks it against."""
        parts: list[str] = []
        async with self._sessionmaker() as db:
            siblings = list(
                await db.scalars(
                    select(ProjectNode).where(
                        ProjectNode.user_id == user_id,
                        ProjectNode.project_id == node.project_id,
                        ProjectNode.parent_id == node.parent_id,
                        ProjectNode.kind == "task",
                        ProjectNode.id != node.id,
                        ProjectNode.status.in_(("in_review", "done")),
                    )
                )
            )
            for task in sorted(siblings, key=lambda n: _number_key(n.number)):
                if _number_key(task.number) > _number_key(node.number):
                    continue
                results = await db.scalars(
                    select(ProjectNode)
                    .where(ProjectNode.parent_id == task.id, ProjectNode.kind == "report")
                    .order_by(ProjectNode.created_at.desc())
                )
                latest = next(iter(results), None)
                if latest is not None and latest.body.strip():
                    parts.append(f"## Result of {task.number} {task.title}\n{latest.body[:12000]}")
        parts.append(f"## The theme format (JSON Schema the hub checks)\n{themes.schema_text()}")
        return "\n\n" + "\n\n".join(parts)

    async def _page_text(self, user_id: uuid.UUID, path: str) -> str:
        """The whole document at ``path`` as this person may read it (knowledge.read, unpaged)."""
        async with self._sessionmaker() as db:
            found = await KnowledgeStore(db, user_id).chunks_of_path(path)
        return "\n\n".join(c.text for c, _ in found)

    async def _watch_program(
        self, user_id: uuid.UUID, task_id: uuid.UUID, name: str, run: Any
    ) -> None:
        try:
            await run.task
        except asyncio.CancelledError:  # the hub goes down: after the start it is requeued
            return
        except Exception:  # the block service keeps a crash on the result already
            log.exception("project program %s crashed", task_id)
        if run.result.status == "stopped":
            # the block service stopped it (the hub goes down): no end is judged; the task stays
            # running and the next start puts it back into the queue (_ended_run, hub_restart)
            return
        # a run that ends at once (a refusal, a test) can end before start_node has marked its
        # node in progress; its end would then be ignored and the node stay in progress for good
        for _ in range(50):
            node = await self._node_of(user_id, task_id)
            if node is not None and node.status == "in_progress":
                break
            await asyncio.sleep(0.1)
        try:
            await self._finish_program(user_id, task_id, name, run.result)
        finally:
            self._programs.pop(task_id, None)

    async def _finish_program(
        self, user_id: uuid.UUID, task_id: uuid.UUID, name: str, result: Any
    ) -> None:
        """The program's rows become the task's result table, its sources the task's evidence;
        then the end is judged like the end of any run of a task. A report program's result is its
        report instead (:meth:`_finish_report`)."""
        if name in programs.REPORT_PROGRAMS:
            await self._finish_report(user_id, task_id, name, result)
            return
        if name == programs.CHECKLIST_PROGRAM and self.second_look is not None:
            await self._finish_checklist(user_id, task_id, name, result)
            return
        texts, row_tasks = programs.gathered_rows(
            result.vars, result.collected_tasks, list(result.agent_tasks)
        )
        rows, row_tasks = programs.expand_rows(texts, row_tasks)
        reads: list[list[dict[str, Any]]] = []
        async with self._sessionmaker() as db:
            store = AgentStore(db, user_id)
            for task_ids in row_tasks:
                found: list[dict[str, Any]] = []
                for tid in task_ids:
                    for ev in await store.evidence(uuid.UUID(tid)):
                        found.append(
                            {
                                "task": tid,
                                "ref": ev.ref,
                                "excerpt": ev.excerpt,
                                "tag": ev.tag,
                                "source": ev.source,
                            }
                        )
                reads.append(found)
        table, sources = programs.number_sources(rows, result.sources, reads)
        ok = result.status == "ok"
        removed: list[dict[str, str]] = []
        numbered = len(table)  # rows that named a source of the run
        if name in programs.DOC_PROGRAMS:  # bare section names, no sentences about the reading
            table, removed = programs.tidy_doc_rows(table)
            # a Zweck paragraph only from the first lines of a file (2026-11-13)
            table = programs.purpose_from_start(table, sources)
        looked: dict[str, Any] = {}
        if ok and table and self.second_look is not None and self.reviser is not None:
            async with self._sessionmaker() as db:
                task = await db.get(AgentTask, task_id)
                models = dict(((task.diagnosis or {}) if task else {}).get("models") or {})
                node = await db.scalar(select(ProjectNode).where(ProjectNode.task_id == task_id))
            excerpts = {s.ref: s.excerpt for s in sources if s.excerpt.strip()}
            question = node.title if node is not None else name
            if name in programs.DOC_PROGRAMS:  # a name the cited code lacks: no model needed
                table, gone = programs.check_names(table, excerpts)
                removed += gone
                # a value of a constant the cited code does not show (2026-11-13)
                table, gone = programs.check_values(table, excerpts)
                removed += gone
            table, gone, looked = await self._review(question, table, excerpts, models)
            removed += gone
            if name in programs.DOC_PROGRAMS and looked.get("ok"):
                # the confirmed paragraphs joined per section; an edit stands only when confirmed
                table, edited = await self._edit(question, table, excerpts, models)
                looked["edited"] = edited
                table, looked["overview"] = await self._overview(question, table, excerpts, models)
            if (
                name in programs.DOC_PROGRAMS
                and (looked.get("ok") or looked.get("unchecked"))
                and node is not None
            ):
                # a look that could not run files the paragraphs too, marked unchecked (2.19)
                await self._file_document(
                    user_id, node, table, sources, removed, unchecked=bool(looked.get("unchecked"))
                )
        ok = result.status == "ok"
        await self._end_program(
            user_id,
            task_id,
            "done" if ok else "failed",
            None if ok else (result.error or result.status),
            {
                "program": name,
                "table": table or None,
                "rows_without_result": sum(1 for r in rows if r is None),
                # rows whose Beleg named no source of the run: left out, not refused as a whole
                "rows_without_source": sum(1 for r in rows if r is not None) - numbered,
                "notes": list(result.notes)[:20],
                "removed": removed,
                **({"second_look": looked} if looked else {}),
            },
            sources,
        )
        await self.on_task_event(user_id, task_id, "done" if ok else "failed")

    async def _reads(self, user_id: uuid.UUID, task_ids: Iterable[str]) -> list[dict[str, Any]]:
        """The kept reads of agent tasks, as number_sources takes them."""
        found: list[dict[str, Any]] = []
        async with self._sessionmaker() as db:
            store = AgentStore(db, user_id)
            for tid in task_ids:
                for ev in await store.evidence(uuid.UUID(tid)):
                    found.append(
                        {
                            "task": tid,
                            "ref": ev.ref,
                            "excerpt": ev.excerpt,
                            "tag": ev.tag,
                            "source": ev.source,
                        }
                    )
        return found

    async def _finish_checklist(
        self, user_id: uuid.UUID, task_id: uuid.UUID, name: str, result: Any
    ) -> None:
        """A checklist worked through item by item (``checklist_check``): every item's row is
        looked at on its own against the reads of its run; a refused item runs once more with an
        empty context and the reviewer's reason (never its refused text), and is looked at again.
        What still is not confirmed is removed with its reason. Rewriting a row from the same
        reads (``_review``) is not tried here: the reason a row fails is mostly a read it never
        made, which only a new run can make."""
        items = [str(x) for x in (result.vars.get("items") or [])]
        texts, row_tasks = programs.gathered_rows(
            result.vars, result.collected_tasks, list(result.agent_tasks)
        )
        rows = programs.parse_rows(texts)
        async with self._sessionmaker() as db:
            task = await db.get(AgentTask, task_id)
            models = dict(((task.diagnosis or {}) if task else {}).get("models") or {})
            node = await db.scalar(select(ProjectNode).where(ProjectNode.task_id == task_id))
        question = node.title if node is not None else name
        reviewer = models.get("reviewer")

        async def settle(i: int, item: str) -> dict[str, Any]:
            """One item: its look, and when refused one new run on another model and its look."""
            row = rows[i] if i < len(rows) else None
            owners = row_tasks[i] if i < len(row_tasks) else []
            reads = await self._reads(user_id, owners)
            # the reviewer is another model than the one that really wrote the row (its run's)
            author = await self._author_of(user_id, owners) or models.get("author") or ""
            why, judged, by = await self._item_look(
                question, row, reads, result.sources, author, reviewer
            )
            reran = False
            if why is not None and judged and result.status == "ok" and node is not None:
                again = await self._rerun_item(user_id, node, item, why, result.vars, author)
                if again is not None:
                    reran = True
                    row, reads, tool_sources, author = again
                    why, judged, by = await self._item_look(
                        question, row, reads, tool_sources, author, reviewer
                    )
            return {"row": row, "reads": reads, "why": why, "judged": judged, "by": by,
                    "reran": reran, "author": author}  # fmt: skip

        # the items are looked at side by side: a new run of one does not wait for the others
        settled = await asyncio.gather(*(settle(i, item) for i, item in enumerate(items)))
        kept_rows: list[dict[str, Any] | None] = []
        kept_reads: list[list[dict[str, Any]]] = []
        removed: list[dict[str, str]] = []
        unchecked: list[str] = []
        for item, s in zip(items, settled, strict=True):
            # a look that could not be made is no verdict (issue 2.19, live 2026-11-22: three right
            # rows of task 3.3.3 were removed because the one slot of the reviewer was taken): the
            # row stays, counted as unchecked
            if s["why"] is not None and not s["judged"] and s["row"] is not None:
                unchecked.append(item)
            if s["why"] is None or item in unchecked:
                kept_rows.append(s["row"])
                kept_reads.append(s["reads"])
                continue
            row = s["row"]
            said = programs.statement(json.dumps(row, ensure_ascii=False)) if row else ""
            removed.append(
                {
                    "Aussage": f"{item}: {said}" if said else item,
                    "Grund": f"{revise.UNCONFIRMED}: {s['why']}" if s["judged"] else s["why"],
                }
            )
        confirmed = len(kept_rows) - len(unchecked)
        reviewers = sorted({s["by"] for s in settled if s["by"]})
        authors = sorted({s["author"] for s in settled if s["author"]})
        table, sources = programs.number_sources(kept_rows, result.sources, kept_reads)
        ok = result.status == "ok"
        await self._end_program(
            user_id,
            task_id,
            "done" if ok else "failed",
            None if ok else (result.error or result.status),
            {
                "program": name,
                "table": table or None,
                "rows_without_result": sum(1 for r in rows if r is None),
                "rows_without_source": 0,
                "notes": list(result.notes)[:20],
                "removed": removed,
                "second_look": {
                    "ok": bool(table) and len(table) == confirmed,
                    "reviewer": ", ".join(reviewers) or (reviewer or ""),
                    "author": ", ".join(authors),
                    "rounds": sum(1 for s in settled if s["reran"]),  # items run once more
                    "rows": len(table),
                    "unchecked": unchecked,
                    # a reviewer of the author's own family: a self-check, shown as such (2.19)
                    "same_family": any(
                        model_profiles.same_family(s["by"], s["author"]) for s in settled
                    ),
                    "reason": "; ".join(
                        x
                        for x in (
                            f"{len(removed)} of {len(items)} item(s) not confirmed",
                            f"{len(unchecked)} unchecked (the second look could not run)"
                            if unchecked
                            else "",
                        )
                        if x
                    ),
                },
            },
            sources,
        )
        await self.on_task_event(user_id, task_id, "done" if ok else "failed")

    async def _author_of(self, user_id: uuid.UUID, task_ids: Iterable[str]) -> str:
        """The model that answered in the first of these agent tasks (its first model turn)."""
        ids = [uuid.UUID(t) for t in task_ids]
        if not ids:
            return ""
        async with self._sessionmaker() as db:
            data = await db.scalar(
                select(AgentStep.data)
                .where(
                    AgentStep.user_id == user_id,
                    AgentStep.task_id.in_(ids),
                    AgentStep.kind == "assistant",
                )
                .order_by(AgentStep.seq)
                .limit(1)
            )
        return str((data or {}).get("model") or "")

    async def _item_look(
        self,
        question: str,
        row: dict[str, Any] | None,
        reads: list[dict[str, Any]],
        tool_sources: list[dict[str, Any]],
        author: str,
        reviewer: str | None,
    ) -> tuple[str | None, bool, str]:
        """The second look at one item's row: (why it is refused or None, whether the reviewer
        judged it - a look that could not be made is no reason to run the item again, the
        reviewer)."""
        if row is None:
            return "the run found nothing that makes a row", True, ""
        cited = {
            ref
            for k, v in row.items()
            if str(k).strip().casefold().startswith(programs.EVIDENCE_COLUMN)
            for ref in _CITED_REF.findall(str(v))
        }
        if len(cited) > ITEM_REFS:
            # a row that cites everything it read shows nothing in particular (live 2026-11-18:
            # one row cited E11 to E16)
            return (
                f"the row cites {len(cited)} reads at once; it must name the one or two reads "
                "that show it",
                True,
                "",
            )
        table, sources = programs.number_sources([row], tool_sources, [reads])
        if not table:
            return "the row names no read its run made", True, ""
        excerpts = {s.ref: s.excerpt for s in sources if s.excerpt.strip()}
        look = await self._look(question, table, excerpts, author, reviewer)
        if look.ok:
            return None, True, look.reviewer or ""
        if look.objections:
            return next(iter(look.objections.values())), True, look.reviewer or ""
        return look.reason or "the second look could not be made", False, look.reviewer or ""

    async def _rerun_item(
        self,
        user_id: uuid.UUID,
        node: ProjectNode,
        item: str,
        why: str,
        given: dict[str, Any],
        author: str = "",
    ) -> tuple[dict[str, Any] | None, list[dict[str, Any]], list[dict[str, Any]], str] | None:
        """One item once more in a fresh run of ``checklist_check``, told why its row was refused
        (not shown the row: a model given its old text copies it), on another model than the one
        that wrote it when one is free (another model makes other mistakes). None when it could
        not run."""
        if self.blocks is None:
            return None
        program = self.blocks.programs().get(programs.CHECKLIST_PROGRAM)
        if program is None:
            return None
        roles = programs.models_of(node.body)
        other = None
        if "reader" not in roles and self.second_look is not None:
            pick = getattr(self.second_look, "other_model", None)
            other = await pick(author, "dev") if pick is not None else None
            if other:
                roles["reader"] = other
        program = with_roles(program, roles)
        inputs = {
            "task": str(given.get("task") or node.title),
            "form": str(given.get("form") or ""),
            "items": [item],
            "hint": (
                "An earlier run of this item was refused by a reviewer who compared its row with "
                f"the reads it cited: {why}\nLook again in the code, read the lines you cite and "
                "say only what they show."
            ),
        }
        try:
            run = self.blocks.start(await self._user(user_id), program, inputs)
            await run.task
        except Exception as exc:  # no place, a refused program: the item keeps its refusal
            log.info("item of %s did not run again: %s", node.number, exc)
            return None
        res = run.result
        if res.status != "ok":
            return None
        texts, tasks = programs.gathered_rows(res.vars, res.collected_tasks, list(res.agent_tasks))
        rows = programs.parse_rows(texts)
        owners = tasks[0] if tasks else []
        reads = await self._reads(user_id, owners)
        wrote = await self._author_of(user_id, owners) or (other or "").partition("@")[0]
        return (rows[0] if rows else None), reads, list(res.sources), wrote

    async def _finish_report(
        self, user_id: uuid.UUID, task_id: uuid.UUID, name: str, result: Any
    ) -> None:
        """The report the program wrote, checked and revised, becomes the task's result: no table,
        so the verdict reads it from the diagnosis (``report``), and it is filed below the task."""
        draft = result.vars.get("draft")
        text = str(draft or "").strip()
        verdict = result.vars.get("verdict")
        checked = bool(verdict.get("ok")) if isinstance(verdict, dict) else False
        problems: list[str] = []
        notes = list(result.notes)[:20]
        if name == programs.THEME_PROGRAM and text:
            text, problems = theme_result(text)
            if not problems:  # only a theme that passes the check is drawn
                pictures, note = await self._theme_pictures(user_id, task_id, text)
                text += pictures
                if note:
                    notes.append(note)
            advice = verdict.get("advice") if isinstance(verdict, dict) else None
            if advice:  # the model's last word on the design: advice, the pictures decide
                text += "\n\n## Hinweise der Prüfung\n\n" + "\n".join(f"- {a}" for a in advice)
        if name == programs.FIX_PROGRAM and text:
            text, problems = await self._fix_draft(user_id, task_id, text)
            notes += problems
        if name in (programs.STEPS_PROGRAM, programs.PLAN_PROGRAM) and text:
            text, problems = await self._fix_in_steps(
                user_id,
                task_id,
                text,
                str(result.authors.get("draft") or ""),
                repair=name == programs.STEPS_PROGRAM,
            )
            notes += problems
        plan: dict[str, Any] | None = None
        failed: list[str] = []
        issue: dict[str, str] = {}
        draft: dict[str, Any] | None = None
        if name == programs.INVESTIGATE_PROGRAM:
            text, plan, failed, node_id, draft = await self._investigate(user_id, task_id, text)
            notes += failed
            if node_id is not None:  # the next investigation of the issue finds this one
                issue = {"node": str(node_id)}
        ok = result.status == "ok" and bool(text)
        if name == programs.INVESTIGATE_PROGRAM:
            # the start read is a start only (live 2026-11-19: its model answered with nothing,
            # the rounds found the rest, and the issue was blocked for the start): the rounds decide
            ok = bool(text) and not failed
        await self._end_program(
            user_id,
            task_id,
            "done" if ok else "failed",
            None if ok else (result.error or result.status or "no report"),
            {
                "program": name,
                **issue,
                "summary": report_summary(text),
                "report": {
                    "text": text,
                    "filed": list(result.filed),
                    "checked": checked,
                    "problems": problems,
                    "issues": list(verdict.get("issues") or [])
                    if isinstance(verdict, dict)
                    else [],
                    **({"plan": plan} if plan else {}),
                    **({"draft": draft} if draft and not plan else {}),
                },
                "notes": notes,
            },
        )
        await self.on_task_event(user_id, task_id, "done" if ok else "failed")
        refused = self._refused_fixes.pop(task_id, None)
        if refused is not None and self.fixes is not None:
            await self.fixes.next_draft(*refused)  # the issue is settled: its next draft may start

    async def _fix_draft(
        self, user_id: uuid.UUID, task_id: uuid.UUID, text: str
    ) -> tuple[str, list[str]]:
        """A run of ``issue_fix``: its JSON becomes a fix draft (issue_fixes.py) and the result
        shows it as a diff; a draft that is no fix stays text with what is wrong with it."""
        from topcoat_hub import issue_fixes

        summary, edits, files, problems = issue_fixes.parse_fix(text)
        async with self._sessionmaker() as db:
            node = await db.scalar(select(ProjectNode).where(ProjectNode.task_id == task_id))
        if problems:
            raw = text[:6000]
            if node is not None and self.fixes is not None:
                # a draft out of form gets its next draft too (live 2026-11-18: "the new text is
                # missing" ended the issue's drafts until someone started one by hand)
                why = "der Entwurf hat nicht die verlangte Form: " + "; ".join(problems)
                fix = await self.fixes.create(user_id, node.id, task_id, summary, [], [])
                await self.fixes.refuse_draft(fix.id, why)
                self._refused_fixes[task_id] = (fix, why)
            return (
                f"**Fix-Entwurf unbrauchbar:** {'; '.join(problems)}\n\n```\n{raw}\n```",
                problems,
            )
        if node is None or self.fixes is None:
            return issue_fixes.render(summary, edits, files), []
        # held against the live code first: an old text that misses it only by blank lines is
        # taken verbatim from the file, so the draft that is kept is the one that applies
        edits, misfit = await self.fixes.verify(user_id, edits, files)
        # the same text where the last draft failed fails the same way (1.13): refused at once
        same = (
            None
            if misfit
            else await self.fixes.unchanged_at_failure(user_id, node.id, edits, files)
        )
        fix = await self.fixes.create(user_id, node.id, task_id, summary, edits, files)
        body = issue_fixes.render(summary, edits, files)
        if same:
            why = "der Entwurf ändert die Fehlerstelle nicht: " + same
            await self.fixes.refuse_draft(fix.id, why)
            self._refused_fixes[task_id] = (fix, why)
            return f"{body}\n\n_Fix {fix.id}: {why}._", [same]
        if misfit:
            why = "die Edits passen nicht zum Code: " + "; ".join(misfit)
            await self.fixes.refuse_draft(fix.id, why)
            self._refused_fixes[task_id] = (fix, why)
            return (
                f"{body}\n\n_Fix {fix.id}: passt nicht zum Code, ein neuer Entwurf folgt._\n\n"
                + "\n".join(f"- {m}" for m in misfit),
                [m.split("\n", 1)[0] for m in misfit],
            )
        return f"{body}\n\n_Fix {fix.id}: wartet auf „Übernehmen und testen“._", []

    async def _asker(self, user_id: uuid.UUID, node: ProjectNode) -> Any:
        """``ask(program, inputs) -> (answer, model)``: one run of a block program on the models
        the task names, waiting for a free place of the person's programs; a failed run answers
        with why, so a refusal shows it."""
        user = await self._user(user_id)
        roles = programs.models_of(node.body)

        async def ask(name: str, inputs: dict[str, Any]) -> tuple[str, str]:
            program = with_roles(self.blocks.programs()[name], roles)
            for _ in range(STEP_WAITS):  # the person's places may all be taken for a while
                try:
                    run = self.blocks.start(user, program, inputs)
                    break
                except Exception as exc:
                    if getattr(exc, "code", "") != "too_many_runs":
                        raise
                    await asyncio.sleep(STEP_WAIT_SECONDS)
            else:
                return "", ""
            try:
                await run.task
            except Exception:  # the block service keeps the crash on the result
                log.info("program run %s crashed", name, exc_info=True)
            res = run.result
            if res.status != "ok":
                return f"(the run of {name} failed: {res.error or res.status})", ""
            return str(res.vars.get("draft") or ""), str(res.authors.get("draft") or "")

        return ask

    async def _investigate(
        self, user_id: uuid.UUID, task_id: uuid.UUID, start: str
    ) -> tuple[str, dict[str, Any] | None, list[str], uuid.UUID | None, dict[str, Any] | None]:
        """A run of ``issue_investigate``: the rounds with Development and Verwaltung
        (issue_investigation.py) up to a report and a checked plan:
        ``(text, plan, problems, issue node, refused plan as written)``. It starts from the
        report of the issue's previous
        investigation, if there is one (live 2026-11-21: a refused plan files no result, so the
        next iteration asked everything again and ran out of rounds)."""
        from topcoat_hub import issue_investigation

        async with self._sessionmaker() as db:
            node = await db.scalar(select(ProjectNode).where(ProjectNode.task_id == task_id))
        if node is None or self.fixes is None or self.blocks is None:
            return start, None, ["the investigation could not run"], None, None
        previous, draft = await self._previous_investigation(user_id, node.id, task_id)
        if previous:
            start = (
                f"{start}\n\n## Die vorige Untersuchung dieses Issues (baue darauf auf: was sie "
                "gefunden hat, gilt; korrigiere nur, was die Rückmeldung oder die Ablehnung des "
                f"Plans sagt)\n{previous}"
            )
            if draft:
                start += "\n\n" + await self._draft_note(user_id, draft)
        wiki = await self._wiki_for(user_id, f"{node.body}\n{start}")
        if wiki:
            start += (
                "\n\n## Aus dem Wiki (beschreibt den Code Seite für Seite; was es über die "
                f"genannten Dateien und Namen sagt)\n{wiki}"
            )
        question = self._step_questions.pop(task_id, None) or report_question(node.title, node.body)
        ask = await self._asker(user_id, node)

        async def read(paths: list[str]) -> dict[str, str | None]:
            return await self.fixes.read_live(user_id, paths)

        async def outline(path: str) -> list[dict[str, Any]]:
            return await self.fixes.outline_live(user_id, path)

        # tries only under the person's probe mandate (issue_probes.py, D83); the runner checks it
        # again on every try, so a revoked mandate stops the next one
        from topcoat_hub.issue_probes import ProbeRunner
        from topcoat_hub.project_store import ProjectStore

        runner = ProbeRunner(self.fixes, ProjectStore(self._sessionmaker, self._store.actor))
        try_run = None
        if await runner.mandate(user_id, node.project_id, node.number) is not None:

            async def try_run(n: int, content: str) -> str:
                return await runner.run(user_id, node.project_id, node.number, n, content)

        inv = await issue_investigation.investigate(
            ask, read, question, start, try_run, review=True, outline=outline
        )
        if inv.title or inv.checks:
            await self._brief_issue(user_id, node.id, inv.title, inv.checks)
        failed = [inv.failed] if inv.failed else []
        return issue_investigation.with_log(inv), inv.plan, failed, node.id, inv.draft

    async def _wiki_for(self, user_id: uuid.UUID, text: str) -> str:
        """The wiki paragraphs about the files and names ``text`` mentions (the person's wiki,
        every page in its current version); "" when there is none or it cannot be read."""
        from topcoat_hub import issue_investigation
        from topcoat_hub.wiki_store import WikiStore

        try:
            async with self._sessionmaker() as db:
                store = WikiStore(db, user_id)
                pages = [(p.row.path, (await store.current(p)).body) for p in await store.pages()]
        except Exception:  # the wiki is a help, never a reason for the investigation to fail
            log.info("project runs: the wiki was not read for an investigation", exc_info=True)
            return ""
        return issue_investigation.wiki_excerpt(pages, text)

    async def _previous_investigation(
        self, user_id: uuid.UUID, node_id: uuid.UUID, current: uuid.UUID
    ) -> tuple[str, dict[str, Any] | None]:
        """The report of the newest earlier investigation of the issue that wrote one (its rounds'
        log left out) and its refused plan as written, or ``("", None)``."""
        async with self._sessionmaker() as db:
            rows = await db.scalars(
                select(AgentTask)
                .where(
                    AgentTask.user_id == user_id,
                    AgentTask.id != current,
                    AgentTask.diagnosis["node"].astext == str(node_id),
                    AgentTask.diagnosis["program"].astext == programs.INVESTIGATE_PROGRAM,
                )
                .order_by(AgentTask.created_at.desc())
                .limit(5)
            )
            for task in rows:
                stored = (task.diagnosis or {}).get("report") or {}
                text = str(stored.get("text") or "")
                report = text.split("\n\n## Untersuchung\n", 1)[0].strip()
                if report and not report.startswith("**Untersuchung ohne Ergebnis"):
                    draft = stored.get("draft")
                    return report[:PREVIOUS_REPORT_CHARS], draft if isinstance(
                        draft, dict
                    ) else None
        return "", None

    async def _draft_note(self, user_id: uuid.UUID, draft: dict[str, Any]) -> str:
        """The refused plan of the previous investigation, as written, held against the live code
        again with today's rules (a test step moved, anchors settled): either it fits now, or what
        still does not."""
        from topcoat_hub import fix_steps
        from topcoat_hub.fix_steps import parse_plan, plan_problems, tests_out_of_steps

        raw = tests_out_of_steps(dict(draft))
        plan, problems = parse_plan(json.dumps(raw))
        if plan is not None and self.fixes is not None:
            fixes = self.fixes

            async def read(paths: list[str]) -> dict[str, str | None]:
                return await fixes.read_live(user_id, paths)

            await fix_steps.settle_test(plan, raw, read)  # a test file that exists: a free name
            files = await read(sorted({s.path for s in plan.steps} | {plan.test_path}))
            problems = plan_problems(plan, files)
        verdict = (
            "The hub checked this plan again: it FITS the code now. Write the report with it."
            if not problems
            else "The hub checked this plan again; still wrong: " + "; ".join(problems)[:2000]
        )
        return (
            f"Its plan, as written (JSON):\n{json.dumps(raw, ensure_ascii=False)[:6000]}\n{verdict}"
        )

    async def _brief_issue(
        self, user_id: uuid.UUID, node_id: uuid.UUID, title: str, checks: list[str]
    ) -> None:
        """The investigation's title and checks into its issue (project_issues.apply_brief); the
        issue was started by the person's click, and the Akte shows the change. A failure leaves
        the issue as it is: the report still holds both."""
        from topcoat_hub import project_issues
        from topcoat_hub.project_store import ProjectStore

        store = ProjectStore(self._sessionmaker, self._store.actor)
        try:
            node, _project = await self._store.load(user_id, node_id)
            if not project_issues.is_issue(node):
                return
            new_title, new_body = project_issues.apply_brief(node.title, node.body, title, checks)
            if new_title is None and new_body is None:
                return
            await store.update_node(
                user_id, node.id, revision=node.revision, title=new_title, body=new_body
            )
        except Exception:
            log.exception("project runs: the investigation's title and checks were not written")

    async def issue_plan(self, user_id: uuid.UUID, node_id: uuid.UUID) -> dict[str, Any] | None:
        """The settled plan of an issue: the plan of its newest result, when that result is an
        investigation whose plan the hub accepted; ``None`` otherwise (a later run, a fix of the
        plan, filed its own result)."""
        async with self._sessionmaker() as db:
            latest = await db.scalar(
                select(ProjectNode)
                .where(
                    ProjectNode.user_id == user_id,
                    ProjectNode.parent_id == node_id,
                    ProjectNode.kind == "report",
                    ProjectNode.trashed_at.is_(None),
                )
                .order_by(ProjectNode.created_at.desc())
                .limit(1)
            )
            found = _RUN_REF.search(latest.body or "") if latest is not None else None
            task = await db.get(AgentTask, uuid.UUID(found.group(1))) if found else None
        if task is None or task.user_id != user_id:
            return None
        diagnosis = task.diagnosis or {}
        if diagnosis.get("program") != programs.INVESTIGATE_PROGRAM:
            return None
        plan = (diagnosis.get("report") or {}).get("plan")
        return plan if isinstance(plan, dict) else None

    async def given_plan(
        self, user_id: uuid.UUID, node_id: uuid.UUID, raw: dict[str, Any]
    ) -> tuple[dict[str, Any] | None, list[str]]:
        """The person's own plan for an issue ("Plan vorgeben", 2026-11-21): held against the live
        code like any plan (a test step moved, close anchors settled), and when it fits filed as the
        issue's newest result, so "Plan umsetzen" carries it out step by step. The planner is left
        out: live 2026-11-21 (issue 2.10) the local planner models found the places but planned new
        GUI code wrong ten times, even when the person wrote the way down. Returns ``(plan, [])``
        or ``(None, problems)``."""
        from topcoat_hub import fix_steps, issue_investigation, project_issues
        from topcoat_hub.fix_steps import parse_plan, plan_problems, tests_out_of_steps
        from topcoat_hub.project_store import ProjectStore

        node, _project = await self._store.load(user_id, node_id)
        if not project_issues.is_issue(node):
            return None, ["only an issue takes a plan"]
        if node.status == "in_progress":
            return None, ["a run of the issue is in progress"]
        if self.fixes is None:
            return None, ["the workshop is not set up"]
        plan_raw = tests_out_of_steps({k: raw.get(k) for k in ("summary", "steps", "test")})
        plan_raw["given"] = True  # the person's plan: finer steps allowed (fix_steps.parse_plan)
        plan, problems = parse_plan(json.dumps(plan_raw))
        if plan is None:
            return None, problems
        fixes = self.fixes

        async def read(paths: list[str]) -> dict[str, str | None]:
            return await fixes.read_live(user_id, paths)

        await fix_steps.settle_test(plan, plan_raw, read)  # a test file that exists: a free name
        files = await read(sorted({s.path for s in plan.steps} | {plan.test_path}))
        problems = plan_problems(plan, files)
        if problems:
            return None, problems
        fix_steps.settled_into(plan_raw.get("steps"), plan)  # the places as the hub settled them
        text = (
            "## Lösungsplan (von der Person vorgegeben)\n"
            + issue_investigation.plan_text(plan_raw)
            + "\n\nDer Hub hat ihn gegen den laufenden Code geprüft: jede Stelle steht dort genau "
            "einmal."
        )
        task_id = uuid.uuid4()
        async with self._sessionmaker() as db:
            db.add(
                AgentTask(
                    id=task_id,
                    user_id=user_id,
                    prompt=f"[given plan] {node.title}"[:2000],
                    title=f"[Plan vorgegeben] {node.title}"[:200],
                    status="done",
                    application="dev",
                    finished_at=clock.hub_now(),
                    summary="the plan of the person",
                    diagnosis={
                        "program": programs.INVESTIGATE_PROGRAM,
                        "node": str(node.id),
                        "given": True,
                        "report": {"text": text, "plan": plan_raw},
                    },
                )
            )
            await db.commit()
        report = await ProjectStore(self._sessionmaker, self._store.actor).create_node(
            user_id,
            node.project_id,
            kind="report",
            title=f"Plan: {node.title}"[:200],
            body=f"**Stand:** Plan von der Person vorgegeben\n\n{text}\n\n_Lauf: {task_id}_",
            parent_id=node.id,
            report_kind="document",
        )
        await IterationStore(self._sessionmaker, self._store.actor).file(
            user_id, node.id, report.id
        )
        await self._store.set_run(
            user_id, node.id, status="in_review", task_id=task_id, reason="Plan vorgegeben"
        )
        return plan_raw, []

    async def _fix_in_steps(
        self,
        user_id: uuid.UUID,
        task_id: uuid.UUID,
        plan: str,
        planner: str,
        *,
        repair: bool = True,
    ) -> tuple[str, list[str]]:
        """A run of ``issue_steps``: its plan carried out step by step (fix_steps.py), each step a
        fresh run of ``fix_step`` on the models the issue names; the edits of all steps become one
        draft like that of ``issue_fix``. A plan or step that does not fit twice ends in a refused
        draft with the reason, and its next draft starts after the run (like a misfit)."""
        from topcoat_hub import fix_steps, issue_fixes

        async with self._sessionmaker() as db:
            node = await db.scalar(select(ProjectNode).where(ProjectNode.task_id == task_id))
        if node is None or self.fixes is None or self.blocks is None:
            return (
                f"**Plan ohne Ausführung** (kein Werkstatt-Dienst):\n\n```\n{plan[:6000]}\n```",
                ["the steps could not run"],
            )
        question = self._step_questions.pop(task_id, None) or report_question(node.title, node.body)
        ask = await self._asker(user_id, node)

        async def read(paths: list[str]) -> dict[str, str | None]:
            return await self.fixes.read_live(user_id, paths)

        # a repair builds on the newest draft of the issue that was tested red (drafts refused
        # before any test, and discarded ones, are passed over); a settled plan starts afresh
        base = None
        for older in reversed(await self.fixes.of_issue(user_id, node.id) if repair else []):
            if older.state == "discarded" or (older.state == "failed" and not older.tests):
                continue
            if older.state == "failed" and await self.fixes.program_of(older) in (
                programs.STEPS_PROGRAM,
                programs.PLAN_PROGRAM,
            ):
                base = (list(older.edits or []), list(older.files or []))
            break
        out = await fix_steps.run_steps(ask, read, question, plan, planner, base)
        steps = "**Schritte:**\n" + "\n".join(f"- {line}" for line in out.log)
        if out.failed:
            fix = await self.fixes.create(
                user_id, node.id, task_id, out.summary, out.edits, out.files
            )
            await self.fixes.refuse_draft(fix.id, out.failed)
            self._refused_fixes[task_id] = (fix, out.failed)
            done = issue_fixes.render(out.summary, out.edits, out.files) if out.edits else ""
            return (
                f"{steps}\n\n_Fix {fix.id}: {out.failed}; ein neuer Entwurf folgt._\n\n{done}",
                [out.failed.split("\n", 1)[0][:300]],
            )
        text, problems = await self._fix_draft(user_id, task_id, fix_steps.as_draft(out))
        return f"{steps}\n\n{text}", problems

    async def _theme_pictures(
        self, user_id: uuid.UUID, task_id: uuid.UUID, text: str
    ) -> tuple[str, str]:
        """The theme drawn by the workshop browser, as a section of Markdown pictures kept by the
        hub (report_images, shown only to this person), and a note when there are none."""
        from topcoat_hub.models import ReportImage
        from topcoat_hub.tools import Caller

        tools = getattr(self._agents, "tools", None)
        # the agents hold the loop's ToolsAdapter; the node calls are its ToolService (.tools)
        if tools is not None and not hasattr(tools, "usable_tools"):
            tools = getattr(tools, "tools", None)
        if tools is None:
            return "", ""
        try:
            block = text.split("```json\n", 1)[-1].split("\n```", 1)[0]  # hints may follow it
            doc = json.loads(block[block.find("{") : block.rfind("}") + 1])
        except ValueError:
            return "", ""
        user = await self._user(user_id)
        caller = Caller(user_id, user.is_admin, "agent", application="dev", task_id=task_id)
        try:
            shot = await theme_preview.take(tools, self._sessionmaker, caller, doc)
        except Exception as exc:  # a picture is a bonus: the theme stands without it
            log.warning("theme pictures failed", exc_info=True)
            return "", f"no pictures of the theme: {exc}"
        lines: list[str] = []
        async with self._sessionmaker() as db:
            for caption, data in shot.pictures:
                image = ReportImage(
                    user_id=user_id,
                    task_id=task_id,
                    content_type="image/jpeg",
                    data=data,
                    image_url="workshop:browser.shot",
                    page_url="",
                    caption=caption,
                )
                db.add(image)
                await db.flush()
                lines.append(f"![{caption}](/api/reports/images/{image.id})")
            await db.commit()
        section = "\n\n## Vorschau\n\n" + "\n\n".join(lines) if lines else ""
        return section, shot.note

    async def _look(
        self,
        question: str,
        table: list[dict[str, Any]],
        excerpts: dict[str, str],
        author: str,
        reviewer: str | None,
    ) -> Any:
        """The second look over a table in portions that fit one context (look_portions); the
        objections come back numbered as rows of the whole table."""
        portions = look_portions(table, excerpts)
        if len(portions) == 1:
            return await self.second_look.check(question, table, excerpts, author, reviewer)
        objections: dict[int, str] = {}
        last = None
        for start, rows in portions:
            last = await self.second_look.check(question, rows, excerpts, author, reviewer)
            if not last.ok and not last.objections:
                return last  # it could not judge this portion: the whole look is not made
            objections.update({start + n: why for n, why in last.objections.items()})
        if objections:
            rows_named = ", ".join(str(n) for n in sorted(objections))
            return type(last)(
                False,
                f"row {rows_named} not supported by the cited reads (checked on {last.reviewer})",
                last.reviewer,
                objections,
            )
        return last

    async def _edit(
        self,
        title: str,
        table: list[dict[str, Any]],
        excerpts: dict[str, str],
        models: dict[str, str],
    ) -> tuple[list[dict[str, Any]], list[str]]:
        """The editing step of a document, after the second look: the author rewrites the
        confirmed paragraphs of each section (written file by file, part by part) into one text on
        how the parts work together, with no new facts. The edited paragraphs are looked at again;
        each confirmed one replaces the paragraphs it joins, a refused one leaves them: an edit can
        make the page read better, never lose what was confirmed (2026-11-12: edited before the
        look, whole sections fell for one doubtful clause). Returns the table and the sections
        where an edit stands."""
        edit = getattr(self.reviser, "edit", None)
        if edit is None:
            return table, []
        author = await self.reviser.author(models.get("author"))
        reviewer = models.get("reviewer")
        sections: dict[str, list[dict[str, Any]]] = {}
        for row in table:
            sections.setdefault(str(row.get("Abschnitt") or "").strip(), []).append(row)
        out: list[dict[str, Any]] = []
        done: list[str] = []
        for section, rows in sections.items():
            edited = await edit(author, title, section, rows) if len(rows) > 1 else None
            if not edited:
                out += rows
                continue
            look = await self._look(title, [r for r, _ in edited], excerpts, author, reviewer)
            refused = set(look.objections) if not look.ok else set()
            if not look.ok and not refused:  # it could not judge: nothing of the edit stands
                refused = set(range(1, len(edited) + 1))
            covered: set[int] = set()
            for n, (row, idx) in enumerate(edited, start=1):
                covered.update(idx)
                # a confirmed paragraph replaces those it joins; a refused one leaves them
                out += [rows[i] for i in idx] if n in refused else [row]
            out += [r for i, r in enumerate(rows) if i not in covered]
            if len(refused) < len(edited):
                done.append(section)
            if refused:
                log.info("edit of %s: %d of %d not confirmed", section, len(refused), len(edited))
        return out, done

    async def _overview(
        self,
        title: str,
        table: list[dict[str, Any]],
        excerpts: dict[str, str],
        models: dict[str, str],
    ) -> tuple[list[dict[str, Any]], bool]:
        """The page's introduction (2026-11-13): written from the confirmed Zweck paragraphs and
        looked at like every paragraph; it stands on top only when confirmed, and adds to the page
        (the Zweck paragraphs stay)."""
        write = getattr(self.reviser, "overview", None)
        purposes = [r for r in table if r.get("Abschnitt") == "Zweck"]
        # one Zweck paragraph is enough: the edit may have joined those of every file into one
        if write is None or not purposes:
            return table, False
        author = await self.reviser.author(models.get("author"))
        row = await write(author, title, programs.OVERVIEW, purposes)
        if row is None:
            return table, False
        look = await self._look(title, [row], excerpts, author, models.get("reviewer"))
        if not look.ok:
            log.info("overview of %s not confirmed", title[:60])
            return table, False
        return [row, *table], True

    async def _revise(
        self,
        author: str,
        question: str,
        table: list[dict[str, Any]],
        objections: dict[int, str],
        excerpts: dict[str, str],
    ) -> tuple[list[dict[str, Any]], list[dict[str, str]]] | None:
        """The author's revision in the portions of the second look: only portions with an
        objection, each with the excerpts its rows cite (the whole table with all excerpts filled
        the author's context, 2026-11-12). None when no portion was revised."""
        out: list[dict[str, Any]] = []
        gone: list[dict[str, str]] = []
        revised = False
        for start, rows in look_portions(table, excerpts):
            mine = {
                n - start: why for n, why in objections.items() if start < n <= start + len(rows)
            }
            found = (
                await self.reviser.revise(author, question, rows, mine, excerpts) if mine else None
            )
            if found is None:
                out += rows
                continue
            out += found[0]
            gone += found[1]
            revised = True
        return (out, gone) if revised else None

    async def _review(
        self,
        question: str,
        table: list[dict[str, Any]],
        excerpts: dict[str, str],
        models: dict[str, str],
    ) -> tuple[list[dict[str, Any]], list[dict[str, str]], dict[str, Any]]:
        """Second look, then revision by the author while the reviewer objects and a round changes
        something (project_revise.py). Returns the table that stands, what was removed and why, and
        how the second look ended (``ok`` only for a table it confirmed as a whole)."""
        author = await self.reviser.author(models.get("author"))
        reviewer = models.get("reviewer")
        removed: list[dict[str, str]] = []
        rounds = 0
        look = await self._look(question, table, excerpts, author, reviewer)
        while not look.ok and look.objections and rounds < revise.MAX_ROUNDS:
            found = await self._revise(author, question, table, look.objections, excerpts)
            if found is None:
                break
            new_table, gone = found
            for n in reversed(revise.unknown_refs(new_table, set(excerpts))):
                row = new_table.pop(n - 1)  # a revision may not invent a source
                removed.append(
                    {
                        "Aussage": str(row.get("Aussage") or row.get("Text") or ""),
                        "Grund": revise.INVENTED,
                    }
                )
            rounds += 1
            if revise.same_table(new_table, table) and not gone:
                break  # the progress guard: the author changed nothing
            removed += gone
            table = new_table
            if not table:
                break
            look = await self._look(question, table, excerpts, author, reviewer)
        ok = look.ok
        if not ok and look.objections and table:
            # what the reviewer still does not confirm leaves the table, with its last reason;
            # every other row was confirmed in this very look
            kept = []
            for n, row in enumerate(table, start=1):
                if n in look.objections:
                    why = look.objections[n]
                    removed.append(
                        {
                            "Aussage": str(row.get("Aussage") or row.get("Text") or ""),
                            "Grund": f"{revise.UNCONFIRMED}: {why}",
                        }
                    )
                else:
                    kept.append(row)
            table, ok = kept, bool(kept)
        return (
            table,
            removed,
            {
                "ok": ok and bool(table),
                # no verdict at all (the look could not run): the rows stand, unchecked
                "unchecked": bool(table) and not look.ok and not look.objections,
                "same_family": model_profiles.same_family(look.reviewer, author),
                "reviewer": look.reviewer,
                "author": author,
                "rounds": rounds,
                "rows": len(table),
                "reason": look.reason,
            },
        )

    async def _file_document(
        self,
        user_id: uuid.UUID,
        node: ProjectNode,
        table: list[dict[str, Any]],
        sources: list[programs.Source],
        removed: list[dict[str, str]],
        unchecked: bool = False,
    ) -> None:
        """The confirmed paragraphs as a document below the task (a report of the project);
        ``unchecked``: the second look could not run, and the page says so."""
        from topcoat_hub.project_store import ProjectStore  # the store imports this module's kin

        body = programs.render_document(
            node.title,
            table,
            sources,
            removed,
            build_info.BUILD,
            clock.hub_now().strftime("%Y-%m-%d"),
            unchecked=unchecked,
        )
        try:
            await ProjectStore(self._sessionmaker, self._store.actor).create_node(
                user_id,
                node.project_id,
                kind="report",
                title=f"Dokument: {node.title}"[:200],
                body=body,
                parent_id=node.id,
                report_kind="document",
            )
        except Exception:  # the result stands; the person sees the rows on the task
            log.exception("the document of task %s was not filed", node.id)

    async def _end_program(
        self,
        user_id: uuid.UUID,
        task_id: uuid.UUID,
        status: str,
        error: str | None,
        diagnosis: dict[str, Any] | None,
        sources: list[programs.Source] | None = None,
        stop_reason: str | None = None,
    ) -> None:
        async with self._sessionmaker() as db:
            task = await db.get(AgentTask, task_id)
            if task is None or task.user_id != user_id:
                return
            for s in sources or []:
                db.add(
                    AgentEvidence(
                        task_id=task_id,
                        user_id=user_id,
                        ref=s.ref,
                        tag=s.tag,
                        source=s.source,
                        excerpt=s.excerpt,
                        bytes=len(s.excerpt),
                    )
                )
            task.status = status
            task.error = error
            task.stop_reason = stop_reason
            if diagnosis is not None:
                task.diagnosis = diagnosis
                table = diagnosis.get("table") or []
                task.summary = f"{len(table)} result rows by the program {diagnosis['program']}"
            task.finished_at = clock.hub_now()
            await db.commit()

    async def _model_of(self, user_id: uuid.UUID, task_id: uuid.UUID) -> str | None:
        """The model a run was asked for (a retry keeps it, so a named model stays the one used)."""
        async with self._sessionmaker() as db:
            task = await db.scalar(
                select(AgentTask).where(AgentTask.id == task_id, AgentTask.user_id == user_id)
            )
        return task.model if task is not None else None

    async def _user(self, user_id: uuid.UUID) -> CurrentUser:
        async with self._sessionmaker() as db:
            role = await db.scalar(select(User.role).where(User.id == user_id))
        return CurrentUser(id=str(user_id), name="", role=role or "user")

    # --- batches ---------------------------------------------------------------------------------

    async def create_batch(
        self, user_id: uuid.UUID, project_id: uuid.UUID, node_ids: list[uuid.UUID], max_runs: int
    ) -> ProjectBatch:
        if not node_ids:
            raise RunError("a batch needs at least one task")
        if len(set(node_ids)) != len(node_ids):
            raise RunError("a task is named twice")
        if not 1 <= max_runs <= MAX_RUNS:
            raise RunError(f"the limit of runs must be between 1 and {MAX_RUNS}")
        async with self._sessionmaker() as db:
            project = await get_project(db, user_id, project_id)
            for nid in node_ids:
                node = await get_node(db, user_id, nid)
                if node.project_id != project.id:
                    raise NotFound("node")
            now = clock.hub_now()
            batch = ProjectBatch(
                user_id=user_id,
                project_id=project_id,
                node_ids=[str(n) for n in node_ids],
                max_runs=max_runs,
                runs=0,
                status="active",
                created_at=now,
                updated_at=now,
            )
            db.add(batch)
            await db.commit()
            batch_id = batch.id
        # the person approves them again: a blocked task goes back into the queue
        for nid in node_ids:
            async with self._sessionmaker() as db:
                node = await get_node(db, user_id, nid)
                was = node.status
            if was == "blocked":
                await self._store.set_run(
                    user_id, nid, status="todo", task_id=None, reason="approved again"
                )
        await self.dispatch(user_id, batch_id)
        async with self._sessionmaker() as db:
            return await db.get(ProjectBatch, batch_id)  # type: ignore[return-value]

    async def stop_batch(self, user_id: uuid.UUID, batch_id: uuid.UUID) -> ProjectBatch:
        """The person's stop: no further task starts. A run that is going on finishes as it is."""
        async with self._sessionmaker() as db:
            batch = await self._batch(db, user_id, batch_id)
            if batch.status == "active":
                self._end(batch, "stopped", "stopped by the person")
                await db.commit()
            await db.refresh(batch)
            return batch

    async def batches(self, user_id: uuid.UUID, project_id: uuid.UUID) -> list[ProjectBatch]:
        async with self._sessionmaker() as db:
            await get_project(db, user_id, project_id)
            rows = await db.scalars(
                select(ProjectBatch)
                .where(ProjectBatch.user_id == user_id, ProjectBatch.project_id == project_id)
                .order_by(ProjectBatch.created_at.desc())
                .limit(20)
            )
            return list(rows)

    async def detail(self, user_id: uuid.UUID, batch_id: uuid.UUID) -> dict[str, Any]:
        """The batch and the state of each of its tasks, for the card in the chat: the node's
        status now, and for a blocked one the reason of its last status change (the Akte)."""
        async with self._sessionmaker() as db:
            batch = await self._batch(db, user_id, batch_id)
            nodes = await self._batch_nodes(db, user_id, batch)
            waits = await unfinished_blockers(db, user_id, [n.id for n in nodes])
            tasks = []
            for node in nodes:
                last = await db.scalar(
                    select(ProjectEvent.payload)
                    .where(
                        ProjectEvent.user_id == user_id,
                        ProjectEvent.node_id == node.id,
                        ProjectEvent.kind == "node.status_changed",
                    )
                    .order_by(ProjectEvent.seq.desc())
                    .limit(1)
                )
                last = last or {}
                reason = (last.get("reason") or None) if node.status == "blocked" else None
                tasks.append(
                    {
                        "node_id": str(node.id),
                        "number": node.number,
                        "title": node.title,
                        "status": node.status,
                        "reason": reason,
                        "class": last.get("class"),
                        "waits_for": waits[str(node.id)],
                        "application": target_of(node.body or ""),
                        "mode": mode_of(node.body or ""),
                    }
                )
            return {**batch_view(batch), "tasks": tasks}

    # --- the end of a run ------------------------------------------------------------------------

    async def on_task_event(self, user_id: uuid.UUID, task_id: uuid.UUID, event: str) -> None:
        """Called when an agent task ends. A task on no node is none of our business."""
        batch_id = await self._settle_end(user_id, task_id, event)
        if batch_id is not None:
            await self.dispatch(user_id, batch_id)

    async def _settle_end(
        self, user_id: uuid.UUID, task_id: uuid.UUID, event: str
    ) -> uuid.UUID | None:
        """The end of a run: its node takes the verdict. Returns the batch whose queue to dispatch
        again, if any (a task sent back to the queue waits for the next trigger instead)."""
        if event not in END_STATES:
            return None
        # the end of a run is heard twice at times (its own event, and a dispatch that finds the
        # run over): the second one waits for nothing, so the result is judged once
        if task_id in self._settling:
            return None
        self._settling.add(task_id)
        try:
            return await self._settle_once(user_id, task_id, event)
        finally:
            self._settling.discard(task_id)

    async def _settle_once(
        self, user_id: uuid.UUID, task_id: uuid.UUID, event: str
    ) -> uuid.UUID | None:
        node = await self._node_of(user_id, task_id)
        log.info("batch run %s ended (%s): node %s", task_id, event, node.number if node else None)
        if node is None or node.status != "in_progress":
            return None
        status, reason = await self._verdict(user_id, node, task_id, event)
        if status == "retry":
            # the run ended without its table: ask once more; a second miss blocks the node
            try:
                await self.start_node(
                    user_id, node.id, retry=True, model=await self._model_of(user_id, task_id)
                )
                return None
            except Exception as exc:
                log.info("task %s: the run again did not start (%s)", node.id, exc)
                status, reason = "blocked", f"the run again did not start: {exc}"
        node = await self._store.set_run(
            user_id,
            node.id,
            status=status,
            task_id=task_id,
            reason=reason,
            result_class=await self._result_class(user_id, task_id),
        )
        if status in ("in_review", "blocked"):
            await self._file_result(user_id, node, task_id, status, reason)
        batch_id = await self._batch_of(user_id, node.id)
        if batch_id is None or status == "todo":
            return None
        # a blocked task holds back only the tasks that wait for it; the others go on, and the
        # batch ends with the blocked ones named (dispatch)
        return batch_id

    async def _file_result(
        self, user_id: uuid.UUID, node: ProjectNode, task_id: uuid.UUID, status: str, reason: str
    ) -> None:
        """The result of a run as a report below its task, so the person finds it where the task
        is (live 2026-11-14: task 1.5 stood at "in review" and its table was only in the agent
        run, which no page of the project shows). A run that filed its document itself (a
        program) gets no second one. A failure here leaves the verdict as it is."""
        from topcoat_hub.project_store import ProjectStore  # the store imports this module's kin

        try:
            async with self._sessionmaker() as db:
                task = await db.get(AgentTask, task_id)
                if task is None or task.user_id != user_id:
                    return
                filed = await db.scalar(
                    select(ProjectNode.id).where(
                        ProjectNode.parent_id == node.id,
                        ProjectNode.kind == "report",
                        ProjectNode.user_id == user_id,
                        ProjectNode.created_at >= task.created_at,
                    )
                )
            iterations = IterationStore(self._sessionmaker, self._store.actor)
            if filed is not None:  # filed by the program itself: it is the iteration all the same
                await iterations.file(user_id, node.id, filed)
                return
            report = (task.diagnosis or {}).get("report")
            if isinstance(report, dict) and report.get("text"):
                body = f"**Stand:** {reason}\n\n{report['text']}\n\n_Lauf: {task_id}_"
            else:
                async with self._sessionmaker() as db:
                    kept = await AgentStore(db, user_id).evidence(task_id)
                body = result_document(
                    task.diagnosis or {},
                    status,
                    reason,
                    str(task_id),
                    form_of(node.body),
                    [(e.ref, dict(e.source or {}), e.excerpt or "") for e in kept],
                )
            if not body:
                return
            report = await ProjectStore(self._sessionmaker, self._store.actor).create_node(
                user_id,
                node.project_id,
                kind="report",
                title=f"Ergebnis: {node.title}"[:200],
                body=body,
                parent_id=node.id,
                report_kind="document",
            )
            await iterations.file(user_id, node.id, report.id)
        except Exception:
            log.exception("the result of run %s was not filed below its task", task_id)

    async def _result_class(self, user_id: uuid.UUID, task_id: uuid.UUID) -> str | None:
        """The class of a run's result table (report_class.py), read from the run itself."""
        async with self._sessionmaker() as db:
            task = await db.get(AgentTask, task_id)
        if task is None or task.user_id != user_id or not task.diagnosis:
            return None
        table = task.diagnosis.get("table")
        if table is None:
            return None
        return report_class.classify(json.dumps(table, ensure_ascii=False))

    async def _verdict(
        self, user_id: uuid.UUID, node: ProjectNode, task_id: uuid.UUID, event: str
    ) -> tuple[str, str]:
        if event != "done":
            if event == "failed" and await self._node_failed(user_id, task_id):
                return "todo", "the node did not run it: back in the queue"
            return "blocked", f"the run ended without a result ({event})"
        async with self._sessionmaker() as db:
            task = await db.get(AgentTask, task_id)
        if task is None or task.user_id != user_id:
            return "blocked", "the run is not there any more"
        if task.stop_reason:
            return "blocked", f"the run stopped early ({task.stop_reason})"
        diagnosis = task.diagnosis or {}
        report = diagnosis.get("report")
        if isinstance(report, dict):  # a report program: its own check and revision judged it
            if not str(report.get("text") or "").strip():
                return "blocked", "the program wrote no report"
            if report.get("problems"):  # a theme or a fix draft the hub's own check refuses
                fix = diagnosis.get("program") in programs.FIX_PROGRAMS
                what = "the fix draft does not fit" if fix else "the theme does not fit the format"
                return "blocked", f"{what}: " + "; ".join(str(p) for p in report["problems"][:4])
            if report.get("checked"):
                return "in_review", "report written, checked by another model and revised"
            return "in_review", (
                "report written and revised; the last check still found issues (see the report)"
            )
        async with self._sessionmaker() as db:
            kept = await AgentStore(db, user_id).evidence(task_id)
        refs = {r.ref for r in kept if (r.excerpt or "").strip()}
        problem = result_problem(
            form_of(node.body), diagnosis.get("table"), checklist(node.body)[1], refs
        )
        looked = diagnosis.get("second_look") or {}
        if problem is not None:
            if (
                task.kind == programs.KIND
                and diagnosis.get("removed")
                and not diagnosis.get("table")
            ):
                return "blocked", "every statement was removed in the revision (see removed)"
            if diagnosis.get("table") is None and not task.prompt.startswith(RETRY_MARK):
                return "retry", problem
            return "blocked", problem
        secret = secret_problem(diagnosis["table"])
        if secret is not None:
            return "blocked", secret
        if (
            task.kind == programs.KIND
            and looked.get("ok")
            and looked.get("rows") == len(diagnosis["table"])
        ):
            # the revision loop ended with this very table confirmed by the second look
            removed = len(diagnosis.get("removed") or [])
            return "in_review", (
                f"every row is supported by its cited reads (checked on {looked.get('reviewer')}"
                f" after {looked.get('rounds', 0)} revision round(s); {removed} removed)"
            )
        if self.second_look is None:
            return "blocked", "no second look is set up on this hub"
        excerpts = {r.ref: r.excerpt for r in kept if (r.excerpt or "").strip()}
        look = await self.second_look.check(node.title, diagnosis["table"], excerpts, task.model)
        if not look.ok:
            return "blocked", look.reason
        return "in_review", f"{look.reason} (checked on {look.reviewer})"

    async def dispatch(self, user_id: uuid.UUID, batch_id: uuid.UUID) -> None:
        """Start the ready tasks of a batch on the free nodes, in the order the approval named them.
        A task belongs to a node only once its run starts there; a task that finds no free node
        waits in the queue, and the next trigger (a node joins, a run ends) starts it."""
        async with self._dispatching:
            async with self._sessionmaker() as db:
                batch = await self._batch(db, user_id, batch_id)
                if batch.status != "active":
                    return
                nodes = await self._batch_nodes(db, user_id, batch)
                graph = await blocker_graph(db, user_id)
            # a run that ended while its end was not heard (a restart, a lost event) is finished now
            for node in [n for n in nodes if n.status == "in_progress"]:
                ended = await self._ended_run(user_id, node.task_id)
                if ended is not None:
                    await self._settle_end(user_id, node.task_id, ended)
            async with self._sessionmaker() as db:
                nodes = await self._batch_nodes(db, user_id, batch)
            todo = [n for n in nodes if n.status == "todo"]
            running = [n for n in nodes if n.status == "in_progress"]
            if not todo and not running:
                blocked = [n.number for n in nodes if n.status == "blocked"]
                if blocked:
                    await self._finish(
                        user_id, batch_id, "stopped", f"blocked: {', '.join(blocked)}"
                    )
                else:
                    await self._finish(user_id, batch_id, "done", None)
                return
            if todo and not running and await self._runs_of(user_id, batch_id) >= batch.max_runs:
                await self._finish(user_id, batch_id, "stopped", "the limit of runs is reached")
                return
            # a blocker may sit in another batch or outside the batch: its status is read here
            blockers = {b for n in todo for b in graph.get(str(n.id), set())}
            finished = await self._finished_ids(user_id, blockers)
            ready = [n for n in todo if graph.get(str(n.id), set()) <= finished]
            if not ready and not running:
                await self._finish(
                    user_id, batch_id, "stopped", "no task can start: its blockers are not done"
                )
                return
            # as many runs as the nodes of the tasks' applications have places; the gateway picks
            # node and model from the application's row, like for any other request of it
            targets = {t for n in (*ready, *running) if (t := target_of(n.body))}
            places = await self.places(targets) if self.places is not None else 1
            free = places - len(running)
            for node in ready:
                if free <= 0 or await self._runs_of(user_id, batch_id) >= batch.max_runs:
                    break
                try:
                    await self.start_node(user_id, node.id, batch_id)
                except RunError as exc:  # the task itself cannot run: stop, say why
                    await self._stop_with(user_id, batch_id, str(exc))
                    return
                except Exception as exc:  # the queue does not take it now: the next trigger
                    log.info("batch %s: %s did not start (%s)", batch_id, node.number, exc)
                    break
                free -= 1

    async def kick_all(self) -> None:
        """A node joined or a place freed: every active batch looks at the queue again."""
        async with self._sessionmaker() as db:
            rows = await db.execute(
                select(ProjectBatch.user_id, ProjectBatch.id).where(ProjectBatch.status == "active")
            )
            pairs = [(u, b) for u, b in rows.all()]
        for user_id, batch_id in pairs:
            try:
                await self.dispatch(user_id, batch_id)
            except Exception:
                log.exception("batch %s: the dispatch failed", batch_id)

    async def _finish(
        self, user_id: uuid.UUID, batch_id: uuid.UUID, status: str, reason: str | None
    ) -> None:
        async with self._sessionmaker() as db:
            batch = await self._batch(db, user_id, batch_id)
            if batch.status == "active":
                self._end(batch, status, reason)
                await db.commit()

    async def _batch_nodes(
        self, db: AsyncSession, user_id: uuid.UUID, batch: ProjectBatch
    ) -> list[ProjectNode]:
        """The batch's task nodes in the order the approval named them."""
        out: list[ProjectNode] = []
        for raw in batch.node_ids:
            node = await db.scalar(
                select(ProjectNode).where(
                    ProjectNode.id == uuid.UUID(raw), ProjectNode.user_id == user_id
                )
            )
            if node is not None:
                out.append(node)
        return out

    async def _ended_run(self, user_id: uuid.UUID, task_id: uuid.UUID | None) -> str | None:
        """The end state of a run that is over (done, failed, canceled), else None."""
        if task_id is None:
            return None
        async with self._sessionmaker() as db:
            task = await db.scalar(
                select(AgentTask).where(AgentTask.id == task_id, AgentTask.user_id == user_id)
            )
        if task is None:
            return None
        if (
            task.kind == programs.KIND
            and task.status == "running"
            and task_id not in self._programs
        ):
            # a program run of a process that is gone (a hub restart): it goes back into the queue
            await self._end_program(
                user_id, task_id, "failed", "the hub restarted during the run", None,
                stop_reason=programs.RESTART,
            )  # fmt: skip
            return "failed"
        if task.status not in END_STATES:
            return None
        return task.status

    async def _finished_ids(self, user_id: uuid.UUID, ids: set[str]) -> set[str]:
        """Of these nodes, the ones finished enough to unblock a task (in review or done)."""
        if not ids:
            return set()
        async with self._sessionmaker() as db:
            rows = await db.execute(
                select(ProjectNode.id, ProjectNode.status).where(
                    ProjectNode.user_id == user_id,
                    ProjectNode.id.in_([uuid.UUID(i) for i in ids]),
                )
            )
            return {str(nid) for nid, status in rows.all() if status in ("done", "in_review")}

    async def _runs_of(self, user_id: uuid.UUID, batch_id: uuid.UUID) -> int:
        async with self._sessionmaker() as db:
            batch = await self._batch(db, user_id, batch_id)
            return batch.runs or 0

    async def _node_failed(self, user_id: uuid.UUID, task_id: uuid.UUID) -> bool:
        """Did the run end because its node could not run it (not because of the task)?"""
        async with self._sessionmaker() as db:
            task = await db.scalar(
                select(AgentTask).where(AgentTask.id == task_id, AgentTask.user_id == user_id)
            )
        return task is not None and task.stop_reason in REQUEUE_REASONS

    async def _stop_with(self, user_id: uuid.UUID, batch_id: uuid.UUID, reason: str) -> None:
        async with self._sessionmaker() as db:
            batch = await self._batch(db, user_id, batch_id)
            if batch.status == "active":
                self._end(batch, "stopped", reason)
                await db.commit()

    async def _count(self, user_id: uuid.UUID, batch_id: uuid.UUID) -> None:
        async with self._sessionmaker() as db:
            batch = await self._batch(db, user_id, batch_id)
            batch.runs = (batch.runs or 0) + 1
            batch.updated_at = clock.hub_now()
            await db.commit()

    # --- helpers ---------------------------------------------------------------------------------

    async def _blockers(
        self, user_id: uuid.UUID, node_id: uuid.UUID
    ) -> list[tuple[ProjectNode, list[ProjectNode]]]:
        """The tasks ``node_id`` waits for, each with its children."""
        async with self._sessionmaker() as db:
            ids = (await blocker_graph(db, user_id)).get(str(node_id), set())
            if not ids:
                return []
            rows = await db.scalars(
                select(ProjectNode).where(
                    ProjectNode.user_id == user_id,
                    ProjectNode.id.in_([uuid.UUID(i) for i in ids]),
                    ProjectNode.current.is_(True),
                )
            )
            tasks = sorted(rows, key=lambda n: n.number or "")
        return [(t, await self._children(user_id, t.id)) for t in tasks]

    async def _children(self, user_id: uuid.UUID, node_id: uuid.UUID) -> list[ProjectNode]:
        async with self._sessionmaker() as db:
            return list(
                await db.scalars(
                    select(ProjectNode).where(
                        ProjectNode.user_id == user_id,
                        ProjectNode.parent_id == node_id,
                        ProjectNode.trashed_at.is_(None),
                    )
                )
            )

    async def _node_of(self, user_id: uuid.UUID, task_id: uuid.UUID) -> ProjectNode | None:
        async with self._sessionmaker() as db:
            return await db.scalar(
                select(ProjectNode).where(
                    ProjectNode.task_id == task_id, ProjectNode.user_id == user_id
                )
            )

    async def _batch_of(self, user_id: uuid.UUID, node_id: uuid.UUID) -> uuid.UUID | None:
        async with self._sessionmaker() as db:
            rows = await db.scalars(
                select(ProjectBatch).where(
                    ProjectBatch.user_id == user_id, ProjectBatch.status == "active"
                )
            )
            for batch in rows:
                if str(node_id) in batch.node_ids:
                    return batch.id
        return None

    async def _batch(
        self, db: AsyncSession, user_id: uuid.UUID, batch_id: uuid.UUID
    ) -> ProjectBatch:
        batch = await db.scalar(
            select(ProjectBatch).where(ProjectBatch.id == batch_id, ProjectBatch.user_id == user_id)
        )
        if batch is None:
            raise NotFound("batch")
        return batch

    @staticmethod
    def _end(batch: ProjectBatch, status: str, reason: str | None) -> None:
        now = clock.hub_now()
        batch.status = status
        # the column holds 60 characters; the full reason is the node's line in the Akte
        batch.stop_reason = reason[:60] if reason else reason
        batch.updated_at = now
        if status != "active":
            batch.stopped_at = now


def batch_view(batch: ProjectBatch) -> dict[str, Any]:
    return {
        "id": str(batch.id),
        "project_id": str(batch.project_id),
        "node_ids": list(batch.node_ids),
        "max_runs": batch.max_runs,
        "runs": batch.runs,
        "status": batch.status,
        "stop_reason": batch.stop_reason,
        "created_at": batch.created_at.isoformat() if batch.created_at else None,
        "stopped_at": batch.stopped_at.isoformat() if batch.stopped_at else None,
    }


__all__ = [
    "APPLICATIONS",
    "RunError",
    "RunService",
    "batch_view",
    "form_of",
    "result_problem",
    "run_prompt",
    "target_of",
]
