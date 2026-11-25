"""The Akte (case file) of a hand-over.

Every hand-over has one from the moment it is prepared: the request as the person saw it, the
steps it went through, and, when the work is done, the report with its sources. It is written by
the hub in code, in the language of the question, from what is stored (the dossier, the task's
diagnosis and the evidence the tools returned), never by a model: nothing in it can be invented.

The report part leans on the evidence marks (E1, E2 …) of the check. Each finding is shown with the
passage of its evidence that shows it (a quote, picked by the words the claim and the passage share,
shortened, never reworded), what the check could not see is listed as open, and the sources section
names every tool call with the machine, the input and an excerpt. That makes the file something a
person can check and something the knowledge base can hold: a later question finds the report and
can point at where each statement came from.
"""

import re
from datetime import datetime
from typing import Any

from topcoat_hub import report as report_card
from topcoat_hub.chat import question_language

VERSION = 1
QUOTE_CHARS = 320
SOURCE_CHARS = 700

LABELS: dict[str, dict[str, str]] = {
    "de": {
        "file": "Akte",
        "facts": "Eckdaten",
        "status": "Stand",
        "from_to": "Von → an",
        "created": "Angelegt",
        "sent": "Abgeschickt",
        "finished": "Abgeschlossen",
        "id": "Nummer",
        "request": "Anfrage",
        "question": "Frage",
        "asked": "Auftrag",
        "expected": "Erwartet",
        "context": "Zitate aus dem Gespräch",
        "withheld": "Zurückgehalten",
        "withheld_none": "nichts",
        "report": "Bericht",
        "summary": "Zusammenfassung",
        "findings": "Befunde",
        "assumptions": "Annahmen (nicht gesehen, nicht bestätigt)",
        "to_check": "prüfen mit",
        "proposal": "Empfehlung (nur ein Rat, nichts wurde geändert)",
        "impact": "Auswirkung",
        "rollback": "Rückweg",
        "open": "Offene Punkte",
        "incomplete": "Die Prüfung ist unvollständig",
        "not_checked": "Nicht geprüft auf",
        "checked": "Geprüft auf",
        "not_retrieved": "Nicht abrufbar",
        "no_report": "Es liegt noch kein Bericht vor.",
        "error": "Fehler",
        "sources": "Quellen",
        "quote": "Zitat",
        "on": "auf",
        "input": "Eingabe",
        "cut": "gekürzt",
        "timeline": "Verlauf",
        "contradicts": "widerspricht den Notizen",
        "none": "keine",
        "tag_live": "gerade gesehen",
        "tag_doc": "aus den Notizen",
        "tag_web": "aus dem Web",
        "st_proposed": "vorbereitet",
        "st_sent": "abgeschickt, wird bearbeitet",
        "st_done": "erledigt",
        "st_failed": "fehlgeschlagen",
        "st_cancelled": "zurückgezogen",
        "ev_proposed": "Anfrage vorbereitet",
        "ev_sent": "an die andere Anwendung abgeschickt",
        "ev_done": "Bericht liegt vor",
        "ev_failed": "Arbeit fehlgeschlagen",
        "ev_cancelled": "zurückgezogen",
    },
    "en": {
        "file": "File",
        "facts": "Facts",
        "status": "State",
        "from_to": "From → to",
        "created": "Prepared",
        "sent": "Sent",
        "finished": "Finished",
        "id": "Number",
        "request": "Request",
        "question": "Question",
        "asked": "What is asked",
        "expected": "Expected",
        "context": "Quotes from the conversation",
        "withheld": "Kept back",
        "withheld_none": "nothing",
        "report": "Report",
        "summary": "Summary",
        "findings": "Findings",
        "assumptions": "Assumptions (not seen, not confirmed)",
        "to_check": "check with",
        "proposal": "Recommendation (advice only, nothing was changed)",
        "impact": "Impact",
        "rollback": "Way back",
        "open": "Open points",
        "incomplete": "The check is incomplete",
        "not_checked": "Not checked on",
        "checked": "Checked on",
        "not_retrieved": "Not retrieved",
        "no_report": "There is no report yet.",
        "error": "Error",
        "sources": "Sources",
        "quote": "Quote",
        "on": "on",
        "input": "Input",
        "cut": "shortened",
        "timeline": "Timeline",
        "contradicts": "contradicts the notes",
        "none": "none",
        "tag_live": "seen just now",
        "tag_doc": "from the notes",
        "tag_web": "from the web",
        "st_proposed": "prepared",
        "st_sent": "sent, being worked on",
        "st_done": "done",
        "st_failed": "failed",
        "st_cancelled": "withdrawn",
        "ev_proposed": "request prepared",
        "ev_sent": "sent to the other application",
        "ev_done": "report is in",
        "ev_failed": "the work failed",
        "ev_cancelled": "withdrawn",
    },
}

_WORD = re.compile(r"[\wäöüß]{4,}", re.I)


def language_of(question: str) -> str:
    return question_language(question) or "en"


_SENTENCE_END = re.compile(r"(?<=[.!?])\s+(?=[A-ZÄÖÜ0-9`\"(\[])")
_ESCAPED_BREAK = re.compile(re.escape(chr(92)) + "[nrt]")


