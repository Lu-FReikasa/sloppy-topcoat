"""A project task that names a block program (W3, plan of 2026-11-11).

A task body may say ``Programm: wiki_check``: then the task is not a free agent run but that fixed
program (blocks/builtin.py). The project still decides what is a task, cuts the work and queues it;
the program is the check itself. ``Seite: <path>`` names the document of the knowledge base the
program reads (its input ``page``); the input ``task`` is the task's text and ``form`` its columns.

The run is kept as an agent task of kind ``program`` (no agent loop runs it: the block service
does), with the result table in its diagnosis and its sources as evidence rows. So the project's
own checks judge it unchanged: the form, a Beleg in every row that names a kept source, the secret
check and the second look by another model.

Sources are numbered once for the whole run, E1, E2 ...: first what tool blocks read (the program
calls them F1, F2 ...), then the reads of the agent task of each row (each of which numbered its own
from E1). A row's Beleg is rewritten to these numbers, so a reference always means one source.
"""

import json
import posixpath
import re
from dataclasses import dataclass, field
from typing import Any

from topcoat_hub.agent_store import PROGRAM_KIND
from topcoat_hub.json_repair import repair_json_object

_PROGRAM = re.compile(r"^\s*Programm:\s*([a-z][a-z0-9_]{0,60})\s*$", re.MULTILINE)
_PAGE = re.compile(r"^\s*Seite:\s*(.+?)\s*$", re.MULTILINE)
_REF = re.compile(r"\b([EF])(\d+)\b")
_TRAILING_COMMA = re.compile(r",\s*([}\]])")
KIND = PROGRAM_KIND  # agent_tasks.kind of a program run; the agent runner never picks it up
VIA = "program"
ROWS = "rows"  # the program's list of rows (for_each ... into: rows): the result table
RESTART = "hub_restart"  # why a program run ended that a restart cut off (it is requeued)
SOURCE_EXCERPT = 12000  # what a tool source keeps for the second look (the whole fact)
EVIDENCE_COLUMN = "beleg"
UNKNOWN = "(Quelle unbekannt)"  # what an unknown reference becomes: it names no source


_MODELS = re.compile(r"^\s*Modelle:\s*(.+?)\s*$", re.MULTILINE)
_ROLE_MODEL = re.compile(r"([a-z][a-z0-9_]{0,30})\s*=\s*([A-Za-z0-9._\-]+(?:@[A-Za-z0-9._\-]+)?)")


def models_of(body: str) -> dict[str, str]:
    """``Modelle: judge=model@node, author=model`` - the model of each role, for comparing models
    on the same program (blocks with that role ask it; ``author`` revises, ``reviewer`` checks)."""
    found = _MODELS.search(body or "")
    return dict(_ROLE_MODEL.findall(found.group(1))) if found else {}


def program_of(body: str) -> str | None:
    found = _PROGRAM.search(body or "")
    return found.group(1) if found else None


_AREA = re.compile(r"^\s*Bereich:\s*([A-Za-z0-9_./\-]{1,200})\s*$", re.MULTILINE)


def area_of(body: str) -> str | None:
    """``Bereich: hub/src/topcoat_hub/blocks`` - the part of the repository a task documents."""
    found = _AREA.search(body or "")
    return found.group(1).strip("/") if found and ".." not in found.group(1) else None


_FILES = re.compile(r"^\s*Dateien:\s*(.+?)\s*$", re.MULTILINE)


def files_of(body: str, area: str | None) -> list[str]:
    """``Dateien: a.py, b.py, sub/`` - the files a task names, as paths of the repository (relative
    to its ``Bereich``; a folder ends with /). A path that would leave the repository is dropped."""
    found = _FILES.search(body or "")
    if not found:
        return []
    out = []
    for raw in re.split(r"[,;]", found.group(1)):
        name = raw.strip().strip("`")
        if not name:
            continue
        path = posixpath.normpath(posixpath.join(area or "", name))
        if path.startswith("..") or path.startswith("/"):
            continue
        out.append(path + ("/" if name.endswith("/") else ""))
    return out


