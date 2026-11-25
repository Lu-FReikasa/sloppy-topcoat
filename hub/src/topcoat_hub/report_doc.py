# ruff: noqa: E501, RUF001  (the text of a document is prose, with its own dashes)
"""The report document of a thorough research (D102): Markdown, assembled by code.

The model writes the *Vorwort* and the chapters (one section per tool call, see
``message_tools.SECTION``). Everything that is a statement about the run or about the sources is
written here, from what is stored, so that nothing in it can be invented: the title page, the box
*Grenzen dieser Recherche*, the table of contents, the numbered citations and the list of sources
with the passages literally, and the appendix *Entstehung* (models, times, counts).

The document is Markdown with front matter. It holds no raw HTML: the view and the export are made
from it by the interface (D102). A pure function: nothing is read from the database here.
"""

import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any
from urllib.parse import urlsplit

from topcoat_hub import stop_reasons
from topcoat_hub.message_result import findings_of

LABELS: dict[str, dict[str, str]] = {
    "de": {
        "question": "Fragestellung",
        "facts": "Eckdaten",
        "status": "Stand",
        "status_draft": "Entwurf",
        "status_reviewed": "Geprüft",
        "status_released": "Freigegeben",
        "version": "Version",
        "created": "Erstellt",
        "frame": "Tiefe",
        "foreword": "Vorwort",
        "limits": "Grenzen dieser Recherche",
        "limits_none": (
            "Der Lauf meldete keine nicht erreichten Quellen und keine Aussage ohne wörtlichen "
            "Beleg. Das heißt nicht, dass das Thema vollständig behandelt ist; was im Vorwort "
            "als ausgelassen genannt wird, bleibt ausgelassen."
        ),
        "limits_failed": "Nicht erreicht",
        "limits_unbacked": "Ohne wörtlichen Beleg",
        "limits_stopped": "Der Lauf endete vorzeitig",
        "limits_uncovered": "Nicht abgedeckt",
        "stop_no_progress": "weil über mehrere Schritte keine neue Quelle und kein neuer Beleg dazukam",
        "stop_other": "Grund",
        "incomplete": "unvollständig",
        "banner": "**Diese Recherche ist unvollständig.** Sie wurde vorzeitig beendet: {why}. Was bis dahin gefunden wurde, steht in diesem Dokument; was fehlt oder nicht belegt ist, steht im Kasten „Grenzen dieser Recherche“.",
        "why_repeated_last": "zuletzt „{call}“",
        "why_other": "Grund: {code}",
        "write_up_note": "Der folgende Text ist die Niederschrift, die das Modell nach dem Abbruch aus dem bis dahin Gelesenen verfasst hat. Er wurde vom Hub nicht auf Belege geprüft.",
        "no_text": "Es liegt kein ausgearbeiteter Text vor.",
        "foreword_default": "Der Lauf hat kein eigenes Vorwort geschrieben. Gegenstand dieses Berichts ist die oben genannte Fragestellung.",
        "log": "Suchverlauf",
        "call_n": "Nr.",
        "call_kind": "Art",
        "call_target": "Anfrage / Adresse",
        "call_result": "Ergebnis",
        "kind_search": "Suche",
        "kind_page": "Seite",
        "kind_notes": "Notizen",
        "kind_tool": "Werkzeug",
        "result_ok": "gelesen",
        "result_failed": "nicht erreicht",
        "sources_cited_read": "{cited} zitiert, {read} gelesen",
        "calls_more": "… und {n} weitere",
        "contents": "Inhalt",
        "chapter": "Kapitel",
        "findings": "Kernaussagen mit Beleg",
        "claim": "Aussage",
        "quote": "Beleg (wörtlich)",
        "source": "Quelle",
        "sources": "Quellenverzeichnis",
        "retrieved": "abgerufen",
        "excerpt": "Auszug",
        "search": "Suche",
        "origin": "Entstehung",
        "run": "Lauf",
        "started": "Beginn",
        "finished": "Ende",
        "duration": "Dauer",
        "author": "Autor-Modell",
        "models": "Beteiligte Modelle",
        "model": "Modell",
        "node": "Rechner",
        "work": "Aufgabe",
        "calls": "Aufrufe",
        "time": "Zeit",
        "tokens": "Tokens",
        "steps": "Schritte",
        "tool_calls": "Werkzeugaufrufe",
        "searches": "Suchen",
        "pages": "Seiten gelesen",
        "sources_n": "Quellen",
        "hosts": "Hosts",
        "backed": "Belegprüfung",
        "backed_of": "von {total} Kernaussagen mit wörtlichem Beleg: {n}",
        "build": "Hub-Build",
        "no_chapters": "Ergebnisse (Niederschrift des Modells)",
        "unknown_mark": "Beleg nicht im Lauf gefunden",
    },
    "en": {
        "question": "Question",
        "facts": "Key facts",
        "status": "Status",
        "status_draft": "Draft",
        "status_reviewed": "Reviewed",
        "status_released": "Released",
        "version": "Version",
        "created": "Created",
        "frame": "Depth",
        "foreword": "Foreword",
        "limits": "Limits of this research",
        "limits_none": (
            "The run reported no sources it could not reach and no claim without a literal "
            "quote. That does not mean the topic is covered completely; what the foreword names "
            "as left out stays left out."
        ),
        "limits_failed": "Not reached",
        "limits_unbacked": "Without a literal quote",
        "limits_stopped": "The run ended early",
        "limits_uncovered": "Not covered",
        "stop_no_progress": "because several steps in a row brought no new source and no new evidence",
        "stop_other": "reason",
        "incomplete": "incomplete",
        "banner": '**This research is incomplete.** It ended early: {why}. What was found up to then is in this document; what is missing or not backed is in the box "Limits of this research".',
        "why_repeated_last": 'last "{call}"',
        "why_other": "reason: {code}",
        "write_up_note": "The text below is the write-up the model made after the stop, from what it had read up to then. The hub did not check it against evidence.",
        "no_text": "There is no written-out text.",
        "foreword_default": "The run wrote no foreword of its own. The subject of this report is the question stated above.",
        "log": "Search log",
        "call_n": "No.",
        "call_kind": "Kind",
        "call_target": "Request / address",
        "call_result": "Result",
        "kind_search": "Search",
        "kind_page": "Page",
        "kind_notes": "Notes",
        "kind_tool": "Tool",
        "result_ok": "read",
        "result_failed": "not reached",
        "sources_cited_read": "{cited} cited, {read} read",
        "calls_more": "… and {n} more",
        "contents": "Contents",
        "chapter": "Chapter",
        "findings": "Key claims with evidence",
        "claim": "Claim",
        "quote": "Evidence (literal)",
        "source": "Source",
        "sources": "Sources",
        "retrieved": "retrieved",
        "excerpt": "Excerpt",
        "search": "Search",
        "origin": "How it was made",
        "run": "Run",
        "started": "Started",
        "finished": "Finished",
        "duration": "Duration",
        "author": "Author model",
        "models": "Models involved",
        "model": "Model",
        "node": "Machine",
        "work": "Task",
        "calls": "Calls",
        "time": "Time",
        "tokens": "Tokens",
        "steps": "Steps",
        "tool_calls": "Tool calls",
        "searches": "Searches",
        "pages": "Pages read",
        "sources_n": "Sources",
        "hosts": "Hosts",
        "backed": "Evidence check",
        "backed_of": "of {total} key claims with a literal quote: {n}",
        "build": "Hub build",
        "no_chapters": "Results (the model's write-up)",
        "unknown_mark": "evidence not found in the run",
    },
}

