"""A fix in steps (2026-11-19): the plan of ``issue_steps`` cut into places, one fresh run each.

Dev wrote fixes at one place well (issues 2.5, 2.6) and failed at changes over several (2.2, 2.3:
invented code, missing parts, an existing test file written as new). So the work is cut before
anyone writes code:

1. **Plan** (the program ``issue_steps``): steps as JSON, each a file, an *anchor* (one line that
   stands in the file, e.g. a ``def`` line), what to do there and the steps it builds on, plus a
   test in a new file. The hub checks every anchor against the live code (the file exists, the
   line stands there exactly once) and asks for a new plan once, with the reason.
2. **Steps** (the program ``fix_step``, one fresh run each): the code around the step's anchor as
   the steps before left it (a *virtual* state: their edits applied to the live text), the plan and
   the diffs of the steps before. Its edits are checked on that state (:func:`check_edits`); a
   refused step runs once more with the reason and the real lines, then its edits are applied.
3. **One fix:** all edits in order and the test file, the draft that ``project_runs`` keeps and
   checks like any other.

Nothing here writes code anywhere: the result is a draft; applying it stays the person's click
(issue_fixes.py, D103)."""

import difflib
import json
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from topcoat_hub import code_symbols
from topcoat_hub.blocks.builtin import MAX_FIX_STEPS
from topcoat_hub.issue_fixes import PLACES, _json_object, _path_ok, check_edits, parse_fix

AROUND = 60  # lines of code on each side of a step's anchor
NOTHING = "NOTHING"  # a step's answer when its work is in the code already
DONE_CHARS = 8000  # the diffs of the steps before, at most
TASK_CHARS = 6000
_NUMBERED = re.compile(r"^\s*\d+\s*[:|\t]\s?")  # "120: def x()" - a line number copied along
# "147-148", "l. 120", "line 88": a line number given as the anchor (live 2026-11-23, 2.18.3)
_LINE_NUMBER = re.compile(r"^\s*(?:l\.|lines?|zeilen?)?\s*\d+(?:\s*[-–]\s*\d+)?\s*$", re.I)  # noqa: RUF001
WHERE = ("in", "before", "after", "end")
SYMBOL_AROUND = 10  # lines shown on each side of a symbol's body
SYMBOL_SHOWN = 160  # a longer symbol is shown around its head only
MAX_SYMBOL_LINES = 80  # a symbol written anew whole (REPLACE SYMBOL) is at most this long


@dataclass
class Step:
    n: int
    path: str
    anchor: str  # a line of the file; for a symbol step the hub sets its first line
    do: str
    after: list[int] = field(default_factory=list)
    symbol: str = ""  # Class.method, function, JSON key path, heading, CSS selector (1.10)
    where: str = "in"  # in, before, after the symbol, or the end of the file


@dataclass
class Plan:
    summary: str
    steps: list[Step]
    test_path: str
    test_do: str


@dataclass
class Outcome:
    summary: str = ""
    edits: list[dict[str, str]] = field(default_factory=list)
    files: list[dict[str, str]] = field(default_factory=list)
    log: list[str] = field(default_factory=list)  # one line per plan and step, for the result
    failed: str = ""  # why no whole fix came of it ("" when it did)


# --- pure ----------------------------------------------------------------------------------------


MAX_GIVEN_STEPS = 12


def _is_test_file(path: str) -> bool:
    name = path.rsplit("/", 1)[-1]
    return (path.startswith("web/src/") and path.endswith((".test.ts", ".test.tsx"))) or (
        path.startswith("hub/tests/") and name.startswith("test_") and name.endswith(".py")
    )