def page_of(body: str) -> str | None:
    found = _PAGE.search(body or "")
    return found.group(1) if found else None


def parse_rows(texts: list[str]) -> list[dict[str, Any] | None]:
    """One object per gathered text (None where a round wrote nothing or no object)."""
    out: list[dict[str, Any] | None] = []
    for text in texts:
        row: Any = None
        text = (text or "").strip()
        start, end = text.find("{"), text.rfind("}")
        if 0 <= start < end:
            body = text[start : end + 1]
            try:
                row = json.loads(body)
            except ValueError:  # a small model's JSON: a comma too many, a quote missing
                row = repair_json_object(_TRAILING_COMMA.sub(r"\1", body))
        out.append(row if isinstance(row, dict) and row else None)
    return out


def expand_rows(
    texts: list[str], tasks: list[list[str]]
) -> tuple[list[dict[str, Any] | None], list[list[str]]]:
    """One row per object: a round that wrote a JSON list (the paragraphs of one file) gives each
    of its objects its own row, all backed by that round's agent tasks."""
    rows: list[dict[str, Any] | None] = []
    row_tasks: list[list[str]] = []
    for text, owner in zip(texts, tasks, strict=False):
        stripped = (text or "").strip()
        items: Any = None
        bracket, brace = stripped.find("["), stripped.find("{")
        if bracket != -1 and (brace == -1 or bracket < brace):  # a list comes first: a list
            start, end = bracket, stripped.rfind("]")
            try:
                items = json.loads(stripped[start : end + 1]) if 0 <= start < end else None
            except ValueError:
                items = None
        if isinstance(items, list):
            for item in items:
                rows.append(item if isinstance(item, dict) and item else None)
                row_tasks.append(owner)
            continue
        rows += parse_rows([text])
        row_tasks.append(owner)
    return rows, row_tasks


def gathered_rows(
    variables: dict[str, Any],
    collected: dict[str, list[list[str]]],
    run_agents: list[str] | None = None,
) -> tuple[list[str], list[list[str]]]:
    """The texts of every row list a program wrote (``rows`` and ``rows_<name>``, in the order it
    wrote them) and, per row, the agent tasks that worked on it. A list a loop gathered knows its
    tasks per item; a list written in one go (one call over what the run read) is backed by all
    agent tasks of the run (``run_agents``)."""
    texts: list[str] = []
    tasks: list[list[str]] = []
    for name, value in variables.items():
        if (name != ROWS and not name.startswith(ROWS + "_")) or not isinstance(value, list):
            continue
        per_item = collected.get(name)
        for i, text in enumerate(value):
            texts.append(text if isinstance(text, str) else json.dumps(text, ensure_ascii=False))
            if per_item is None:
                tasks.append(list(run_agents or []))
            else:
                tasks.append(per_item[i] if i < len(per_item) else [])
    return texts, tasks


@dataclass
class Source:
    ref: str  # the run's own number, E1 ...
    tag: str
    excerpt: str
    source: dict[str, Any] = field(default_factory=dict)