_FOREWORD = re.compile(r"^\s*(vorwort|vorbemerkung|preface|foreword)\s*$", re.IGNORECASE)
_MARKS = re.compile(r"\[(E\d{1,4}(?:\s*[,;]\s*E\d{1,4})*)\]")
_HEADING = re.compile(r"^(#{1,6})\s+(.*)$", re.MULTILINE)
EXCERPT_CHARS = 280
# the title of the section that holds what a model wrote in plain text after the run was stopped
# (agent_loop._wrap_up): the document gives it a title of its own language and says what it is
WRITE_UP_KEY = "__write_up__"
MAX_CALLS_SHOWN = 80


def _why(data: "ReportInput", t: Mapping[str, str]) -> str:
    """Why the run ended early, in plain words of the report's language."""
    reason = data.stop_reason or ""
    text = stop_reasons.long(reason, data.language) or t["why_other"].format(code=reason or "?")
    if reason == "repeated_calls" and data.last_repeated:
        text += (
            " ("
            + t["why_repeated_last"].format(call=" ".join(data.last_repeated.split())[:120])
            + ")"
        )
    if not reason:
        text = t["no_text"].rstrip(".")
    return text


def slug(text: str) -> str:
    """The anchor of a heading: lower case, letters and digits, a hyphen for the rest. The
    interface makes its heading ids the same way (``web/src/report``)."""
    out = re.sub(r"[^\w]+", "-", text.strip().lower(), flags=re.UNICODE).strip("-_")
    return out or "section"