def _passages(line: str, limit: int) -> list[str]:
    """A line as the passages best_quote chooses from. A short line is one passage. A web page
    arrives as one long line (menus first, the text later): it is cut into sentences, so the
    sentence that shares the claim's words is found and not the start of the page. Every passage
    is a literal part of the line."""
    if len(line) <= limit:
        return [line]
    parts: list[str] = []
    for piece in _ESCAPED_BREAK.split(line):
        parts += [p.strip() for p in _SENTENCE_END.split(piece) if p.strip()]
    return parts or [line]


def _window_start(text: str, wanted: set[str]) -> int:
    """Where a too long passage is quoted from: a few words before the first word of the claim, at
    a word boundary. A web page's menu has no sentence end, so its sentence starts with the menu."""
    for match in _WORD.finditer(text):
        if match.group(0).lower() in wanted:
            back = text.rfind(" ", 0, max(match.start() - 40, 0))
            return back + 1 if back >= 0 else 0
    return 0


def best_quote(claim: str, excerpt: str, limit: int = QUOTE_CHARS) -> str:
    """The passage of ``excerpt`` that shares the most words with ``claim``, as it stands there
    (whitespace folded, shortened with an ellipsis, never reworded); the start of the excerpt
    when no word is shared; empty for an empty excerpt."""
    lines = [" ".join(line.split()) for line in re.split(r"[\r\n]+", excerpt) if line.strip()]
    lines = [part for line in lines for part in _passages(line, limit)]
    if not lines:
        return ""
    wanted = {w.lower() for w in _WORD.findall(claim)}
    scored = [
        (len(wanted & {w.lower() for w in _WORD.findall(line)}), -i, line)
        for i, line in enumerate(lines)
    ]
    score, _, best = max(scored)
    chosen = best if score > 0 else lines[0]
    if len(chosen) <= limit:
        return chosen
    start = _window_start(chosen, wanted) if score > 0 else 0
    window = chosen[start:]
    return window if len(window) <= limit else window[: limit - 1].rstrip() + "…"


def slug(text: str, limit: int = 40) -> str:
    words = re.sub(r"[^a-z0-9]+", "-", _ascii(text).lower()).strip("-")
    return words[:limit].strip("-") or "akte"


def _ascii(text: str) -> str:
    table = str.maketrans(
        {"ä": "ae", "ö": "oe", "ü": "ue", "ß": "ss", "Ä": "Ae", "Ö": "Oe", "Ü": "Ue"}
    )
    return text.translate(table)


def knowledge_path(handover_id: str, question: str, created: datetime | None) -> str:
    day = (created or datetime.now()).strftime("%Y%m%d")
    return f"akte-{day}-{slug(question)}-{handover_id[:8]}.md"


def _when(value: datetime | None) -> str:
    return value.strftime("%Y-%m-%d %H:%M UTC") if value else ""


def _text(value: Any) -> str:
    return " ".join(str(value or "").split())


def _quote_lines(prefix: str, quotes: list[dict[str, str]]) -> list[str]:
    return [f"> „{q['quote']}“ — {prefix}{q['ref']}{q['where']}" for q in quotes]