def number_sources(
    rows: list[dict[str, Any] | None],
    tool_sources: list[dict[str, Any]],
    row_reads: list[list[dict[str, Any]]],
) -> tuple[list[dict[str, Any]], list[Source]]:
    """The result table with every Beleg rewritten to the run's own numbers, and the sources.

    ``tool_sources`` are the program's tool reads in order (``ref`` F1 ...); ``row_reads`` holds,
    per row, the kept reads of its agent task(s) (``ref`` E1 ... as that task numbered them, with
    ``excerpt``, ``tag``, ``source``). A row whose Beleg names no source the run has is left out
    (the caller counts it): one statement nobody could back does not sink the rows that are."""
    sources: list[Source] = []
    tool_map: dict[str, str] = {}
    for s in tool_sources:
        ref = f"E{len(sources) + 1}"
        tool_map[str(s.get("ref"))] = ref
        call = {**(s.get("call") or {}), "node": s.get("node")}
        sources.append(Source(ref, "tool", str(s.get("text") or "")[:SOURCE_EXCERPT], call))
    table: list[dict[str, Any]] = []
    shared: dict[tuple[str, str], str] = {}  # (agent task, its ref) -> the run's number
    for i, row in enumerate(rows):
        if row is None:
            continue
        local: dict[str, str] = {}
        for read in row_reads[i] if i < len(row_reads) else []:
            if not str(read.get("excerpt") or "").strip():
                continue
            key = (str(read.get("task") or ""), str(read.get("ref")))
            if key[0] and key in shared:  # the same read of the same task: one number
                local.setdefault(key[1], shared[key])
                continue
            ref = f"E{len(sources) + 1}"
            if key[0]:
                shared[key] = ref
            local.setdefault(str(read.get("ref")), ref)
            sources.append(
                Source(ref, str(read.get("tag") or "live")[:8], str(read["excerpt"]),
                       dict(read.get("source") or {}))
            )  # fmt: skip

        def renumber(m: re.Match[str], local: dict[str, str] = local) -> str:
            # a reference this row's reads do not have must not land on another source's number
            return (tool_map if m.group(1) == "F" else local).get(m.group(0), UNKNOWN)

        out = {
            k: _REF.sub(renumber, str(v)) if k.strip().casefold().startswith(EVIDENCE_COLUMN) else v
            for k, v in row.items()
        }
        cited = " ".join(
            str(v) for k, v in out.items() if k.strip().casefold().startswith(EVIDENCE_COLUMN)
        )
        if _REF.search(cited):
            table.append(out)
    return table, sources


DOC_PROGRAMS = ("doc_section",)
# programs whose result is one text, not rows: a research report (thorough research, a check by
# another model, revision rounds, filed in the Ablage). It is the default of a Recherche task: what
# Recherche delivers is a report, also when a project asks for it (2026-11-15: task 1.6 ran as a
# three-row checklist table, "Referenzbericht erstellt", with no design in it)
REPORT_PROGRAMS = (
    "research_report",
    "theme_design",
    "issue_check",
    "issue_fix",
    "issue_steps",
    "issue_investigate",
    "issue_plan",
)
# an issue (project_issues.py): the check finds the cause, the fix writes the edits (issue_fixes.py)
ISSUE_PROGRAMS = ("issue_check", "issue_fix", "issue_steps", "issue_investigate", "issue_plan")
FIX_PROGRAM = "issue_fix"
# the fix planned as steps, one fresh run per place (fix_steps.py, 2026-11-19)
STEPS_PROGRAM = "issue_steps"
# "Fix entwerfen": rounds with Development and Verwaltung up to a report and a plan
# (issue_investigation.py); "Plan umsetzen": that plan in steps, without planning again
INVESTIGATE_PROGRAM = "issue_investigate"
PLAN_PROGRAM = "issue_plan"
FIX_PROGRAMS = (FIX_PROGRAM, STEPS_PROGRAM, PLAN_PROGRAM)
DEFAULT_PROGRAMS = {"research": "research_report"}
CHECKLIST_PROGRAM = "checklist_check"
FREE = "frei"  # "Programm: frei": the task is a free agent run whatever it looks like
_WRITES = re.compile(r"^\s*Modus:\s*schreiben\b", re.MULTILINE | re.IGNORECASE)
_ITEM = re.compile(r"^\s*[-*+]\s+\[[ xX]\]\s*(.+?)\s*$", re.MULTILINE)


def checklist_items(body: str) -> list[str]:
    """The texts of the checklist items of a task body (``- [ ] ...`` and ``- [x] ...``)."""
    return [m.group(1) for m in _ITEM.finditer(body or "")]


def without_checklist(body: str) -> str:
    """The body without its checklist lines: the context an item's run gets with its one item."""
    return "\n".join(line for line in (body or "").splitlines() if not _ITEM.match(line)).strip()


# a Development task about a theme is a theme program: written, checked, revised (2026-11-15)
THEME_PROGRAM = "theme_design"
_THEME = re.compile(r"\btheme\w*\b", re.I)