@dataclass
class ReportInput:
    """What a run leaves behind, as the document needs it."""

    title: str
    question: str
    language: str  # "de" | "en"
    depth: str
    summary: str
    sections: Sequence[tuple[str, str]]
    claims: Sequence[Mapping[str, Any]]  # {"claim", "evidence": [refs]} of the diagnosis
    evidence: Sequence[Any]  # rows with .ref .tag .source .excerpt
    open_points: Sequence[str] = ()
    failed: Sequence[Mapping[str, Any]] = ()  # calls that gave nothing
    must_cover: Sequence[str] = ()
    covered: Sequence[str] = ()  # the must_cover items the run says it covered
    stop_reason: str | None = None
    version: int = 1
    status: str = "draft"
    created_at: datetime | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None
    author_model: str | None = None
    author_node: str | None = None
    steps: int = 0
    tool_calls: int = 0
    models: Sequence[Mapping[str, Any]] = ()  # {model, node, work, calls, ms, tokens}
    # the last request the hub refused as a repeat, and every request of the run in order:
    # {"kind": "search" | "page" | "notes" | "tool", "target": str, "ok": bool}
    last_repeated: str = ""
    calls: Sequence[Mapping[str, Any]] = ()
    build: str = ""
    extra_limits: list[str] = field(default_factory=list)


def _jstr(value: Any) -> str:
    return json.dumps(str(value), ensure_ascii=False)


def _iso(value: datetime | None) -> str:
    return value.isoformat(timespec="seconds") if value is not None else ""


def _seconds(a: datetime | None, b: datetime | None) -> int | None:
    return int((b - a).total_seconds()) if a is not None and b is not None else None


def _clock(seconds: int | None) -> str:
    if seconds is None:
        return "–"
    h, rest = divmod(max(seconds, 0), 3600)
    m, s = divmod(rest, 60)
    return f"{h} h {m:02d} min" if h else (f"{m} min {s:02d} s" if m else f"{s} s")


def _cell(text: Any) -> str:
    return " ".join(str(text).split()).replace("|", "\\|")


def _demote(text: str, title: str | None = None) -> str:
    """Headings the model wrote inside a section become sub-headings: the chapters' own headings
    are the document's structure. A first heading that only repeats the section's title (the
    document shows that title as the chapter heading already) is dropped."""
    if title is not None:
        lines = text.lstrip().splitlines()
        first = _HEADING.match(lines[0]) if lines else None
        if first is not None:
            heading = first.group(2).strip(" #*_:").casefold()
            wanted = {title.strip(" #*_:").casefold()}
            if _FOREWORD.match(title):
                wanted |= {"vorwort", "vorbemerkung", "preface", "foreword"}
            if heading in wanted:
                text = "\n".join(lines[1:])
    return _HEADING.sub(
        lambda m: "###" + m.group(1)[2:] + " " + m.group(2) if len(m.group(1)) < 3 else m.group(0),
        text,
    )


_SOURCE_LIST_HEADING = re.compile(
    r"^(#{2,6})\s*(?:\d+[.)]?\s*)?(quellenverzeichnis|quellen|literaturverzeichnis|literatur|"
    r"sources|references|bibliography)\s*:?\s*$",
    re.IGNORECASE,
)


def _without_source_list(text: str) -> str:
    """A list of sources the model wrote inside a chapter is dropped: the document builds its own
    from what the run read, and a list from the model can name pages nobody opened."""
    out: list[str] = []
    skipping = 0  # the level of the heading being skipped
    for line in text.splitlines():
        heading = re.match(r"^(#{1,6})\s", line)
        if skipping:
            if heading and len(heading.group(1)) <= skipping:
                skipping = 0
            else:
                continue
        found = _SOURCE_LIST_HEADING.match(line)
        if found:
            skipping = len(found.group(1))
            continue
        out.append(line)
    return "\n".join(out)


def _unnumbered(title: str) -> str:
    """A chapter title without the number the model gave it: the document numbers its chapters."""
    return re.sub(r"^\s*\d+[.)]?\s+", "", title).strip() or title.strip()


