"""An issue's investigation in rounds, ending in a report and a plan (2026-11-19).

The maintainer: "Fix entwerfen" is first a gathering of what is known - what exactly the problem
is, where in the code the cause could lie, a proposed fix - and the project's agent may work with
Development and Verwaltung for as many rounds as it needs. Once the issue has a settled plan (the
person's click "Plan umsetzen"), it is carried out step by step (fix_steps.py).

A round: the lead (``investigate_round``, one model call that reads no code itself) sees the
problem and everything found so far and either asks up to three questions - each answered by a
fresh agent run in Development (``probe_dev``, reads the live code) or Verwaltung
(``probe_admin``, the running system) - or writes the report with its plan. The hub holds the
plan against the live code (fix_steps.plan_problems); a plan that does not fit goes back to the
lead as the next round, not to the person. The rounds end at :data:`MAX_ROUNDS`; the last one is
told to write the report. The lead's context stays free of the reading: it gets answers, not the
runs' tool calls."""

import difflib
import json
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from topcoat_hub import code_symbols
from topcoat_hub.blocks.builtin import MAX_PROBES
from topcoat_hub.fix_steps import (
    anchor_lines,
    parse_plan,
    plan_problems,
    settle_test,
    settled_into,
    tests_out_of_steps,
)
from topcoat_hub.issue_fixes import _json_object

MAX_ROUNDS = 6
# Rounds after MAX_ROUNDS that only exist while the hub refuses a written plan: the lead corrects
# it with the lines the hub names (live 2026-11-21: issue 2.10 found the right places, but its one
# plan came in round 6 with a paraphrased anchor, and nothing was left to repair it).
MAX_REPAIRS = 3
REPAIR_HINT = (
    "\nREPAIR ROUND {n} OF {total}: your plan was refused (see above). Write the report again with "
    'the plan corrected: name every place by a symbol the hub lists ("the nearest names: …"), '
    "never by a line number. A step changes a file that exists; a test file that does not exist "
    "yet "
    'is never a step, it is the plan\'s "test" (a new file). Ask a question only if a line you '
    "need is not shown."
)
FORCE_HINT = (
    "\nWRITE THE REPORT NOW. No more questions: they are not answered any more. The places are in "
    "the findings. What the issue asks for does not exist in the code yet - that is the cause; "
    "plan the NEW code at the places found (each named by its symbol: function, Class.method, "
    'component; new code with "where": "new" and "near": an existing symbol). If an earlier '
    "plan is in the findings, correct it instead of starting over."
)
WIKI_CHARS = 6000  # of the wiki paragraphs that name what the issue names, at the start
MAX_REVIEWS = 2  # second looks at a plan; after that a plan that fits the code is taken
AROUND = 25  # lines before and after an anchor that the second look reads
REVIEW_CODE_CHARS = 12_000
SAME_QUESTION = 0.8  # a question this close to one asked before is not sent again
ANSWER_CHARS = 4000  # of one probe's answer, in the findings
ANSWER_SHOWN = 1500  # of one probe's answer, in the report
FINDINGS_CHARS = 40_000  # the findings the lead reads in one round (the newest are kept)
PROBES = {"dev": "probe_dev", "admin": "probe_admin"}
WHO = {"dev": "Dev", "admin": "Verwaltung", "try": "Versuch"}
TRY_HINT = (
    '\nTRIES ARE ALLOWED (the person\'s probe mandate): a question with "to": "try" names a '
    "guess; a small test of the real code is written for it and run in the workshop, and its "
    "output comes back. Try a guess before you call it the cause; one try per guess."
)
CODE_SHOWN = 3000  # of a try's test, in the findings
# a try that did not run: its test could not be collected (an import, a name, the syntax)
_BROKEN = re.compile(
    r"ERROR collecting|ImportError|ModuleNotFoundError|SyntaxError|IndentationError|"
    r"NameError: name '\w+' is not defined|fixture '\w+' not found|"
    # a call with the wrong arguments tried the test's guess of the signature, not the code
    r"TypeError: \w+\(\) (?:takes \d+|missing \d+ required|got an unexpected keyword)"
)
CHECK = (
    "Check the suspected cause with the real code: {cause}\nThe test passes only when the cause "
    "holds; show the values the code gives with the problem's exact input."
)
REPRODUCE = (
    "Reproduce the problem exactly as the problem text describes it, with the same input (copy "
    "it character for character, quotes included). Call the code the input passes through, one "
    "step after the other (e.g. reading or parsing the input, the values it makes of it, the "
    "condition or result it builds), and show what each step gives."
)