def tests_out_of_steps(doc: dict[str, Any]) -> dict[str, Any]:
    """A step that writes a test file is the plan's test, not a step (live 2026-11-21, issue 2.10:
    gpt-oss put the new GUI test among the steps in four rounds, and named a Python test for a
    change of web/ only). The hub moves it: such steps leave the list (the others are numbered
    again, their "after" follows), and the plan's test becomes the step's test when the plan has
    none, or when every code step is in web/ and its test is not a GUI test. A step another step
    builds on stays where it is."""
    raw = doc.get("steps") if isinstance(doc.get("steps"), list) else []
    needed = {
        int(a) for s in raw if isinstance(s, dict) for a in s.get("after") or [] if str(a).isdigit()
    }
    moved = [
        (n, s)
        for n, s in enumerate(raw, 1)
        if isinstance(s, dict)
        and _is_test_file(str(s.get("path") or "").strip())
        and n not in needed
    ]
    if not moved:
        return doc
    gone = {n for n, _ in moved}
    renumber: dict[int, int] = {}
    kept: list[Any] = []
    for n, s in enumerate(raw, 1):
        if n in gone:
            continue
        kept.append(s)
        renumber[n] = len(kept)
    for s in kept:
        if isinstance(s, dict) and s.get("after"):
            s["after"] = [
                renumber[int(a)] for a in s["after"] if str(a).isdigit() and int(a) in renumber
            ]
    test = doc.get("test") if isinstance(doc.get("test"), dict) else {}
    test_path = str(test.get("path") or "").strip()
    code_paths = [str(s.get("path") or "") for s in kept if isinstance(s, dict)]
    gui_only = bool(code_paths) and all(p.startswith("web/") for p in code_paths)
    first_path = str(moved[0][1].get("path")).strip()
    if not test_path or (
        gui_only and not test_path.startswith("web/") and first_path.startswith("web/")
    ):
        step = moved[0][1]
        test = {"path": first_path, "do": step.get("do") or test.get("do") or ""}
    return {**doc, "steps": kept, "test": test}


def parse_plan(text: str, *, repair: bool = False) -> tuple[Plan | None, list[str]]:
    """The plan a run of ``issue_steps`` wrote, or the problems that make it none. A ``repair``
    of a draft may have no code steps (only its test was wrong)."""
    doc = _json_object(text or "")
    if doc is None:
        return None, ["the answer holds no JSON object"]
    doc = tests_out_of_steps(doc)
    problems: list[str] = []
    steps: list[Step] = []
    raw = doc.get("steps") if isinstance(doc.get("steps"), list) else []
    for n, s in enumerate(raw, 1):
        if not isinstance(s, dict):
            problems.append(f"step {n} is not an object")
            continue
        path = str(s.get("path") or "").strip()
        # several lines are kept: plan_problems takes the first one that stands once (live
        # 2026-11-19: the anchor of a call that spans three lines was refused)
        lines = [_NUMBERED.sub("", ln).strip() for ln in str(s.get("anchor") or "").splitlines()]
        anchor = "\n".join(ln for ln in lines if ln)
        symbol = " ".join(str(s.get("symbol") or "").split())
        where = str(s.get("where") or "in").strip().lower()
        where = {"new": "after" if symbol else "end", "inside": "in", "at": "in"}.get(where, where)
        do = " ".join(str(s.get("do") or "").split())
        # a step that names itself is no error (live 2026-11-19, gpt-oss: step 1 "after": [1])
        after = [int(a) for a in s.get("after") or [] if str(a).isdigit() and int(a) != n]
        if not _path_ok(path):
            problems.append(f"step {n}: {path!r} is not a file under {PLACES}")
        elif where not in WHERE:
            problems.append(f"step {n} ({path}): where is one of {', '.join(WHERE)}, not {where!r}")
        elif not symbol and where != "end" and _LINE_NUMBER.match(anchor or "0"):
            problems.append(
                f"step {n} ({path}): name the symbol the change is at (function, Class.method, "
                "component, JSON key path, heading), not a line number"
            )
        elif not do:
            problems.append(f"step {n} ({path}): it does not say what to do")
        elif any(a >= n for a in after):
            problems.append(f"step {n}: it builds on a step that comes later ({after})")
        else:
            steps.append(Step(n, path, anchor, do, after, symbol, where))
    if not raw and not repair:
        problems.append("the plan has no steps")
    # a plan the person wrote ("given", RunService.given_plan) may be finer: the limit is the
    # models' (live 2026-11-21: the plan of issue 2.10 needed ten small steps)
    limit = MAX_GIVEN_STEPS if doc.get("given") is True else MAX_FIX_STEPS
    if len(raw) > limit:
        problems.append(f"at most {limit} steps")
    test = doc.get("test") if isinstance(doc.get("test"), dict) else {}
    test_path = str(test.get("path") or "").strip()
    test_do = " ".join(str(test.get("do") or "").split())
    web_test = test_path.startswith("web/src/") and test_path.endswith((".test.ts", ".test.tsx"))
    if not (test_path.startswith("hub/tests/") or web_test) or not _path_ok(test_path):
        problems.append(
            f"the test {test_path!r} is not a new file under hub/tests/ "
            "(or web/src/…/<name>.test.ts(x) for the GUI)"
        )
    elif not test_do:
        problems.append("the test does not say what it checks")
    summary = " ".join(str(doc.get("summary") or "").split())[:500]
    if problems:
        return None, problems
    return Plan(summary, steps, test_path, test_do), []