def default_program(target: str, title: str, body: str) -> str | None:
    """The program a task runs when its text names none: Recherche delivers a report, a
    Development task about a theme delivers a theme, one with a checklist works through it item by
    item; anything else is a free agent run."""
    if target == "dev" and _THEME.search(f"{title}\n{body}"):
        return THEME_PROGRAM
    if target == "dev" and checklist_items(body) and not _WRITES.search(body):
        # one fresh run per item (2026-11-18): a free run over a whole checklist lost its way.
        # The program only reads; a task that writes code stays a free run
        return CHECKLIST_PROGRAM
    return DEFAULT_PROGRAMS.get(
        target
    )  # programs whose confirmed rows become a document of the project


# "Überblick": the page's introduction, written from the confirmed Zweck paragraphs (2026-11-13)
OVERVIEW = "Überblick"
DOC_ORDER = (OVERVIEW, "Zweck", "Begriffe", "Ablauf", "Einstellungen", "Zusammenspiel", "Grenzen")
_CITE = re.compile(r"\bE\d+\b")
# a sentence about the writer's reading, not about the code (first run, 2026-11-12: "die
# vollständige Implementierung ist nicht einsehbar"); the second look confirms such a sentence,
# since the notes do lack it, so it is sorted out here, without a model
_META = re.compile(
    r"nicht (?:näher |direkt |vollständig |genau |im Detail )?"
    r"(?:einsehbar|ersichtlich|erfasst|gelesen|erkennbar|dokumentiert|beschrieben|angegeben)"
    # "Notizen" was the reader agent's notes; it is gone, and in the memory's code the word is a
    # term of the domain ("Review-Notizen", D7 2026-11-13, lost a true sentence)
    r"|Lesebeschränk|Lesebereich|gelesenen (?:Zeilen|Ausschnitt|Code|Teil)"
    r"|vorliegenden (?:Ausschnitt|Code|Zeilen|Teil)",
    re.IGNORECASE,
)
_SENTENCE = re.compile(r"(?<=[.!?])\s+(?=[A-ZÄÖÜ`„\"(])")
_SECTION_PREFIX = re.compile(r"^[#\s]*(?:Abschnitt\s*[:\-\u2013]\s*)?", re.IGNORECASE)
META_REASON = "Satz über das Lesen, nicht über den Code"


_ROW_DUMP = re.compile(
    r"^\s*(?:\d+\.\s*)?(?:Abschnitt:\s*[^;]*;\s*)?(?:Text:\s*)?(.*?)(?:\s*Beleg:.*)?$", re.DOTALL
)


def statement(raw: Any) -> str:
    """A removed statement as the reader should see it: an author that removed a row sometimes
    hands back the row as the look listed it ("1. Abschnitt: Ablauf; Text: ...; Beleg: [...]")."""
    found = _ROW_DUMP.match(str(raw or ""))
    return (found.group(1) if found else str(raw or "")).strip().rstrip(";").strip()


def section_name(raw: Any) -> str:
    """The bare name of a section: ``Abschnitt: Zweck`` and ``zweck`` are ``Zweck``."""
    name = _SECTION_PREFIX.sub("", str(raw or "")).strip().rstrip(":").strip()
    for known in DOC_ORDER:
        if name.casefold() == known.casefold():
            return known
    return name


_TICKED = re.compile(r"`([^`\n]{1,120})`")
_IDENT = re.compile(r"[A-Za-z_][A-Za-z0-9_]{2,}")
NAME_REASON = "nennt einen Namen, der in den zitierten Zeilen nicht vorkommt"


def unknown_names(text: str, cited: str) -> list[str]:
    """The names in backticks that the cited code does not contain (a writer that misremembers
    ``_start_program`` as ``_start_project_run``, 2026-11-12). A path or file name is not checked
    (the page names its files), nor a piece shorter than three letters."""
    out = []
    for ticked in _TICKED.findall(text):
        if "/" in ticked or ticked.endswith((".py", ".go", ".ts", ".tsx", ".md")):
            continue
        out += [p for p in _IDENT.findall(ticked) if p not in cited and p not in out]
    return out