# (program, inputs) -> (answer, model)
Ask = Callable[[str, dict[str, Any]], Awaitable[tuple[str, str]]]
# (number of the try, content of its test file) -> the output of its run, or why it did not run
Try = Callable[[int, str], Awaitable[str]]
Read = Callable[[list[str]], Awaitable[dict[str, str | None]]]
# (a path or folder) -> the files of the workshop's ``repo.outline`` there (plan step 1.11), each
# ``{path, lines, symbols: [{name, kind, start, end}], symbols_left?}``; [] when the node has none
Outline = Callable[[str], Awaitable[list[dict[str, Any]]]]


@dataclass
class Investigation:
    text: str = ""  # the report as the person reads it (Markdown, German)
    plan: dict[str, Any] | None = None  # the plan in the form of issue_steps, checked
    log: list[str] = field(default_factory=list)
    # every question with the start of its answer, for the person (live 2026-11-19: the report
    # listed only the questions, nobody saw why one was asked four times)
    answers: list[str] = field(default_factory=list)
    failed: str = ""
    # the issue as the lead understood it: a short title and what is true once it is fixed
    # (apply_brief in project_issues.py writes them into the issue)
    title: str = ""
    checks: list[str] = field(default_factory=list)
    # the last plan the hub refused, as written: the issue's next investigation starts from it
    draft: dict[str, Any] | None = None