def build(
    handover: dict[str, Any], card: dict[str, Any] | None, language: str | None = None
) -> tuple[dict[str, Any], str]:
    """The file as data and as Markdown. ``handover`` is the row as plain values (``id``,
    ``from_application``, ``to_application``, ``status``, ``dossier``, ``error``, the three times);
    ``card`` the report card of the check (``report.build``), or None while there is none."""
    dossier = handover["dossier"]
    lang = language or language_of(str(dossier.get("question") or ""))
    t = LABELS.get(lang, LABELS["en"])
    evidence = {e["ref"]: e for e in (card or {}).get("evidence") or []}

    def where(ref: str) -> str:
        e = evidence.get(ref)
        if not e:
            return ""
        return f" ({e.get('capability') or '?'} {t['on']} {e.get('node') or '?'})"

    quotes: dict[int, list[dict[str, str]]] = {}
    findings = (card or {}).get("findings") or []
    for i, f in enumerate(findings):
        found = []
        for ref in f.get("evidence") or []:
            e = evidence.get(ref)
            q = best_quote(str(f.get("claim") or ""), str((e or {}).get("excerpt") or ""))
            if q:
                found.append({"ref": ref, "quote": q, "where": where(ref)})
        if found:
            quotes[i] = found

    timeline = [{"at": _when(handover.get("created_at")), "event": "proposed"}]
    if handover.get("sent_at"):
        timeline.append({"at": _when(handover["sent_at"]), "event": "sent"})
    if handover.get("finished_at"):
        timeline.append({"at": _when(handover["finished_at"]), "event": handover["status"]})

    question = _text(dossier.get("question"))
    lines = [f"# {t['file']}: {question[:120]}", ""]
    lines += [
        f"## {t['facts']}",
        "",
        "| | |",
        "|---|---|",
        f"| {t['status']} | {t.get('st_' + handover['status'], handover['status'])} |",
        f"| {t['from_to']} | {handover['from_application']} → {handover['to_application']} |",
        f"| {t['created']} | {_when(handover.get('created_at'))} |",
    ]
    if handover.get("sent_at"):
        lines.append(f"| {t['sent']} | {_when(handover['sent_at'])} |")
    if handover.get("finished_at"):
        lines.append(f"| {t['finished']} | {_when(handover['finished_at'])} |")
    lines += [f"| {t['id']} | `{handover['id']}` |", ""]

    lines += [f"## {t['request']}", "", f"**{t['question']}:** {question}"]
    asked = _text(dossier.get("asked"))
    if asked and asked != question:
        lines += ["", f"**{t['asked']}:** {asked}"]
    lines += ["", f"**{t['expected']}:** {_text(dossier.get('expected'))}"]
    if dossier.get("context"):
        lines += ["", f"**{t['context']}:**", ""]
        lines += [f"> {q['role']}: {_text(q['text'])}" for q in dossier["context"]]
    withheld = "; ".join(_text(w) for w in dossier.get("withheld") or []) or t["withheld_none"]
    lines += ["", f"**{t['withheld']}:** {withheld}", ""]

    lines += [f"## {t['report']}", ""]
    if card is None:
        lines += [
            t["no_report"]
            if not handover.get("error")
            else f"**{t['error']}:** {handover['error']}"
        ]
    else:
        if card.get("summary"):
            lines += [f"### {t['summary']}", "", _text(card["summary"]), ""]
        if findings:
            lines += [f"### {t['findings']}", ""]
            for i, f in enumerate(findings):
                marks = f" [{', '.join(f['evidence'])}]" if f.get("evidence") else ""
                extra = (
                    f" — {t['contradicts']} {', '.join(f['contradicts'])}"
                    if f.get("contradicts")
                    else ""
                )
                lines.append(
                    f"{i + 1}. **{t.get('tag_' + f['tag'], f['tag'])}** — "
                    f"{_text(f['claim'])}{marks}{extra}"
                )
                lines += [f"   {line}" for line in _quote_lines("", quotes.get(i, []))]
            lines.append("")
        if card.get("assumptions"):
            lines += [f"### {t['assumptions']}", ""]
            for a in card["assumptions"]:
                how = (
                    f" ({t['to_check']}: {_text(a['confirm_with'])})"
                    if a.get("confirm_with")
                    else ""
                )
                lines.append(f"- {_text(a['claim'])}{how}")
            lines.append("")
        p = card.get("proposal")
        if p:
            lines += [f"### {t['proposal']}", "", _text(p["text"])]
            if p.get("impact"):
                lines.append(f"- {t['impact']}: {_text(p['impact'])}")
            if p.get("rollback"):
                lines.append(f"- {t['rollback']}: {_text(p['rollback'])}")
            lines.append("")
        gaps: list[str] = []
        if card.get("incomplete"):
            why = card.get("incomplete_reason")
            gaps.append(f"{t['incomplete']}{f': {why}' if why else ''}")
        coverage = card.get("coverage") or {}
        if coverage.get("asked"):
            gaps.append(f"{t['checked']}: {', '.join(coverage['asked'])}")
        if coverage.get("not_asked"):
            gaps.append(f"{t['not_checked']}: {', '.join(coverage['not_asked'])}")
        gaps += [
            f"{t['not_retrieved']}: {report_card.failure_text(f)}"
            for f in card.get("failures") or []
        ]
        gaps += [_text(n) for n in card.get("notes") or []]
        if handover.get("error"):
            gaps.append(f"{t['error']}: {_text(handover['error'])}")
        if gaps:
            lines += [f"### {t['open']}", ""] + [f"- {g}" for g in gaps] + [""]
        if evidence:
            lines += [f"## {t['sources']}", ""]
            for e in evidence.values():
                tag = t.get("tag_" + str(e.get("tag")), e.get("tag"))
                head = (
                    f"**{e['ref']}** · {tag} · `{e.get('capability') or '?'}` "
                    f"{t['on']} {e.get('node') or '?'}"
                )
                lines.append(f"- {head}")
                if e.get("input"):
                    lines.append(f"  - {t['input']}: `{_text(e['input'])[:200]}`")
                if e.get("url"):
                    lines.append(f"  - {e['url']}")
                excerpt = _text(e.get("excerpt"))
                if excerpt:
                    cut = excerpt[:SOURCE_CHARS]
                    dots = "…" if len(excerpt) > SOURCE_CHARS else ""
                    more = (
                        f" ({t['cut']})"
                        if len(excerpt) > SOURCE_CHARS or e.get("truncated")
                        else ""
                    )
                    lines.append(f"  - {t['quote']}: „{cut}{dots}“{more}")
            lines.append("")

    lines += [f"## {t['timeline']}", ""]
    for step in timeline:
        lines.append(f"- {step['at']} — {t['ev_' + step['event']]}")

    data = {
        "version": VERSION,
        "language": lang,
        "request": dossier,
        "timeline": timeline,
        "report": card,
        "quotes": {str(i): q for i, q in quotes.items()},
    }
    return data, "\n".join(lines).rstrip() + "\n"