_OLD = re.compile(r"^\s*<{5,}\s*OLD\s*$")
_MID = re.compile(r"^\s*={5,}\s*$")
_NEW = re.compile(r"^\s*>{5,}\s*NEW\s*$")
_FILE = re.compile(r"^\s*<{5,}\s*FILE\s*$")
_END = re.compile(r"^\s*>{5,}\s*END\s*$")
_SYMBOL = re.compile(r"^\s*<{5,}\s*(?:REPLACE\s+)?SYMBOL\s+(\S.*?)\s*$")


def parse_blocks(text: str, path: str) -> tuple[list[dict[str, str]], list[str], list[str]]:
    """The edits of a step's answer in marker blocks: ``(edits, file contents, problems)``.

    Live 2026-11-19: a step answered in JSON with Python code in its strings; the escaping of a
    docstring broke (``\\"\\"\\n`` for three quotes) and nothing of the edit was left. A step
    knows its file, so it writes plain blocks instead::

        <<<<<<< OLD
        lines copied from the code
        =======
        the lines that replace them
        >>>>>>> NEW

    and a new file as ``<<<<<<< FILE`` ... ``>>>>>>> END``. Every block is whole lines.

    A small symbol may be written anew whole (1.10), its old text is the hub's to find
    (:func:`symbol_edits`); the edit carries ``symbol`` in place of ``old``::

        <<<<<<< REPLACE SYMBOL Class.method
        the whole new definition
        >>>>>>> END"""
    lines = (text or "").splitlines()
    edits: list[dict[str, str]] = []
    files: list[str] = []
    problems: list[str] = []
    i = 0
    while i < len(lines):
        sym = _SYMBOL.match(lines[i])
        if sym:
            body: list[str] = []
            i += 1
            while i < len(lines) and not _END.match(lines[i]):
                body.append(lines[i])
                i += 1
            if i >= len(lines):
                problems.append(f"REPLACE SYMBOL {sym.group(1)}: the block has no '>>>>>>> END'")
                break
            while body and body[0].strip().startswith("```"):
                body.pop(0)
            while body and (not body[-1].strip() or body[-1].strip().startswith("```")):
                body.pop()
            if not "".join(body).strip():
                problems.append(f"REPLACE SYMBOL {sym.group(1)}: the new definition is empty")
            else:
                edits.append({"path": path, "symbol": sym.group(1), "new": "\n".join(body) + "\n"})
        elif _OLD.match(lines[i]):
            old: list[str] = []
            new: list[str] = []
            i += 1
            while i < len(lines) and not _MID.match(lines[i]):
                old.append(lines[i])
                i += 1
            i += 1
            while i < len(lines) and not _NEW.match(lines[i]):
                new.append(lines[i])
                i += 1
            if i >= len(lines):
                problems.append(f"edit {len(edits) + 1}: the block has no '>>>>>>> NEW' line")
                break
            old_text = "\n".join(old) + "\n"
            new_text = "\n".join(new) + "\n" if new else ""
            if not "".join(old).strip():
                problems.append(f"edit {len(edits) + 1}: the OLD part is empty")
            elif old_text == new_text:
                problems.append(f"edit {len(edits) + 1}: OLD and NEW are the same")
            else:
                edits.append({"path": path, "old": old_text, "new": new_text})
        elif _FILE.match(lines[i]):
            body: list[str] = []
            i += 1
            while i < len(lines) and not _END.match(lines[i]):
                body.append(lines[i])
                i += 1
            if i >= len(lines):
                problems.append("the FILE block has no '>>>>>>> END' line")
                break
            # the path or a marker line written into the content (live 2026-11-19, gpt-oss:
            # "hub/tests/test_x.py" and "=======" as its first lines) is no part of the file
            while body and (
                not body[0].strip()
                or body[0].strip() == path
                or _MID.match(body[0])
                or body[0].strip().startswith("```")
            ):
                body.pop(0)
            while body and (not body[-1].strip() or body[-1].strip().startswith("```")):
                body.pop()  # a code fence around the content (an agent's answer)
            files.append("\n".join(body).strip("\n") + "\n")
        i += 1
    return edits, files, problems