def _findings_text(findings: list[str]) -> str:
    text = "\n\n".join(findings)
    if len(text) <= FINDINGS_CHARS:
        return text
    # the start (what was read first) and the newest rounds
    head = findings[0][: FINDINGS_CHARS // 4]
    tail = text[-(FINDINGS_CHARS - len(head) - 20) :]
    return f"{head}\n\n[…]\n\n{tail}"


def report_text(report: dict[str, Any], plan: dict[str, Any] | None, problems: list[str]) -> str:
    """The report in the sections the person reads; the plan as a numbered list."""

    def s(key: str) -> str:
        return str(report.get(key) or "").strip() or "(keine Angabe)"

    parts = [
        f"## Problem\n{s('problem')}",
        *(
            ["## Prüfpunkte\n" + "\n".join(f"- {c}" for c in report["checks"])]
            if isinstance(report.get("checks"), list) and report["checks"]
            else []
        ),
        f"## Ursache\n{s('cause')}",
        "## Belege\n" + ("\n".join(f"- `{e}`" for e in report.get("evidence") or []) or "-"),
        f"## Vorgeschlagener Fix\n{s('fix')}",
    ]
    steps = report.get("steps") or []
    rows = [
        f"{i}. `{st.get('path')}` {_place(st, 'bei')}: {st.get('do')}"
        for i, st in enumerate(steps, 1)
        if isinstance(st, dict)
    ]
    test = report.get("test") if isinstance(report.get("test"), dict) else {}
    rows.append(f"{len(rows) + 1}. Test, neue Datei `{test.get('path')}`: {test.get('do')}")
    head = "## Lösungsplan" + ("" if plan else " (passt nicht zum Code)")
    parts.append(head + "\n" + "\n".join(rows))
    if problems:
        parts.append("Der Hub hat den Plan abgelehnt: " + "; ".join(problems))
    return "\n\n".join(parts)


_FILE = re.compile(r"\b([\w-]+)\.(?:tsx?|py|go|css)\b")
_CODE = re.compile(r"`([A-Za-z_][A-Za-z0-9_]{3,40})")
_GENERIC = frozenset({"const", "return", "import", "export", "function", "useState", "useEffect"})


# The window an issue was filed from ("Fenster: project" in the hub's context of the issue) and the
# wiki page that describes that window's GUI (live 2026-11-21: "die Baum-Ansicht" with no file
# named; the planner took the tag tree of the memory and invented
# web/src/components/NodeDetail.tsx).
WINDOW_PAGES = {
    "project": "Projekte-Oberfläche",
    "chat": "Chat-Oberfläche",
    "wiki": "Wiki und Recherche",
    "research": "Wiki und Recherche",
    "inbox": "Posteingang",
    "desktop": "Desktop und Themes",
    "settings": "Einstellungen",
}
_WINDOW = re.compile(r"^\s*-?\s*Fenster:\s*([a-z0-9-]+)", re.M)


def window_page(text: str) -> str | None:
    """The title fragment of the wiki page of the window the issue was filed from, or None."""
    found = _WINDOW.search(text or "")
    return WINDOW_PAGES.get(found.group(1)) if found else None


def wiki_terms(text: str) -> set[str]:
    """The names a problem text and its findings point at: file names (with and without their
    ending) and identifiers in backticks."""
    terms = {m.group(0) for m in _FILE.finditer(text)} | {m.group(1) for m in _FILE.finditer(text)}
    terms |= {m.group(1) for m in _CODE.finditer(text)}
    return {t for t in terms if t not in _GENERIC and len(t) >= 4}


def wiki_excerpt(pages: list[tuple[str, str]], text: str, limit: int = WIKI_CHARS) -> str:
    """The paragraphs of the wiki pages (path, body) that name what ``text`` names, the best page
    first, each paragraph under its page (live 2026-11-21, issue 2.10: the page of the Projekte GUI
    said what the planner got wrong nine times - ``found`` is the filter search, ``visibleNodes``
    builds the rows of ``ListView`` - and no run of the investigation read it)."""
    terms = wiki_terms(text)
    window = window_page(text)
    if not terms and not window:
        return ""
    scored: list[tuple[int, int, str, str]] = []
    for order, (path, body) in enumerate(pages):
        # every paragraph of the window's own page counts, the more it names the more
        own = 1 if window and window in path else 0
        for para in re.split(r"\n\s*\n", body or ""):
            hits = sum(1 for t in terms if t in para) + own
            if hits:
                scored.append((hits, -order, path, para.strip()))
    if not scored:
        return ""
    by_page: dict[str, int] = {}
    for hits, _o, path, _p in scored:
        by_page[path] = by_page.get(path, 0) + hits
    scored.sort(key=lambda s: (by_page[s[2]], s[0]), reverse=True)
    out: list[str] = []
    size = 0
    seen_page = ""
    for _hits, _o, path, para in scored:
        piece = (f"### {path}\n" if path != seen_page else "") + para
        if size + len(piece) > limit:
            break
        out.append(piece)
        size += len(piece) + 2
        seen_page = path
    return "\n\n".join(out)


def plan_text(raw: dict[str, Any]) -> str:
    """The plan as the second look reads it: numbered steps with anchor and change, then the
    test."""
    rows = [
        f"{i}. {st.get('path')} {_place(st, 'at')}: {st.get('do')}"
        for i, st in enumerate(raw.get("steps") or [], 1)
        if isinstance(st, dict)
    ]
    test = raw.get("test") if isinstance(raw.get("test"), dict) else {}
    rows.append(f"Test (new file {test.get('path')}): {test.get('do')}")
    return "\n".join(rows)


def _place(st: dict[str, Any], at: str) -> str:
    """Where a step of the plan's JSON works: its symbol (1.10) or its anchor line."""
    symbol = str(st.get("symbol") or "").strip()
    if symbol:
        return f"{st.get('where') or 'in'} `{symbol}`"
    if st.get("where") == "end":
        return "end"
    return f"{at} `{st.get('anchor')}`"


def around_anchors(plan: Any, files: dict[str, str | None]) -> str:
    """The code at each step (numbered lines), for the second look: a symbol's body, else the
    lines around its anchor."""
    parts: list[str] = []
    for s in plan.steps:
        text = files.get(s.path) or ""
        lines = text.splitlines()
        span = None
        if s.symbol:
            found_symbols, _ = code_symbols.find(s.path, text, s.symbol)
            if len(found_symbols) == 1 and found_symbols[0].lines <= 2 * AROUND:
                sym = found_symbols[0]
                span = (max(sym.start - 5, 0), min(sym.end + 5, len(lines)))
        found = anchor_lines(text, s.anchor.splitlines()[0] if s.anchor else "")
        if span is None and not found:
            continue
        lo, hi = span or (max(found[0] - AROUND, 0), min(found[0] + AROUND + 1, len(lines)))
        body = "\n".join(f"{n + 1}: {lines[n]}" for n in range(lo, hi))
        parts.append(f"### step {s.n}: {s.path}, lines {lo + 1}-{hi}\n{body}")
    return "\n\n".join(parts)[:REVIEW_CODE_CHARS]


# --- round 0: the area (plan step 1.15) -------------------------------------------------------

# what a fix may change (issue_fixes.ALLOWED) and the parts that only Claude Code changes
INSIDE = ("hub/", "proto/", "docs/", "web/src/")
OUTSIDE = ("desktop/", "node/")
AREA_FINDINGS_CHARS = 12_000
OUTSIDE_TEXT = (
    "**Untersuchung beendet: der Bereich liegt außerhalb der Fix-Pipeline.**\n\n"
    "Das Issue betrifft {part} ({files}). Entwürfe des Hubs dürfen nur hub/, proto/, docs/ und "
    "web/src/ ändern; {part} baut und testet die Werkstatt nicht. Das ist ein Fall für Claude "
    "Code. Liegt die Ursache doch im Hub oder in der GUI, sag es in der Rückmeldung (mit einer "
    "Datei oder einem Namen), dann untersucht die nächste Iteration dort."
)


_REPO_PATH = re.compile(r"(?<![\w/.-])((?:hub|web/src|proto|docs)/[\w./-]+\.\w+)")


def inside_paths(text: str) -> list[str]:
    """The files where a fix may change that a text names (search hits, wiki excerpts), in order."""
    return list(dict.fromkeys(m.group(1) for m in _REPO_PATH.finditer(text)))


def outside_part(files: list[str], existing: dict[str, str | None]) -> str:
    """The part outside the pipeline (``desktop/`` or ``node/``) an area points to, or "": only
    when a named file stands there in the code and no named file lies where a fix may change
    (a guess of the model alone stops nothing)."""
    if any(f.startswith(INSIDE) for f in files):
        return ""
    real = [f for f in files if f.startswith(OUTSIDE) and existing.get(f) is not None]
    return real[0].split("/", 1)[0] + "/" if real else ""


# the map of the files the start found (repo.outline, plan step 1.11): their names and lines, so
# round 0 and the rounds after it name symbols that exist instead of guessing them
OUTLINE_FILES = 4
OUTLINE_CHARS = 6000
_ANY_PATH = re.compile(r"(?<![\w/.-])((?:hub|web/src|proto|docs|desktop|node)/[\w./-]+\.\w+)")


def named_files(text: str, limit: int = OUTLINE_FILES) -> list[str]:
    """The repository files a text names, the most named first (ties in order of appearance)."""
    found = [m.group(1) for m in _ANY_PATH.finditer(text)]
    order = list(dict.fromkeys(found))
    return sorted(order, key=lambda f: -found.count(f))[:limit]


def outline_text(files: list[dict[str, Any]], limit: int = OUTLINE_CHARS) -> str:
    """The outline of files as the models read it: one line a file, its names with their lines."""
    rows: list[str] = []
    size = 0
    for f in files:
        names = [
            f"{s.get('name')} [{s.get('start')}-{s.get('end')}]"
            for s in f.get("symbols") or []
            if isinstance(s, dict) and s.get("name")
        ]
        if left := f.get("symbols_left"):
            names.append(f"… {left} weitere")
        row = f"- `{f.get('path')}` ({f.get('lines') or 0} Zeilen): " + (", ".join(names) or "-")
        if size + len(row) > limit:
            rows.append("- …")
            break
        rows.append(row)
        size += len(row) + 1
    return "\n".join(rows)


async def _outline_findings(outline: Outline, findings: list[str]) -> str:
    """The outline of the files the findings name, or "" (no files, an old node, an error)."""
    files: list[dict[str, Any]] = []
    for path in named_files(_findings_text(findings)):
        try:
            files += [f for f in await outline(path) if isinstance(f, dict)]
        except Exception:  # the map is a help, never a reason for the investigation to fail
            return ""
    return outline_text(files)


async def _area_round(
    ask: Ask,
    read: Read,
    question: str,
    findings: list[str],
    out: Investigation,
    outline: Outline | None = None,
) -> tuple[str, list[str]] | None:
    """Round 0: the area, files and names before any question; they go into the findings. Returns
    ``(part, files)`` when the issue lies outside the pipeline, else ``None``. With ``outline``,
    the names and lines of the files the start found go into the findings first."""
    if outline is not None and (mapped := await _outline_findings(outline, findings)):
        findings.append(
            "## Übersicht der gefundenen Dateien (repo.outline: Namen und Zeilen)\n" + mapped
        )
        out.log.append("Runde 0: Übersicht der gefundenen Dateien gelesen")
    answer, who = await ask(
        "issue_area",
        {"question": question, "findings": _findings_text(findings)[-AREA_FINDINGS_CHARS:]},
    )
    doc = _json_object(answer or "") or {}
    area = str(doc.get("area") or "unknown")
    files = [str(f).strip().lstrip("./") for f in doc.get("files") or [] if str(f).strip()][:3]
    names = [str(s).strip() for s in doc.get("symbols") or [] if str(s).strip()][:3]
    if not doc:
        out.log.append(f"Runde 0 ({who or '?'}): kein Bereich bestimmt")
        return None
    head = f"Runde 0 ({who or '?'}): Bereich {area}" + (f": {', '.join(files)}" if files else "")
    found_inside = inside_paths(_findings_text(findings))
    if area in ("desktop", "node") and found_inside:
        # live 2026-11-25 (3.2.3.2, "Sheets im Chat"): gpt-oss named desktop/…/overlay.rs and the
        # investigation stopped, while the issue was the hub's. What was read at the start decides:
        # with hits where a fix may change, the guess is dropped and the rounds start there
        out.log.append(f"{head}; verworfen, die Funde zeigen {', '.join(found_inside[:3])}")
        return None
    lines = [f"- Bereich: {area}"]
    lines += [f"- Datei: `{f}`" for f in files] + [f"- Name: `{n}`" for n in names]
    if doc.get("why"):
        lines.append(f"- Warum: {' '.join(str(doc['why']).split())[:400]}")
    findings.append("## Runde 0: Bereich (lies hier zuerst)\n" + "\n".join(lines))
    out.log.append(head)
    if area not in ("desktop", "node"):
        return None
    part = outside_part(files, await read(files) if files else {})
    return (part, files) if part else None


def _outside(out: Investigation, stop: tuple[str, list[str]], findings: list[str]) -> Investigation:
    part, files = stop
    out.failed = f"der Bereich liegt in {part}: außerhalb der Fix-Pipeline (Claude Code)"
    out.text = OUTSIDE_TEXT.format(part=part, files=", ".join(f"`{f}`" for f in files))
    return out


async def investigate(
    ask: Ask,
    read: Read,
    question: str,
    start: str,
    try_run: Try | None = None,
    review: bool = False,
    outline: Outline | None = None,
) -> Investigation:
    """The rounds of an issue's investigation; ``start`` is what ``issue_investigate`` read.
    ``review``: a plan that fits the code gets the second look (``review_plan``) before it is
    taken; a refusal of it is a refused plan like any other.
    ``try_run``: the workshop tries of the person's probe mandate (issue_probes.py), or ``None``
    when there is none (then a question to "try" goes to Development).
    ``outline``: the workshop's map of a file (``repo.outline``) for round 0, or ``None``."""
    out = Investigation()
    findings = [f"## Zu Beginn gelesen (Code, live)\n{start.strip() or '(nichts)'}"]
    stop = await _area_round(ask, read, question, findings, out, outline)
    if stop:
        return _outside(out, stop, findings)
    hint = ""
    seen: set[str] = set()
    tries = 0
    checked = False
    report: dict[str, Any] | None = None
    problems: list[str] = []
    if try_run is not None:
        # the first try is the hub's, not the lead's (live 2026-11-19: with tries allowed, the
        # lead asked Dev and Verwaltung for six rounds and never tried once): the problem as it
        # was described, with what the code gives at each step it passes
        reply, who = await _try(ask, try_run, question, findings, REPRODUCE, 1, read)
        tries = 1
        findings.append(f"## Zu Beginn, Versuch: das Problem nachgestellt\n{reply[:ANSWER_CHARS]}")
        out.log.append(f"Zu Beginn, Versuch ({who or '?'}): das Problem nachgestellt")
        out.answers.append(f"**Zu Beginn, Versuch:** {REPRODUCE}\n\n{reply[: ANSWER_SHOWN * 2]}")
    rnd = 0
    repairs = 0
    reviews = 0
    must_report = False  # questions are over: only a report is taken
    while True:
        rnd += 1
        if rnd > MAX_ROUNDS:
            # only a refused plan, or no report at all, earns more rounds, and only a few
            if not (problems or report is None) or repairs >= MAX_REPAIRS:
                break
            repairs += 1
            if report is None:
                must_report = True
            else:
                hint += REPAIR_HINT.format(n=repairs, total=MAX_REPAIRS)
        if try_run is not None and not must_report:
            hint += TRY_HINT
        if must_report:
            hint += FORCE_HINT
        elif rnd == MAX_ROUNDS:
            hint += "\nTHIS IS THE LAST ROUND: write the report now, with what is known."
        answer, model = await ask(
            "investigate_round",
            {"question": question, "findings": _findings_text(findings), "hint": hint},
        )
        hint = ""
        doc = _json_object(answer or "") or {}
        asked = [q for q in doc.get("questions") or [] if isinstance(q, dict)][:MAX_PROBES]
        if isinstance(doc.get("report"), dict):
            report = doc["report"]
            must_report = False
            cause = " ".join(str(report.get("cause") or "").split())
            if try_run is not None and not checked and cause and rnd < MAX_ROUNDS:
                # the lead does not try on its own (live 2026-11-19): the hub tries its cause once
                # and gives it a round to keep or correct the report
                checked = True
                guess = CHECK.format(cause=cause[:1500])
                reply, who = await _try(ask, try_run, question, findings, guess, tries + 1, read)
                tries += 1
                head = f"## Runde {rnd}, Versuch: die vermutete Ursache geprüft"
                findings.append(f"{head}\n{reply[:ANSWER_CHARS]}")
                out.log.append(
                    f"Runde {rnd}, Versuch ({who or '?'}): die vermutete Ursache geprüft"
                )
                out.answers.append(
                    f"**Runde {rnd}, Versuch:** {guess}\n\n{reply[: ANSWER_SHOWN * 2]}"
                )
                hint = (
                    "YOUR SUSPECTED CAUSE WAS TRIED WITH THE REAL CODE (see the last finding). "
                    "Write the report again if the try supports it; if it does not, ask further "
                    "or write the cause the try shows."
                )
                report = None
                continue
            # a test file among the steps is the plan's test (the hub moves it, no round lost)
            raw = tests_out_of_steps({k: report.get(k) for k in ("summary", "steps", "test")})
            report["steps"], report["test"] = raw.get("steps"), raw.get("test")
            # a plan whose steps all lie in desktop/ or node/ ends here: repair rounds cannot move
            # it into the pipeline (live 2026-11-23, 3.2.3.2.1: steps in desktop/src-tauri)
            paths = [
                str(s.get("path") or "") for s in raw.get("steps") or [] if isinstance(s, dict)
            ]
            if paths and all(p.startswith(OUTSIDE) for p in paths):
                part = outside_part(paths, await read(sorted(set(paths))))
                if part:
                    out.log.append(f"Runde {rnd} ({model or '?'}): Plan nur in {part}")
                    return _outside(out, (part, paths[:3]), findings)
            plan, problems = parse_plan(json.dumps(raw))
            if plan is not None:
                # a test file that exists gets a free name, no refused round (1.15)
                if note := await settle_test(plan, raw, read):
                    report["test"] = raw.get("test")
                    out.log.append(f"Runde {rnd}: {note}")
                files = await read(sorted({s.path for s in plan.steps} | {plan.test_path}))
                problems = plan_problems(plan, files)
                # the places as the hub settled them (a symbol's name, a line of a block)
                settled_into(raw.get("steps"), plan)
            who = "Plan abgelehnt"
            if not problems and plan is not None and review and reviews < MAX_REVIEWS:
                # the anchors stand in the code; whether the places serve the checks is the
                # second look's question (another model, the code around each anchor)
                reviews += 1
                verdict, reviewer = await ask(
                    "review_plan",
                    {
                        "question": question[-6000:]
                        + (
                            "\n\nChecks:\n"
                            + "\n".join(f"- {c}" for c in report.get("checks") or [])
                        ),
                        "plan": plan_text(raw),
                        "code": around_anchors(plan, files),
                    },
                )
                said = _json_object(verdict or "") or {}
                found = [str(p) for p in said.get("problems") or [] if str(p).strip()]
                if said.get("ok") is False and found:
                    problems = found[:6]
                    who = f"Zweitcheck ({reviewer or '?'}) lehnt den Plan ab"
                else:
                    out.log.append(
                        f"Runde {rnd}, Zweitcheck ({reviewer or '?'}): "
                        + ("Plan passt" if said.get("ok") is True else "keine klare Antwort")
                    )
            if not problems:
                out.plan = raw
                out.log.append(f"Runde {rnd} ({model or '?'}): Bericht und Plan")
                break
            out.draft = raw  # the next investigation of the issue starts from it
            out.log.append(f"Runde {rnd} ({model or '?'}): {who}: " + "; ".join(problems))
            hint = (
                "YOUR PLAN WAS REFUSED BY THE HUB: " + "; ".join(problems)[:3000] + "\nAsk Dev "
                "for the exact lines, or correct the plan."
            )
            findings.append(f"## Runde {rnd}: Plan abgelehnt\n" + "\n".join(problems))
            continue
        if not asked:
            out.log.append(f"Runde {rnd} ({model or '?'}): keine Fragen, kein Bericht")
            hint = (
                "YOUR LAST ANSWER WAS NEITHER QUESTIONS NOR A REPORT. Answer with the JSON object "
                f"asked for. It began: {(answer or '').strip()[:300]!r}"
            )
            continue
        if must_report or rnd >= MAX_ROUNDS:
            # questions where only a report counts are not answered (live 2026-11-21: the last
            # round asked again and the investigation ended without any report)
            out.log.append(f"Runde {rnd} ({model or '?'}): Fragen statt Bericht, nicht beantwortet")
            must_report = True
            continue
        repeated: list[str] = []
        sent = 0
        for q in asked:
            to = str(q.get("to") or "dev").strip().lower()
            to = to if to in PROBES or (to == "try" and try_run is not None) else "dev"
            text = str(q.get("question") or "").strip()
            if not text:
                continue
            key = " ".join(text.casefold().split())
            # live 2026-11-19: one question four times, the same answer each time; live 2026-11-21:
            # the same question in other words four rounds long
            if key in seen or any(
                difflib.SequenceMatcher(None, key, s).ratio() >= SAME_QUESTION for s in seen
            ):
                repeated.append(text)
                continue
            seen.add(key)
            sent += 1
            if to == "try" and try_run is not None:
                reply, who = await _try(ask, try_run, question, findings, text, tries + 1, read)
                tries += 1
            else:
                reply, who = await ask(PROBES[to], {"task": question[-6000:], "question": text})
            reply = reply.strip() or "(keine Antwort)"
            findings.append(f"## Runde {rnd}, {WHO[to]}: {text}\n{reply[:ANSWER_CHARS]}")
            out.log.append(f"Runde {rnd}, {WHO[to]} ({who or '?'}): {text[:200]}")
            out.answers.append(
                f"**Runde {rnd}, {WHO[to]}:** {text}\n\n{reply[:ANSWER_SHOWN]}"
                + ("…" if len(reply) > ANSWER_SHOWN else "")
            )
        if repeated:
            hint = (
                "THESE QUESTIONS WERE ASKED BEFORE (their answers are in the findings): "
                + " | ".join(repeated)[:1000]
                + "\nAsk what those answers left open, or about the step before."
            )
        if repeated and not sent:
            # nothing new was asked: what can be found is found (live 2026-11-21, issue 2.10: the
            # lead asked for the code the fix is to write, four rounds long)
            out.log.append(f"Runde {rnd} ({model or '?'}): nur Wiederholungen")
            must_report = True
    if report is None:
        out.failed = f"nach {MAX_ROUNDS} Runden kein Bericht"
        out.text = "**Untersuchung ohne Ergebnis.**\n\n" + _findings_text(findings)[-8000:]
        return out
    out.text = report_text(report, out.plan, [] if out.plan else problems)
    out.title = " ".join(str(report.get("title") or "").split())
    out.checks = [str(c) for c in report.get("checks") or [] if isinstance(c, str) and c.strip()]
    return out


HUB_SRC = "hub/src/topcoat_hub/"
_MODULE_WORD = re.compile(
    r"\b(?:topcoat_hub[./])?((?:[a-z][a-z0-9_]*/)?[a-z][a-z0-9_]{2,40})(?:\.py)?\b"
)
_DEF = re.compile(r"^(?:async def|def|class) \w+.*$", re.M)
_METHOD = re.compile(r"^    (?:async def|def) (?:__init__|[a-z]\w*)\(")
MAX_MODULES = 4
SIGNATURE_LINES = 8


def signatures(source: str) -> list[str]:
    """Every top-level def and class of a module with its whole signature, joined to one line (live
    2026-11-19: only the first line "def compile_filter(" reached the writer, which guessed three
    positional arguments for a function that takes one)."""
    lines = source.splitlines()
    out: list[str] = []
    in_class = False
    for i, line in enumerate(lines):
        top = _DEF.match(line)
        if top:
            in_class = line.startswith("class ")
        elif not (in_class and _METHOD.match(line)):
            # the public methods of a class too (live 2026-11-19: ProjectStore.search_nodes was
            # missing, the writer imported it as a function of the module)
            if line and not line[0].isspace():
                in_class = False
            continue
        sig = [line.rstrip() if not top else line.strip()]
        k = i
        while (
            not sig[-1].rstrip().endswith(":") and k + 1 < len(lines) and len(sig) < SIGNATURE_LINES
        ):
            k += 1
            sig.append(lines[k].strip())
        out.append(" ".join(sig))
    return out


OUTLINE_LINES = 120


async def module_outline(read: Read, text: str) -> tuple[list[str], str]:
    """The hub's own reading of the modules a text names: ``(module names, outline)``, each module
    with its import path and every def/class line, read from the live code (not from a model)."""
    words = [
        m.group(1) for m in _MODULE_WORD.finditer(text) if "_" in m.group(1) or "/" in m.group(1)
    ]
    candidates = list(dict.fromkeys(words))[:15]
    if not candidates:
        return [], ""
    files = await read([f"{HUB_SRC}{w}.py" for w in candidates])
    names: list[str] = []
    parts: list[str] = []
    for w in candidates:
        source = files.get(f"{HUB_SRC}{w}.py")
        if not source:
            continue
        names.append(w)
        defs = signatures(source)[:OUTLINE_LINES]
        parts.append(f"# topcoat_hub.{w.replace('/', '.')}\n" + "\n".join(defs))
        if len(names) >= MAX_MODULES:
            break
    return names, "\n\n".join(parts)


_FENCE = re.compile(r"```(?:python|py)?\s*\n(.*?)```", re.S)


def bare_test(answer: str) -> str:
    """A test written without the FILE markers: the code block that holds a test function, or the
    whole answer when it is plain code; "" when there is no test in it."""
    for block in _FENCE.findall(answer or ""):
        if "def test" in block:
            return block.strip("\n") + "\n"
    text = (answer or "").strip()
    if text.startswith(("import ", "from ", "def test")) and "def test" in text:
        return text + "\n"
    return ""


def fix_imports(content: str, modules: list[str]) -> str:
    """A hub module imported without its package (live 2026-11-19: ``from project_filter import
    compile_filter``) gets ``topcoat_hub.`` in front; nothing else is touched."""
    for m in modules:
        dotted = m.replace("/", ".")
        content = re.sub(
            rf"(?m)^(\s*)from {re.escape(dotted)} import",
            rf"\1from topcoat_hub.{dotted} import",
            content,
        )
        content = re.sub(
            rf"(?m)^(\s*)import {re.escape(dotted)}\s*$",
            rf"\1from topcoat_hub import {dotted}",
            content,
        )
    return content


async def _try(
    ask: Ask,
    try_run: Try,
    question: str,
    findings: list[str],
    guess: str,
    n: int,
    read: Read | None = None,
) -> tuple[str, str]:
    """One try: Development finds the code to call (``probe_read``), the hub reads those modules
    and hands over their def lines, a small test is written for the guess (``probe_test``) and run
    in the workshop under the person's probe mandate; the test and its output are the answer."""
    from topcoat_hub.fix_steps import read_answer

    hint = ""
    before = ""
    for attempt in (1, 2):
        code, _ = await ask(
            "probe_read", {"task": question[-6000:], "question": guess, "hint": hint}
        )
        modules, outline = (
            await module_outline(read, f"{code}\n{guess}") if read is not None else ([], "")
        )
        answer, who = await ask(
            "probe_test",
            {
                "task": question[-6000:],
                "code": code[-6000:],
                "outline": outline or "(none found)",
                "findings": _findings_text(findings)[-12_000:],
                "question": guess,
                "hint": hint,
            },
        )
        _, files, problems = read_answer(answer, "hub/tests/_probe.py")
        if (problems or not files) and (bare := bare_test(answer)):
            # live 2026-11-19: the test came as a plain code block, without the FILE markers
            files, problems = [{"path": "hub/tests/_probe.py", "content": bare}], []
        if problems or not files:
            return (
                before
                + "(kein Versuch: der Test kam nicht in der verlangten Form: "
                + "; ".join(problems or ["keine Datei"])
                + ")",
                who,
            )
        content = fix_imports(files[0]["content"], modules)
        output = await try_run(n, content)
        shown = content if len(content) <= CODE_SHOWN else content[:CODE_SHOWN] + "\n# …"
        result = f"Test:\n```python\n{shown.rstrip()}\n```\nErgebnis:\n{output}"
        broken = _BROKEN.search(output)
        if broken is None or attempt == 2:
            return before + result, who
        # a test that did not even run observed nothing (live 2026-11-19: an invented import)
        hint = (
            "YOUR LAST TEST DID NOT RUN: "
            + output[max(broken.start() - 200, 0) : broken.start() + 800]
            + "\nRead the module again and import only what stands there."
        )
        before = f"(erster Test lief nicht: {broken.group(0)}; neu geschrieben)\n"
    return before, ""


def with_log(inv: Investigation) -> str:
    """The report followed by the rounds, so the person sees whom the lead asked what."""
    rounds = "\n".join(f"- {line}" for line in inv.log) or "-"
    text = f"{inv.text}\n\n## Untersuchung\n{rounds}"
    if inv.answers:
        text += "\n\n## Fragen und Antworten\n\n" + "\n\n".join(inv.answers)
    return text