def check_names(
    table: list[dict[str, Any]], excerpts: dict[str, str]
) -> tuple[list[dict[str, Any]], list[dict[str, str]]]:
    """Sentences that name something the code they cite does not contain leave the page, with
    the names as the reason; a paragraph left with nothing else goes. No model is asked."""
    kept: list[dict[str, Any]] = []
    removed: list[dict[str, str]] = []
    for row in table:
        cited = "\n".join(excerpts.get(r, "") for r in _CITE.findall(str(row.get("Beleg", ""))))
        if not cited:
            kept.append(row)
            continue
        rest = []
        for sentence in _SENTENCE.split(str(row.get("Text") or "").strip()):
            names = unknown_names(sentence, cited)
            if names:
                why = f"{NAME_REASON}: {', '.join(names)}"
                removed.append({"Aussage": sentence, "Grund": why})
            elif sentence.strip():
                rest.append(sentence)
        if rest:
            kept.append({**row, "Text": " ".join(rest)})
    return kept, removed


_CONSTANT = re.compile(r"`[A-Z][A-Z0-9_]{2,}`")
# a number of a sentence: 3.153.600 (German thousands), 0,5 (German decimal), 10_000, 60.0
_NUMBER = re.compile(r"(?<![\w.,])\d[\d_]*(?:[.,]\d+)*(?![\w])")
VALUE_REASON = "nennt einen Wert, der in den zitierten Zeilen nicht steht"


def _number_forms(raw: str) -> set[str]:
    """How a number of a German sentence may stand in code: 3.153.600 is 3153600, 0,5 is 0.5,
    10_000 is 10000."""
    plain = raw.replace("_", "")
    forms = {plain}
    if re.fullmatch(r"\d{1,3}(?:\.\d{3})+", plain):
        forms.add(plain.replace(".", ""))
    if "," in plain:
        forms.add(plain.replace(".", "").replace(",", "."))
    return forms


def unknown_values(sentence: str, cited: str) -> list[str]:
    """The numbers of a sentence about a constant that its cited code does not contain (2026-11-13:
    `MAX_ESTIMATE = 60 * 24 * 366 * 5` became "3.153.600 Minuten"; the name check let it pass).
    Only a sentence that names a constant in backticks is checked, and 0 and 1 are left alone (a
    count, a first line). A value the writer worked out from the code goes too: the page states
    what the code says."""
    if not _CONSTANT.search(sentence):
        return []
    code = cited.replace("_", "")
    out = []
    for raw in _NUMBER.findall(sentence):
        if raw in ("0", "1") or re.fullmatch(r"E\d+", raw):
            continue
        if not any(form in code for form in _number_forms(raw)) and raw not in out:
            out.append(raw)
    return out


def check_values(
    table: list[dict[str, Any]], excerpts: dict[str, str]
) -> tuple[list[dict[str, Any]], list[dict[str, str]]]:
    """Sentences that give a constant a value the code they cite does not show leave the page,
    with the values as the reason; a paragraph left with nothing else goes. No model is asked."""
    kept: list[dict[str, Any]] = []
    removed: list[dict[str, str]] = []
    for row in table:
        cited = "\n".join(excerpts.get(r, "") for r in _CITE.findall(str(row.get("Beleg", ""))))
        if not cited:
            kept.append(row)
            continue
        rest = []
        for sentence in _SENTENCE.split(str(row.get("Text") or "").strip()):
            values = unknown_values(sentence, cited)
            if values:
                removed.append(
                    {"Aussage": sentence, "Grund": f"{VALUE_REASON}: {', '.join(values)}"}
                )
            elif sentence.strip():
                rest.append(sentence)
        if rest:
            kept.append({**row, "Text": " ".join(rest)})
    return kept, removed