def _web(row: Any) -> tuple[str, str]:
    """(url, host) of an evidence row, or ("", "")."""
    source: Mapping[str, Any] = row.source or {}
    url = source.get("url")
    if not isinstance(url, str) or not url:
        given = source.get("input")
        url = given.get("url") if isinstance(given, Mapping) else ""
    if not isinstance(url, str) or not url:
        return "", ""
    return url, urlsplit(url).netloc


class _Citations:
    """Numbers the evidence marks of the text in the order they first appear."""

    def __init__(self, rows: Mapping[str, Any]) -> None:
        self.rows = rows
        self.order: list[str] = []
        self.unknown: list[str] = []

    def number(self, ref: str) -> int | None:
        if ref not in self.rows:
            if ref not in self.unknown:
                self.unknown.append(ref)
            return None
        if ref not in self.order:
            self.order.append(ref)
        return self.order.index(ref) + 1

    def convert(self, text: str) -> str:
        def one(match: re.Match[str]) -> str:
            refs = [r.strip() for r in re.split(r"[,;]", match.group(1))]
            numbers = [self.number(r) for r in refs]
            if all(n is None for n in numbers):
                return match.group(0)
            shown = ", ".join(str(n) for n in sorted({n for n in numbers if n is not None}))
            return f"[{shown}]"

        return _MARKS.sub(one, text)


_SNIPPET = re.compile(r'"(?:title|snippet)":"((?:[^"\\]|\\.)*)"')


def _plain(text: str) -> str:
    """A quote or an excerpt as text a person reads: the answer of a tool arrives as a JSON
    envelope ({"count":5,"note":...,"results":[...]} or {"status":200,"text":"..."}); what the
    page or the hits say is kept, the envelope goes. Text that is no envelope stays as it is."""
    stripped = text.strip()
    if not stripped.startswith('{"'):
        return text
    try:
        data = json.loads(stripped)
    except ValueError:
        data = None
    if isinstance(data, dict):
        if isinstance(data.get("text"), str):
            return data["text"]
        hits = data.get("results")
        if isinstance(hits, list):
            lines = [
                f"{h.get('title', '')}: {h.get('snippet', '')}".strip(": ")
                for h in hits
                if isinstance(h, dict)
            ]
            return " | ".join(line for line in lines if line)
        return text
    # cut off in the middle (an excerpt): take the text field, or the titles and snippets
    cut = re.search(r'"text":"(.*)', stripped, re.DOTALL)
    if cut:
        return cut.group(1).replace("\\n", " ").replace('\\"', '"')
    found = [m.group(1).replace('\\"', '"') for m in _SNIPPET.finditer(stripped)]
    return " | ".join(found) if found else text