def read_answer(
    answer: str, path: str
) -> tuple[list[dict[str, str]], list[dict[str, str]], list[str]]:
    """``(edits, files, problems)`` of a step's answer: marker blocks (:func:`parse_blocks`) for
    ``path``, else the JSON form of ``issue_fix``."""
    if answer.strip().strip(".").upper() == NOTHING:
        return [], [], []  # the step's work is in the code already (a draft before did it)
    edits, contents, problems = parse_blocks(answer, path)
    if edits or contents or problems:
        if not edits and not contents:
            problems.append("the answer changes nothing")
        return edits, [{"path": path, "content": c} for c in contents], problems
    _, edits, files, problems = parse_fix(answer)
    return edits, files, problems


def anchor_lines(text: str, anchor: str) -> list[int]:
    """The (0-based) lines of ``text`` that hold ``anchor``, compared without the indentation."""
    want = anchor.strip()
    return [i for i, line in enumerate(text.splitlines()) if want and want in line]


def joined_anchor(text: str, anchor: str) -> str | None:
    """A line for an anchor that joins several lines of the file into one (live 2026-11-19,
    gpt-oss: ``start_node(user.uuid, node_id, ...)`` for a call over two lines): compared without
    any whitespace, the anchor stands exactly once in the file; the first line of that span that
    stands once on its own is taken. ``None`` when there is no such place."""
    want = re.sub(r"\s+", "", anchor)
    if len(want) < 12:
        return None
    lines = text.splitlines()
    flat: list[str] = []
    owner: list[int] = []  # the line of each character of the flat text
    for i, line in enumerate(lines):
        bare = re.sub(r"\s+", "", line)
        flat.append(bare)
        owner += [i] * len(bare)
    joined = "".join(flat)
    if joined.count(want) != 1:
        return None
    start = joined.index(want)
    span = range(owner[start], owner[start + len(want) - 1] + 1)
    for i in span:
        line = lines[i].strip()
        if line and len(anchor_lines(text, line)) == 1:
            return line
    return None


CLOSE_ANCHOR = 0.8  # a line this like the anchor, and no other line near it, is the anchor
CLOSE_GAP = 0.15


def _one_close_line(anchor: str, lines: list[str]) -> str | None:
    """The one line of the file that is clearly the anchor the model meant: at least
    :data:`CLOSE_ANCHOR` alike, standing once, and the next best line clearly less alike. ``None``
    when there is no such line or two are alike (then the model is asked)."""
    want = anchor.strip()
    if len(want) < 12:
        return None
    scored = sorted(
        ((difflib.SequenceMatcher(None, want, ln).ratio(), ln) for ln in set(lines) if ln),
        reverse=True,
    )
    if not scored or scored[0][0] < CLOSE_ANCHOR or lines.count(scored[0][1]) != 1:
        return None
    if len(scored) > 1 and scored[0][0] - scored[1][0] < CLOSE_GAP:
        return None
    return scored[0][1]


def _head_line(lines: list[str], sym: code_symbols.Symbol) -> str:
    """The line of a symbol that names it (its ``def``, not a decorator above), stripped."""
    last = sym.name.rsplit(".", 1)[-1].lstrip("#").strip()
    body = [ln.strip() for ln in lines[sym.start : sym.end]]
    return next((ln for ln in body if last and last in ln), next((ln for ln in body if ln), ""))


def settle_symbol(step: Step, text: str) -> str:
    """Resolve a symbol step against the file: its name as the file has it, and its head line as
    the anchor (what the result and the second look show). The problem when it cannot be: no such
    name (with the nearest names, never lines) or several of it."""
    lines = text.splitlines()
    if step.where == "end" and not step.symbol:
        step.anchor = next((ln.strip() for ln in reversed(lines) if ln.strip()), "")
        return ""
    found, near = code_symbols.find(step.path, text, step.symbol)
    if len(found) == 1:
        step.symbol = found[0].name
        step.anchor = _head_line(lines, found[0])
        return ""
    if found:
        names = " | ".join(f.name for f in found)
        many = f"{step.symbol!r} is {len(found)} symbols ({names})"
        return f"step {step.n} ({step.path}): {many}; name one"
    if not near:
        near = [s.name for s in code_symbols.symbols(step.path, text)][: code_symbols.NEAREST]
    return (
        f"step {step.n} ({step.path}): no symbol {step.symbol!r} in the file; the nearest names: "
        + ", ".join(near)
    )