def purpose_from_start(table: list[dict[str, Any]], sources: list[Source]) -> list[dict[str, Any]]:
    """A Zweck paragraph says what a file is for, which only its first lines show: one whose
    sources all begin later in a file is about what happens there and becomes Ablauf (the rule in
    the author's prompt was not always kept, 2026-11-12). No model needed: the read names its
    lines."""
    first: dict[str, int] = {}
    for s in sources:
        given = (s.source or {}).get("input") or {}
        try:
            first[s.ref] = int(given.get("start_line") or 1)
        except (TypeError, ValueError):
            first[s.ref] = 1
    out: list[dict[str, Any]] = []
    for row in table:
        if section_name(row.get("Abschnitt")) == "Zweck":
            refs = _CITE.findall(str(row.get("Beleg") or ""))
            if refs and all(first.get(r, 1) > 1 for r in refs):
                row = {**row, "Abschnitt": "Ablauf"}
        out.append(row)
    return out


def tidy_doc_rows(
    table: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, str]]]:
    """The paragraphs with bare section names and without sentences about the reading; a
    paragraph left with nothing else goes. Returns the paragraphs and what was taken out."""
    kept: list[dict[str, Any]] = []
    removed: list[dict[str, str]] = []
    for row in table:
        row = {**row}
        if "Abschnitt" in row:
            row["Abschnitt"] = section_name(row["Abschnitt"])
        text = str(row.get("Text") or "").strip()
        parts = [s for s in _SENTENCE.split(text) if s.strip()]
        meta = [s for s in parts if _META.search(s)]
        removed += [{"Aussage": s, "Grund": META_REASON} for s in meta]
        rest = [s for s in parts if not _META.search(s)]
        if meta and not rest:
            continue
        if meta:
            row["Text"] = " ".join(rest)
        kept.append(row)
    return kept, removed


def where(source: dict[str, Any]) -> str:
    """Where a source was read: capability, path and lines as the call named them."""
    given = source.get("input") or {}
    path = given.get("path") or given.get("query") or ""
    lines = ""
    if given.get("start_line"):
        lines = f", Zeilen {given['start_line']}-{given.get('end_line', '')}".rstrip("-")
    return f"{source.get('capability', 'gelesen')} {path}{lines}".strip()


def render_document(
    title: str,
    table: list[dict[str, Any]],
    sources: list[Source],
    removed: list[dict[str, str]],
    build: str,
    when: str,
    unchecked: bool = False,
) -> str:
    """The confirmed paragraphs as a page: sections in their order, each paragraph with its
    sources, then the list of sources and what was removed (and why). ``unchecked``: the second
    look could not run; the note says so instead of claiming a check."""
    note = f"_Erzeugt aus dem Code des Hub-Builds `{build}` am {when}; "
    if unchecked:
        note += "**ungeprüft**: der zweite Blick konnte nicht laufen._"
    else:
        note += "jeder Absatz ist gegen die genannten Stellen geprüft._"
    out = [f"# {title}", "", note, ""]
    sections: dict[str, list[dict[str, Any]]] = {}
    for row in table:
        sections.setdefault(section_name(row.get("Abschnitt")) or "Weiteres", []).append(row)
    order = [s for s in DOC_ORDER if s in sections] + [s for s in sections if s not in DOC_ORDER]
    cited: list[str] = []
    for name in order:
        out += [f"## {name}", ""]
        for row in sections[name]:
            refs = [r for r in _CITE.findall(str(row.get("Beleg", ""))) if r not in ("",)]
            cited += [r for r in refs if r not in cited]
            out += [
                f"{str(row.get('Text', '')).strip()} {' '.join(f'[{r}]' for r in refs)}".strip(),
                "",
            ]
    by_ref = {s.ref: s for s in sources}
    if cited:
        out += ["## Belege", ""]
        out += [f"- **{r}** {where(by_ref[r].source)}" for r in cited if r in by_ref]
        out.append("")
    if removed:
        out += ["## Nicht belegt, entfernt", ""]
        out += [f"- {statement(r.get('Aussage', ''))} - {r.get('Grund', '')}" for r in removed]
        out.append("")
    return "\n".join(out).rstrip() + "\n"
