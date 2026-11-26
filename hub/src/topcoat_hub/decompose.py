"""A large task cut into sub-tasks, checked by the hub (D109, issue #31).

The block program ``decompose`` (blocks/builtin.py) asks the code model of the house once, with
the task, a map of the repository and the Development contract; the server holds the answer to
the form ``proto/agents/outputs/decompose.schema.json``. The form cannot say whether a file exists
or the dependencies have a cycle, so the hub checks the content here, never a model:

* 1 to ``MAX_SUBTASKS`` sub-tasks with unique ids;
* each has one layer; store and api change files under ``hub/``, gui under ``web/src/``, proto
  under ``proto/``, docs under ``docs/``; admin (a setting) and research (reading) change none;
* at most ``MAX_SUBTASK_FILES`` existing files per sub-task, every one of them readable in the
  live code; every symbol stands in one of them (else the nearest names, as in D119);
* new files lie under the paths of their layer and do not exist yet;
* every acceptance criterion is a test, an endpoint, a screenshot or ``person:``;
* ``depends_on`` names sub-tasks of the same answer, and they form no cycle.

A refused answer gets back *all* its problems at once (:func:`check`), and the model may answer
once more (:func:`decompose`). Nothing here writes anywhere: filing the sub-tasks as child nodes
of the large task is the coordinator's (issue #32).
"""

from __future__ import annotations

import json
import re
from collections.abc import Awaitable, Callable
from dataclasses import asdict, dataclass, field
from typing import Any

from topcoat_hub import code_symbols
from topcoat_hub.blocks.builtin import LAYERS, MAX_SUBTASK_FILES, MAX_SUBTASKS

PROGRAM = "decompose"
TRIES = 2  # the first answer and one more with all its problems
# where the files of a layer lie; a layer that is not here changes no file
LAYER_PATHS: dict[str, tuple[str, ...]] = {
    "store": ("hub/",),
    "api": ("hub/",),
    "gui": ("web/src/",),
    "proto": ("proto/",),
    "docs": ("docs/",),
}
CODE_LAYERS = tuple(LAYER_PATHS)
ACCEPT_KINDS = ("test", "endpoint", "screenshot", "person")
_ID = re.compile(r"^[A-Za-z][A-Za-z0-9_.-]{0,19}$")
_TEST = re.compile(r"^(?:hub/tests/[\w./-]+\.py|web/src/[\w./-]+\.test\.tsx?)(?:::[\w.\[\]-]+)?$")
_ENDPOINT = re.compile(r"^(?:GET|POST|PUT|PATCH|DELETE)\s+/api/\S+$")
TASK_CHARS = 8000
MAP_CHARS = 12000

# (program, inputs) -> (answer, model), as issue_investigation.Ask
Ask = Callable[[str, dict[str, Any]], Awaitable[tuple[str, str]]]
# paths -> their live texts (None: no such file); a path left out could not be read
Read = Callable[[list[str]], Awaitable[dict[str, str | None]]]


@dataclass
class Subtask:
    id: str
    title: str
    goal: str
    layer: str
    files: list[str] = field(default_factory=list)
    symbols: list[str] = field(default_factory=list)
    new_files: list[str] = field(default_factory=list)
    accept: list[str] = field(default_factory=list)
    depends_on: list[str] = field(default_factory=list)


@dataclass
class Decomposition:
    summary: str
    subtasks: list[Subtask]

    def order(self) -> list[str]:
        """The ids in an order that runs every sub-task after its dependencies (stable: among the
        ready ones, the order of the answer)."""
        return topological([(s.id, s.depends_on) for s in self.subtasks]) or []

    def as_json(self) -> dict[str, Any]:
        return {"summary": self.summary, "subtasks": [asdict(s) for s in self.subtasks]}


@dataclass
class Outcome:
    decomposition: Decomposition | None = None
    problems: list[str] = field(default_factory=list)  # of the last answer, when refused
    answers: list[str] = field(default_factory=list)  # every answer as written
    model: str = ""


def topological(nodes: list[tuple[str, list[str]]]) -> list[str] | None:
    """The ids of ``nodes`` (id, dependencies) in a runnable order, or ``None`` when they have a
    cycle. Dependencies on unknown ids are ignored here (:func:`check` reports them)."""
    known = {n for n, _ in nodes}
    deps = {n: {d for d in ds if d in known and d != n} for n, ds in nodes}
    done: list[str] = []
    while len(done) < len(nodes):
        ready = [n for n, _ in nodes if n not in done and deps[n] <= set(done)]
        if not ready:
            return None
        done.append(ready[0])
    return done