MAX_FREE_NAMES = 9  # test_x_2 … test_x_9


def _numbered(path: str, n: int) -> str:
    """``hub/tests/test_x.py`` → ``hub/tests/test_x_2.py``; ``a/B.test.tsx`` → ``a/B2.test.tsx``."""
    for ext in (".test.tsx", ".test.ts", ".py"):
        if path.endswith(ext):
            stem = path[: -len(ext)]
            return f"{stem}_{n}{ext}" if ext == ".py" else f"{stem}{n}{ext}"
    return f"{path}.{n}"


async def free_test_path(path: str, read: "Read") -> tuple[str, str]:
    """A test file that exists is no refusal (plan step 1.15, live 2026-11-23: three plans were
    refused for it): the hub gives the next free name. Returns ``(path, note)``; the note is ""
    when the file is new, and the path stays when no free name is found (the check says so)."""
    found = await read([path])
    if found.get(path) is None:
        return path, ""
    names = [_numbered(path, n) for n in range(2, MAX_FREE_NAMES + 1)]
    taken = await read(names)
    for name in names:
        if name in taken and taken[name] is None:
            return name, f"{path} gibt es schon: der Test heißt {name}"
    return path, ""


async def settle_test(plan: Plan, raw: dict[str, Any], read: "Read") -> str:
    """:func:`free_test_path` for a plan and the JSON it came from (its ``test.path`` follows)."""
    plan.test_path, note = await free_test_path(plan.test_path, read)
    if note and isinstance(raw.get("test"), dict):
        raw["test"]["path"] = plan.test_path
    return note


def plan_problems(plan: Plan, files: dict[str, str | None]) -> list[str]:
    """What is wrong with the plan held against the live code: a file that is not there, an anchor
    that does not stand in it exactly once, a "new" test file that exists."""
    problems: list[str] = []
    for s in plan.steps:
        if s.path not in files:
            continue  # not read: the step finds out
        text = files[s.path]
        if text is None:
            problems.append(f"step {s.n}: {s.path} does not exist in the live code")
            continue
        if s.symbol or s.where == "end":
            problem = settle_symbol(s, text)
            if problem:
                problems.append(problem)
            continue
        if "\n" in s.anchor:
            # the lines as a block (two lines tell apart a line that stands three times, live
            # 2026-11-19), else the first of them that stands once
            block = joined_anchor(text, s.anchor)
            once = [ln for ln in s.anchor.splitlines() if len(anchor_lines(text, ln)) == 1]
            s.anchor = block or (once[0] if once else s.anchor.splitlines()[0])
        found = anchor_lines(text, s.anchor)
        if not found and (line := joined_anchor(text, s.anchor)) is not None:
            s.anchor, found = line, anchor_lines(text, line)
        stripped = [ln.strip() for ln in text.splitlines()]
        if not found and (line := _one_close_line(s.anchor, stripped)) is not None:
            # the anchor as the model remembered it, and one line of the file is clearly it (live
            # 2026-11-21: `useState(new Set())` for `useState<Set<string>>(new Set())`, and a
            # repair that anchored on the line its red draft had already changed): take that line
            s.anchor, found = line, anchor_lines(text, line)
        if len(found) != 1:
            where = "is not in the file" if not found else f"stands {len(found)} times in it"
            problem = f"step {s.n} ({s.path}): the anchor {s.anchor!r} {where}"
            close = difflib.get_close_matches(s.anchor.strip(), stripped, n=2, cutoff=0.6)
            if not found and close:  # live 2026-11-19: gpt-oss wrote a call it had not read
                problem += "; the file has: " + " | ".join(repr(c) for c in close)
            if found:
                problem += "; give two or three consecutive lines as the anchor so they stand once"
            problems.append(problem)
    if files.get(plan.test_path) is not None:
        problems.append(f"the test file {plan.test_path} exists already; name a new one")
    return problems


def excerpt(text: str, at: int, around: int = AROUND) -> str:
    """The lines around line ``at`` (0-based), headed by where they stand."""
    lines = text.splitlines()
    lo, hi = max(at - around, 0), min(at + around + 1, len(lines))
    return f"(lines {lo + 1}-{hi})\n" + "\n".join(lines[lo:hi])