def build_document(data: ReportInput) -> tuple[str, dict[str, Any]]:
    """The Markdown of the report and its metadata (JSON-safe, kept with the version)."""
    t = LABELS.get(data.language, LABELS["en"])
    # the answers of the tools that write the report are no evidence: a model that cites the
    # answer of research.report.section ("sections: ...") has cited nothing it read
    evidence = [r for r in data.evidence if (r.source or {}).get("node") != "messages"]
    rows = {r.ref: r for r in evidence}
    cites = _Citations(rows)

    foreword = next((text for title, text in data.sections if _FOREWORD.match(title)), "")
    chapters = [(title, text) for title, text in data.sections if not _FOREWORD.match(title)]
    chapters = [
        (t["no_chapters"], f"_{t['write_up_note']}_\n\n{text}")
        if title == WRITE_UP_KEY
        else (title, text)
        for title, text in chapters
    ]
    # the placeholder the loop writes when a run ended without a diagnosis is no text of the model
    summary = "" if data.summary.startswith("The run stopped (") else data.summary.strip()
    incomplete = bool(data.stop_reason) or not (chapters or summary or data.claims)
    why = _why(data, t) if incomplete else ""
    if not chapters:
        body = "\n".join(
            f"- {c.get('claim', '')} {' '.join(f'[{r}]' for r in c.get('evidence') or [])}".rstrip()
            for c in data.claims
        )
        text = (summary + "\n\n" + body).strip()
        if summary and data.stop_reason:
            text = f"_{t['write_up_note']}_\n\n{text}"
        chapters = [(t["no_chapters"], text or t["no_text"])]
        foreword = foreword.strip() or t["foreword_default"]
    elif not foreword.strip():
        foreword = summary or t["foreword_default"]

    foreword_md = cites.convert(_demote(foreword, t["foreword"]).strip())
    chapter_md: list[tuple[str, str]] = [
        (_unnumbered(title), cites.convert(_demote(_without_source_list(text), title).strip()))
        for title, text in chapters
    ]

    # key claims with their literal quotes, in the order of the claims
    table: list[tuple[str, str, int | None]] = []
    unbacked: list[str] = []
    for item in data.claims:
        found, missing = findings_of([item], evidence)
        ref = next((r for r in item.get("evidence") or [] if r in rows), None)
        if found:
            number = cites.number(ref) if ref else None
            table.append((found[0]["claim"], _plain(found[0]["quote"]), number))
        else:
            claim = str(item.get("claim") or "").strip()
            if claim:
                unbacked.append(claim)
        del missing
    backed, total = len(table), len(table) + len(unbacked)

    # the box: what the run could not do. Written by the code, the model cannot reword it.
    limits: list[str] = []
    for call in data.failed:
        target = call.get("target") or call.get("capability") or ""
        if call.get("code") == "needs_sources" or str(target).startswith("research.report."):
            continue  # the hub sent a chapter back for its sources; the retry is not a gap
        limits.append(f"{t['limits_failed']}: {_cell(target)} ({_cell(call.get('code', ''))})")
    for claim in unbacked + [p for p in data.open_points if p not in unbacked]:
        limits.append(f"{t['limits_unbacked']}: {_cell(claim)}")
    for item in data.must_cover:
        if item not in data.covered:
            limits.append(f"{t['limits_uncovered']}: {_cell(item)}")
    if data.stop_reason:
        limits.append(f"{t['limits_stopped']} – {why}")
    for ref in cites.unknown:
        limits.append(f"{t['unknown_mark']}: {ref}")
    limits.extend(data.extra_limits)

    # sources, in the order of the citations
    source_lines: list[str] = []
    for n, ref in enumerate(cites.order, 1):
        row = rows[ref]
        url, _host = _web(row)
        at = str((row.source or {}).get("at") or "")[:19].replace("T", " ")
        excerpt = " ".join(_plain(row.excerpt or "").split())[:EXCERPT_CHARS]
        if url:
            line = f"[{n}] {url}"
            if at:
                line += f" – {t['retrieved']} {at} UTC"
        else:
            query = (row.source or {}).get("input")
            label = ""
            if isinstance(query, Mapping):
                label = str(query.get("query") or query.get("path") or "")
            capability = (row.source or {}).get("capability", "tool")
            line = f"[{n}] {t['search'] if 'search' in str(capability) else capability}: {label}".rstrip(
                ": "
            )
        if excerpt:
            line += (
                f"  \n{t['excerpt']}: „{excerpt}“"
                if data.language == "de"
                else f"  \n{t['excerpt']}: “{excerpt}”"
            )
        source_lines.append(line)

    hosts = {_web(rows[r])[1] for r in cites.order if _web(rows[r])[1]}
    urls = {_web(rows[r])[0] for r in cites.order if _web(rows[r])[0]}
    seconds = _seconds(data.started_at, data.finished_at)
    searches = sum(
        1 for r in data.evidence if "search" in str((r.source or {}).get("capability", ""))
    )
    pages = sum(1 for r in data.evidence if "fetch" in str((r.source or {}).get("capability", "")))
    status_label = t.get(f"status_{data.status}", data.status)
    if incomplete:
        status_label += f", {t['incomplete']}"
    read_urls = {u for r in data.evidence if (u := _web(r)[0])}
    read_hosts = {h for r in data.evidence if (h := _web(r)[1])}

    out: list[str] = [
        "---",
        f"title: {_jstr(data.title)}",
        f"question: {_jstr(data.question)}",
        f"version: {data.version}",
        f"status: {_jstr(data.status)}",
        f"language: {_jstr(data.language)}",
        f"depth: {_jstr(data.depth)}",
        f"complete: {'false' if incomplete else 'true'}",
        f"created: {_jstr(_iso(data.created_at))}",
        "---",
        "",
        f"# {data.title}",
        "",
        f"**{t['question']}:** {_cell(data.question)}",
        "",
        f"| {t['facts']} | |",
        "|---|---|",
        f"| {t['status']} | {status_label}, {t['version']} {data.version} |",
        f"| {t['created']} | {_iso(data.created_at).replace('T', ' ')} |",
        f"| {t['frame']} | {data.depth} |",
        "",
        *([t["banner"].format(why=why), ""] if incomplete else []),
        f"## {t['contents']}",
        "",
        f"- [{t['foreword']}](#{slug(t['foreword'])})",
    ]
    for n, (title, _text) in enumerate(chapter_md, 1):
        out.append(f"- [{n}. {title}](#{slug(f'{n}. {title}')})")
    if table:
        out.append(f"- [{t['findings']}](#{slug(t['findings'])})")
    if source_lines:
        out.append(f"- [{t['sources']}](#{slug(t['sources'])})")
    out.append(f"- [{t['origin']}](#{slug(t['origin'])})")
    out += ["", f"## {t['foreword']}", "", foreword_md, "", f"### {t['limits']}", ""]
    out += [f"- {line}" for line in limits] if limits else [t["limits_none"]]
    for n, (title, text) in enumerate(chapter_md, 1):
        out += ["", f"## {n}. {title}", "", text]
    if table:
        out += [
            "",
            f"## {t['findings']}",
            "",
            f"| {t['claim']} | {t['quote']} | {t['source']} |",
            "|---|---|---|",
        ]
        for claim, quote, number in table:
            mark = f"[{number}]" if number else ""
            out.append(f"| {_cell(claim)} | {_cell(quote)} | {mark} |")
    if source_lines:
        out += ["", f"## {t['sources']}", ""]
        out += [line + "\n" for line in source_lines]
    out += ["", f"## {t['origin']}", "", f"| {t['run']} | |", "|---|---|"]
    facts = [
        (t["started"], _iso(data.started_at).replace("T", " ") or "–"),
        (t["finished"], _iso(data.finished_at).replace("T", " ") or "–"),
        (t["duration"], _clock(seconds)),
        (
            t["author"],
            f"{data.author_model or '–'}" + (f" @ {data.author_node}" if data.author_node else ""),
        ),
        (t["steps"], data.steps),
        (t["tool_calls"], data.tool_calls),
        (t["searches"], searches),
        (t["pages"], pages),
        (
            t["sources_n"],
            t["sources_cited_read"].format(
                cited=len(urls) or len(cites.order), read=len(read_urls)
            ),
        ),
        (t["hosts"], len(read_hosts)),
        (t["backed"], t["backed_of"].format(total=total, n=backed)),
        (t["build"], data.build or "–"),
    ]
    out += [f"| {k} | {_cell(v)} |" for k, v in facts]
    if data.models:
        out += [
            "",
            f"**{t['models']}**",
            "",
            f"| {t['model']} | {t['node']} | {t['work']} | {t['calls']} | {t['time']} | {t['tokens']} |",
            "|---|---|---|---|---|---|",
        ]
        for m in data.models:
            out.append(
                f"| {_cell(m.get('model', ''))} | {_cell(m.get('node', ''))} | {_cell(m.get('work', ''))} | "
                f"{m.get('calls', 0)} | {_clock(int(m.get('ms', 0)) // 1000)} | {m.get('tokens') or '–'} |"
            )
    if data.calls:
        out += [
            "",
            f"**{t['log']}**",
            "",
            f"| {t['call_n']} | {t['call_kind']} | {t['call_target']} | {t['call_result']} |",
            "|---|---|---|---|",
        ]
        for n, call in enumerate(list(data.calls)[:MAX_CALLS_SHOWN], 1):
            kind = t.get(f"kind_{call.get('kind', 'tool')}", str(call.get("kind", "")))
            result = t["result_ok"] if call.get("ok", True) else t["result_failed"]
            out.append(f"| {n} | {kind} | {_cell(str(call.get('target', ''))[:200])} | {result} |")
        if len(data.calls) > MAX_CALLS_SHOWN:
            out.append(f"| | | {t['calls_more'].format(n=len(data.calls) - MAX_CALLS_SHOWN)} | |")
    out.append("")

    meta: dict[str, Any] = {
        "complete": not incomplete,
        "end_reason": data.stop_reason,
        "last_repeated": data.last_repeated,
        "calls": len(data.calls),
        "depth": data.depth,
        "language": data.language,
        "started_at": _iso(data.started_at),
        "finished_at": _iso(data.finished_at),
        "seconds": seconds,
        "author_model": data.author_model,
        "author_node": data.author_node,
        "steps": data.steps,
        "tool_calls": data.tool_calls,
        "searches": searches,
        "pages": pages,
        "sources": len(urls) or len(cites.order),
        "hosts": sorted(hosts),
        "claims_total": total,
        "claims_backed": backed,
        "models": [dict(m) for m in data.models],
        "limits": limits,
        "stop_reason": data.stop_reason,
        "build": data.build,
        "chapters": [title for title, _ in chapter_md],
    }
    return "\n".join(out), meta