def _in_cycle(nodes: list[tuple[str, list[str]]]) -> list[str]:
    """The ids that cannot run because of a cycle (on one or depending on one)."""
    known = {n for n, _ in nodes}
    deps = {n: {d for d in ds if d in known and d != n} for n, ds in nodes}
    done: set[str] = set()
    changed = True
    while changed:
        changed = False
        for n, _ in nodes:
            if n not in done and deps[n] <= done:
                done.add(n)
                changed = True
    return [n for n, _ in nodes if n not in done]


def _strings(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [" ".join(str(v).split()) for v in value if isinstance(v, str | int) and str(v).strip()]


def _path_ok(path: str) -> bool:
    return (
        bool(path)
        and not path.startswith("/")
        and ".." not in path.split("/")
        and "\\" not in path
        and "//" not in path
    )


def _accept_problem(text: str) -> str:
    kind, sep, rest = text.partition(":")
    kind, rest = kind.strip().lower(), rest.strip()
    if not sep or kind not in ACCEPT_KINDS or not rest:
        return f"{text!r} is not one of {', '.join(k + ':…' for k in ACCEPT_KINDS)}"
    if kind == "test" and not _TEST.match(rest):
        return f"{text!r}: a test is hub/tests/<file>.py or web/src/<file>.test.tsx (::name)"
    if kind == "endpoint" and not _ENDPOINT.match(rest):
        return f"{text!r}: an endpoint is a method and a path under /api/, e.g. GET /api/x"
    if kind == "screenshot" and not rest.startswith("/"):
        return f"{text!r}: a screenshot names a route of the GUI, e.g. /projects"
    return ""


def parse(text: str) -> tuple[dict[str, Any] | None, list[Subtask], list[str]]:
    """``(document, sub-tasks, problems of form)`` of an answer; the checks that need the code
    are :func:`check`'s."""
    from topcoat_hub.issue_fixes import _json_object  # the tolerant reader, as for fix plans

    doc = _json_object(text or "")
    if doc is None:
        return None, [], ["the answer holds no JSON object"]
    raw = doc.get("subtasks")
    if not isinstance(raw, list) or not raw:
        return doc, [], ["the answer has no sub-tasks"]
    problems: list[str] = []
    if len(raw) > MAX_SUBTASKS:
        problems.append(f"{len(raw)} sub-tasks: at most {MAX_SUBTASKS} per large task")
    subs: list[Subtask] = []
    seen: set[str] = set()
    for n, s in enumerate(raw, 1):
        if not isinstance(s, dict):
            problems.append(f"sub-task {n} is not an object")
            continue
        sid = str(s.get("id") or "").strip()
        name = f"sub-task {sid or n}"
        if not _ID.match(sid):
            problems.append(f"{name}: the id is a short name such as s1")
        elif sid in seen:
            problems.append(f"{name}: the id stands twice")
        seen.add(sid)
        sub = Subtask(
            id=sid or str(n),
            title=" ".join(str(s.get("title") or "").split())[:200],
            goal=" ".join(str(s.get("goal") or "").split())[:500],
            layer=str(s.get("layer") or "").strip().lower(),
            files=list(dict.fromkeys(_strings(s.get("files")))),
            symbols=list(dict.fromkeys(_strings(s.get("symbols")))),
            new_files=list(dict.fromkeys(_strings(s.get("new_files")))),
            accept=_strings(s.get("accept")),
            depends_on=list(dict.fromkeys(_strings(s.get("depends_on")))),
        )
        if not sub.title:
            problems.append(f"{name}: it has no title")
        if not sub.goal:
            problems.append(f"{name}: it has no goal")
        if sub.layer not in LAYERS:
            problems.append(f"{name}: the layer is one of {', '.join(LAYERS)}, not {sub.layer!r}")
        subs.append(sub)
    return doc, subs, problems


def _layer_problems(sub: Subtask) -> list[str]:
    name = f"sub-task {sub.id}"
    problems: list[str] = []
    if sub.layer not in LAYERS:
        return problems  # parse reported it
    if sub.layer not in CODE_LAYERS:
        if sub.files or sub.new_files:
            problems.append(f"{name}: a {sub.layer} sub-task changes no file; make it a code layer")
        return problems
    if not sub.files and not sub.new_files:
        problems.append(f"{name}: a {sub.layer} sub-task names the files it changes")
    if len(sub.files) > MAX_SUBTASK_FILES:
        problems.append(
            f"{name}: {len(sub.files)} files, at most {MAX_SUBTASK_FILES}: cut it in two"
        )
    places = LAYER_PATHS[sub.layer]
    for path in [*sub.files, *sub.new_files]:
        if not _path_ok(path):
            problems.append(f"{name}: {path!r} is not a path in the repository")
        elif not path.startswith(places):
            problems.append(
                f"{name}: {path} is not under {' or '.join(places)} (layer {sub.layer}); a "
                "sub-task stays in one layer"
            )
    return problems


async def check(text: str, read: Read) -> tuple[Decomposition | None, list[str]]:
    """The decomposition an answer of ``decompose`` holds, or every problem that makes it none."""
    doc, subs, problems = parse(text)
    if doc is None:
        return None, problems
    ids = {s.id for s in subs}
    for sub in subs:
        problems += _layer_problems(sub)
        for a in sub.accept:
            if why := _accept_problem(a):
                problems.append(f"sub-task {sub.id}: {why}")
        if not sub.accept:
            problems.append(f"sub-task {sub.id}: it has no acceptance criterion")
        for d in sub.depends_on:
            if d == sub.id:
                problems.append(f"sub-task {sub.id}: it depends on itself")
            elif d not in ids:
                problems.append(f"sub-task {sub.id}: depends on {d!r}, which is not in the answer")
    nodes = [(s.id, s.depends_on) for s in subs]
    if subs and topological(nodes) is None:
        stuck = [n for n in _in_cycle(nodes) if n in ids]
        problems.append(f"the dependencies form a cycle (sub-tasks {', '.join(stuck)})")
    # the code: one read of every path the answer names
    paths = sorted({p for s in subs for p in [*s.files, *s.new_files] if _path_ok(p)})
    texts = await read(paths) if paths else {}
    for sub in subs:
        name = f"sub-task {sub.id}"
        for path in sub.files:
            if not _path_ok(path):
                continue
            if path not in texts:
                problems.append(f"{name}: {path} could not be read")
            elif texts[path] is None:
                problems.append(f"{name}: {path} does not exist (a new file goes in new_files)")
        for path in sub.new_files:
            if _path_ok(path) and texts.get(path) is not None:
                problems.append(f"{name}: {path} exists already (name it in files)")
        readable = {p: t for p in sub.files if isinstance(t := texts.get(p), str)}
        if not readable:
            continue  # nothing to look the symbols up in (reported above, or only new files)
        for sym in sub.symbols:
            found, near = False, []
            for path, body in readable.items():
                hits, close = code_symbols.find(path, body, sym)
                if hits:
                    found = True
                    break
                near += close
            if not found:
                hint = f"; nearest: {', '.join(dict.fromkeys(near))}" if near else ""
                problems.append(f"{name}: the symbol {sym!r} is not in {', '.join(readable)}{hint}")
    if problems:
        return None, problems
    summary = " ".join(str(doc.get("summary") or "").split())[:500]
    return Decomposition(summary, subs), []


def feedback(problems: list[str], answer: str) -> str:
    """What the next try is told: its answer before and every problem of it."""
    return (
        "Your answer before was refused by the hub's check. Write it again and correct ALL of "
        "these problems (keep what was right):\n"
        + "\n".join(f"- {p}" for p in problems)
        + f"\n\nYour answer before:\n<<<\n{answer[:TASK_CHARS]}\n>>>"
    )


async def decompose(
    ask: Ask,
    read: Read,
    task: str,
    repo_map: str,
    contract: str,
    *,
    hint: str = "",
    tries: int = TRIES,
) -> Outcome:
    """Up to ``tries`` runs of ``decompose``: each answer checked, the next one told every problem
    of the one before. ``hint`` goes in front of the feedback (a replan says its branch there)."""
    out = Outcome()
    note = hint
    for _ in range(max(1, tries)):
        answer, model = await ask(
            PROGRAM,
            {
                "task": task[:TASK_CHARS],
                "map": repo_map[:MAP_CHARS] or "(no map: the workshop could not be read)",
                "contract": contract,
                "feedback": note,
            },
        )
        out.answers.append(answer)
        out.model = model or out.model
        found, problems = await check(answer, read)
        if found is not None:
            out.decomposition, out.problems = found, []
            return out
        out.problems = problems
        note = "\n\n".join(p for p in (hint, feedback(problems, answer)) if p)
    return out


def as_text(dec: Decomposition) -> str:
    """The decomposition as JSON text (what a node keeps, what the view reads)."""
    return json.dumps(dec.as_json(), ensure_ascii=False, indent=2)