def settled_into(raw_steps: Any, plan: Plan) -> None:
    """The places as the hub settled them, written back into the plan's JSON steps: the anchor
    line, and a symbol by the name the file has."""
    for st, settled in zip(raw_steps or [], plan.steps, strict=False):
        if isinstance(st, dict):
            st["anchor"] = settled.anchor
            if settled.symbol:
                st["symbol"], st["where"] = settled.symbol, settled.where


def code_at(text: str, step: Step, written: list[str]) -> str:
    """The code a step is shown: a symbol's whole body and :data:`SYMBOL_AROUND` lines (its head
    when it is long), the end of the file, else the lines around the anchor."""
    lines = text.splitlines()
    if step.symbol:
        found, _ = code_symbols.find(step.path, text, step.symbol)
        if len(found) == 1:
            sym = found[0]
            if sym.lines > SYMBOL_SHOWN:
                return excerpt(text, sym.start)
            lo, hi = max(sym.start - SYMBOL_AROUND, 0), min(sym.end + SYMBOL_AROUND, len(lines))
            return f"(lines {lo + 1}-{hi})\n" + "\n".join(lines[lo:hi])
    if step.where == "end" and not step.symbol:
        return excerpt(text, max(len(lines) - 1, 0), around=AROUND // 2)
    return excerpt(text, place_of(text, step, written))


def where_text(step: Step) -> str:
    """Where a step works, in the words of its order."""
    if step.symbol:
        how = {
            "in": "inside",
            "before": "new code right before",
            "after": "new code right after",
            "end": "at the end of the file, after",
        }[step.where]
        return f"{how} `{step.symbol}`"
    if step.where == "end":
        return "at the end of the file"
    return f"at `{step.anchor}`"


def symbol_edits(
    edits: list[dict[str, str]], text: str, path: str
) -> tuple[list[dict[str, str]], list[str]]:
    """``REPLACE SYMBOL`` blocks of an answer as plain edits: the symbol's lines as they stand are
    the old text (nothing to copy, nothing to miscopy). At most :data:`MAX_SYMBOL_LINES` lines."""
    out: list[dict[str, str]] = []
    problems: list[str] = []
    lines = text.splitlines()
    for e in edits:
        if "symbol" not in e:
            out.append(e)
            continue
        found, near = code_symbols.find(path, text, e["symbol"])
        if len(found) != 1:
            why = (
                "is several symbols"
                if found
                else f"is not in the file (nearest: {', '.join(near)})"
            )
            problems.append(f"REPLACE SYMBOL {e['symbol']!r} {why}")
        elif found[0].lines > MAX_SYMBOL_LINES:
            problems.append(
                f"REPLACE SYMBOL {e['symbol']!r}: it has {found[0].lines} lines, more than "
                f"{MAX_SYMBOL_LINES}; change it with OLD/NEW blocks"
            )
        else:
            old = "\n".join(lines[found[0].start : found[0].end]) + "\n"
            if old == e["new"]:
                problems.append(f"REPLACE SYMBOL {e['symbol']!r}: the new text is the same")
            else:
                out.append({"path": path, "old": old, "new": e["new"]})
    return out, problems


def place_of(text: str, step: Step, written: list[str]) -> int:
    """Where a step works in the virtual text: its anchor; when an earlier step replaced that
    line, where that step's new text stands; else the top of the file."""
    found = anchor_lines(text, step.anchor)
    if found:
        return found[0]
    for new in reversed(written):
        head = next((ln.strip() for ln in new.splitlines() if ln.strip()), "")
        hits = anchor_lines(text, head) if head else []
        if hits:
            return hits[0]
    return 0


def edit_diff(edit: dict[str, str]) -> str:
    diff = difflib.unified_diff(
        edit["old"].splitlines(), edit["new"].splitlines(), "vorher", "nachher", lineterm="", n=1
    )
    return f"--- {edit['path']}\n" + "\n".join(list(diff)[2:])


def plan_text(plan: Plan) -> str:
    rows = [f"{s.n}. {s.path} ({where_text(s)}): {s.do}" for s in plan.steps]
    rows.append(f"{len(plan.steps) + 1}. new test {plan.test_path}: {plan.test_do}")
    return f"{plan.summary}\n" + "\n".join(rows)


# --- the steps -----------------------------------------------------------------------------------

# (inputs) -> (answer, model)
Ask = Callable[[str, dict[str, Any]], Awaitable[tuple[str, str]]]
Read = Callable[[list[str]], Awaitable[dict[str, str | None]]]


async def _one_step(
    ask: Ask, inputs: dict[str, Any], check: Callable[[str], tuple[Any, list[str]]]
) -> tuple[Any, list[str], str, int]:
    """A step's run, and once more with the reason when its answer is refused:
    ``(result, problems, model, attempts)``."""
    answer, model = await ask("fix_step", inputs)
    result, problems = check(answer)
    if not problems:
        return result, [], model, 1
    hint = (
        "\nYOUR LAST ANSWER FOR THIS STEP WAS REFUSED: "
        + "; ".join(problems)[:3000]
        + "\nWrite it again; copy old texts verbatim from the code above."
    )
    answer, model = await ask("fix_step", {**inputs, "hint": hint})
    result, problems = check(answer)
    if problems:  # what it answered, so the person sees why (live 2026-11-19: "changes nothing")
        problems = [*problems, f"die letzte Antwort begann mit: {answer.strip()[:600]!r}"]
    return result, problems, model, 2


async def run_steps(
    ask: Ask,
    read: Read,
    question: str,
    plan_answer: str,
    planner: str,
    base: tuple[list[dict[str, str]], list[dict[str, str]]] | None = None,
) -> Outcome:
    """The plan of ``issue_steps`` (``plan_answer``, written by ``planner``) carried out step by
    step. ``ask(program, inputs)`` runs ``issue_steps`` or ``fix_step`` and gives its answer and
    model; ``read(paths)`` the live texts (``None``: no such file).

    ``base``: the edits and files of a draft whose tests were red. A repair builds on it (live
    2026-11-19: the first draft of issue 2.7 had the right code and a wrong test; the repair,
    planned anew from the live code, threw the code away and invented a model and a module). Its
    edits stay at the head of the fix and are applied to the state the steps see; its files stay
    unless the test step writes a file of the same name."""
    out = Outcome()
    base_edits, base_files = base or ([], [])
    base_paths = {e["path"] for e in base_edits}

    async def read_state(paths: set[str]) -> dict[str, str | None]:
        nonlocal base_edits
        live = await read(sorted(paths | base_paths))
        if not base_edits:
            return live
        fitted, misfit, after = check_edits(base_edits, live)
        if misfit:  # the live code moved on under the draft: plan from the live code alone
            out.log.append("Entwurf davor: passt nicht mehr zum Code, neu von vorn")
            base_edits = []
            return live
        base_edits = fitted
        return after

    answer = plan_answer
    repair = bool(base_edits or base_files)

    async def _free_test(p: Plan) -> None:
        # a test file that exists gets a free name, no refusal (1.15)
        p.test_path, note = await free_test_path(p.test_path, read)
        if note:
            out.log.append(note)

    plan, problems = parse_plan(plan_answer, repair=repair)
    files: dict[str, str | None] = {}
    if plan is not None:
        await _free_test(plan)
        files = await read_state({s.path for s in plan.steps} | {plan.test_path})
        problems = plan_problems(plan, files)
    out.log.append(f"Plan ({planner or '?'}): " + ("ok" if not problems else "; ".join(problems)))
    if problems:  # one new plan, told why
        again = (
            f"{question}\n\nDEIN LETZTER PLAN WURDE ABGELEHNT: {'; '.join(problems)}\n"
            "Plane neu; nenne jede Stelle mit ihrem Symbol (Funktion, Klasse.Methode, "
            "Komponente, wie der Hub sie auflistet), nie mit einer Zeilennummer."
        )
        answer, planner = await ask("issue_steps", {"question": again})
        plan, problems = parse_plan(answer, repair=repair)
        if plan is not None:
            await _free_test(plan)
            files = await read_state({s.path for s in plan.steps} | {plan.test_path})
            problems = plan_problems(plan, files)
        out.log.append(
            f"Plan, 2. Versuch ({planner or '?'}): "
            + ("ok" if not problems else "; ".join(problems))
        )
    if plan is None or problems:
        out.failed = "der Plan passt nicht zum Code: " + "; ".join(problems)
        if plan is None:  # what it answered instead (live 2026-11-19: "the plan has no steps")
            out.failed += f"; die letzte Antwort begann mit: {answer.strip()[:600]!r}"
        return out
    out.summary = plan.summary
    out.edits = list(base_edits)
    if base_edits:
        out.log.append(f"Entwurf davor: {len(base_edits)} Edits bleiben, darauf aufgebaut")
    state = dict(files)
    written: dict[str, list[str]] = {}
    overview = plan_text(plan)
    task = question[-TASK_CHARS:]

    def done_text() -> str:
        text = "\n\n".join(edit_diff(e) for e in out.edits) or "(nothing yet: yours is the first)"
        return text[-DONE_CHARS:]

    total = len(plan.steps) + 1
    for step in plan.steps:
        text = state.get(step.path) or ""
        code = code_at(text, step, written.get(step.path, []))
        inputs = {
            "task": task,
            "plan": overview,
            "step": f"Step {step.n} of {total}: {step.path}, {where_text(step)}\n{step.do}",
            "code": f"### {step.path} {code}",
            "done": done_text(),
            "hint": "",
        }
        first = len(out.edits) + 1
        now = state

        def check(
            answer: str, step: Step = step, first: int = first, now: dict[str, Any] = now
        ) -> tuple[Any, list[str]]:
            edits, new_files, bad = read_answer(answer, step.path)
            if new_files:
                bad.append("a step of an existing file gives edits, no files")
            off = [e["path"] for e in edits if e["path"] != step.path]
            if off:
                bad.append(f"the edits must change {step.path} only, not {', '.join(off)}")
            if bad:
                return None, bad
            edits, bad = symbol_edits(edits, now.get(step.path) or "", step.path)
            if bad:
                return None, bad
            fitted, misfit, after = check_edits(edits, now, first=first)
            return (fitted, after), misfit

        result, bad, model, tries = await _one_step(ask, inputs, check)
        if bad:
            out.log.append(f"Schritt {step.n} ({step.path}, {model or '?'}): abgelehnt")
            out.failed = f"Schritt {step.n} ({step.path}) passte zweimal nicht: " + "; ".join(bad)
            return out
        fitted, state = result
        out.edits += fitted
        written.setdefault(step.path, []).extend(e["new"] for e in fitted)
        note = "ok" if tries == 1 else "ok im 2. Versuch"
        if not fitted:
            note = "nichts zu tun (steht schon im Code)"
        out.log.append(f"Schritt {step.n} ({step.path}, {model or '?'}): {note}")

    inputs = {
        "task": task,
        "plan": overview,
        "step": (
            f"Step {total} of {total}: the NEW test file {plan.test_path}\n{plan.test_do}\n"
            + (
                'A vitest file of the GUI: import { describe, expect, it } from "vitest", '
                "render with @testing-library/react, import the changed module by its relative "
                "path; test what the steps changed. The workshop runs ESLint first: never the "
                "type any (cast with `as unknown as T`, take Record<string, unknown> for plain "
                "objects; live 2026-11-21 a test failed for one `any`)."
                if plan.test_path.startswith("web/")
                else "Import from topcoat_hub, plain pytest functions; test what the steps changed."
            )
        ),
        "code": "(a new file: the changes it tests are above)",
        "done": done_text(),
        "hint": "",
    }

    def check_test(answer: str) -> tuple[Any, list[str]]:
        edits, new_files, bad = read_answer(answer, plan.test_path)
        mine = [f for f in new_files if f["path"] == plan.test_path]
        if edits or len(mine) != 1 or len(new_files) != 1:
            bad.append(f"the test step gives exactly one file, {plan.test_path}, and no edits")
        return mine, bad

    test, bad, model, tries = await _one_step(ask, inputs, check_test)
    if bad:
        out.log.append(f"Test ({plan.test_path}, {model or '?'}): abgelehnt")
        out.failed = "der Test passte zweimal nicht: " + "; ".join(bad)
        return out
    out.files = [f for f in base_files if f["path"] != plan.test_path] + test
    note = "ok" if tries == 1 else "ok im 2. Versuch"
    out.log.append(f"Test ({plan.test_path}, {model or '?'}): {note}")
    return out


def as_draft(out: Outcome) -> str:
    """The outcome in the form of an ``issue_fix`` answer, for the draft path of project_runs."""
    return json.dumps(
        {"summary": out.summary, "edits": out.edits, "files": out.files}, ensure_ascii=False
    )
