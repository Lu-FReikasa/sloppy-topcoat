"""The report card of a server check (E3-07 D3, ARCHITECTURE D41).

A check (an agent task) ends with a diagnosis and the evidence its tools returned. The card is
built from exactly that, by the hub, in code: the model does not write the report and does not
repeat it in the chat bubble. What the card contains:

* the short summary of the check;
* findings, each with the evidence marks (``E1`` …) that show it and its tag (live, doc, web) —
  the card links every mark to the evidence excerpt it names;
* assumptions, kept apart from the findings, each with the call that would settle it;
* at most one proposal (advice only — nothing is changed) with its impact and its way back; a
  proposal whose impact or way back the check did not state says so instead of inventing one;
* which machines were asked and which could have been and were not (a finding holds for the first);
* the evidence itself (tool, machine, input, the excerpt the tool returned);
* the calls that gave no result (a link that did not load, a refused tool), so a missing source
  is never silent;
* whether the check is incomplete (it stopped early, or failed after it had looked at something).

``text`` renders the same card as plain Markdown: that is what the conversation stores as the
message content (and what a later question is answered from)."""

import re
from collections.abc import Sequence
from typing import Any

from topcoat_hub import stop_reasons
from topcoat_hub.check_order import TABLE_ROWS, table_markdown

VERSION = 1
EXCERPT_CHARS = 2000  # of one evidence excerpt on the card
TOTAL_EVIDENCE_CHARS = 30000  # of all excerpts on one card
LIVE_TAGS = ("live", "doc", "web")

_LOGS = ("docker.logs", "compose.logs", "journal.logs")


def _excerpt(capability: str | None, text: str, room: int) -> tuple[str, bool]:
    """The excerpt cut to ``room`` characters: the end of a log (the newest lines), the start of
    anything else."""
    if len(text) <= room:
        return text, False
    return (text[-room:] if capability in _LOGS else text[:room]), True


def build(
    task: Any | None,
    evidence: Sequence[Any],
    *,
    error: str = "",
    known_nodes: Sequence[str] = (),
) -> dict[str, Any] | None:
    """The card for a finished (or failed) check; ``None`` when there is nothing to show — no
    diagnosis and nothing seen."""
    diagnosis: dict[str, Any] = (task.diagnosis or {}) if task is not None else {}
    if not diagnosis.get("summary") and not evidence:
        return None
    findings: list[dict[str, Any]] = []
    assumptions: list[dict[str, Any]] = []
    for f in diagnosis.get("findings") or []:
        if not isinstance(f, dict) or not f.get("claim"):
            continue
        refs = [r for r in (f.get("evidence") or []) if isinstance(r, str)]
        if f.get("tag") in LIVE_TAGS:
            item: dict[str, Any] = {"claim": f["claim"], "tag": f["tag"], "evidence": refs}
            if f.get("contradicts"):
                item["contradicts"] = [r for r in f["contradicts"] if isinstance(r, str)]
            if isinstance(f.get("quote"), str) and f["quote"]:
                item["quote"] = f["quote"]  # the words of the result that show it (checked)
            elif f.get("unquoted"):
                item["unquoted"] = True
            findings.append(item)
        else:
            assumptions.append(
                {
                    "claim": f["claim"],
                    "confirm_with": f.get("confirm_with") or "",
                    "evidence": refs,
                    **({"downgraded": True} if f.get("downgraded") else {}),
                    # the words the check said its result shows, and which it does not show
                    **({"misquoted": f["misquoted"]} if f.get("misquoted") else {}),
                }
            )
    # A machine the summary names without any evidence from it is not a finding (T3.5): the card
    # says it was not asked, so a sentence like "node-x is fine" cannot pass as checked.
    coverage = diagnosis.get("coverage") or {}
    known = [
        *known_nodes,
        *(str(n) for n in coverage.get("asked") or []),
        *(str(n) for n in coverage.get("not_asked") or []),
    ]
    seen_nodes = [str((e.source or {}).get("node") or "") for e in evidence]
    # Topcoat's own state of a machine is evidence about that machine (it names it in its input)
    for e in evidence:
        src = e.source or {}
        what = (src.get("input") or {}).get("what") if isinstance(src.get("input"), dict) else None
        if str(src.get("capability") or "").startswith("topcoat.") and isinstance(what, str):
            seen_nodes.append(what)
    for name in unseen_mentions(str(diagnosis.get("summary") or ""), known, seen_nodes):
        assumptions.append(
            {
                "claim": f"{name} was not asked",
                "confirm_with": "",
                "evidence": [],
                "downgraded": True,
            }
        )
    proposal = None
    if diagnosis.get("next"):
        proposal = {
            "text": str(diagnosis["next"]),
            "impact": str(diagnosis.get("impact") or ""),
            "rollback": str(diagnosis.get("rollback") or ""),
            "advice_only": True,
        }
    cards: list[dict[str, Any]] = []
    room_left = TOTAL_EVIDENCE_CHARS
    for e in evidence:
        src = e.source or {}
        text, cut = _excerpt(
            src.get("capability"), e.excerpt or "", min(EXCERPT_CHARS, max(room_left, 0))
        )
        room_left -= len(text)
        cards.append(
            {
                "ref": e.ref,
                "tag": e.tag,
                "node": src.get("node"),
                "capability": src.get("capability"),
                "input": src.get("input"),
                **({"url": src["url"]} if src.get("url") else {}),
                "excerpt": text,
                "truncated": cut or (e.bytes or 0) > len(text.encode("utf-8", "ignore")),
            }
        )
    status = task.status if task is not None else "failed"
    stop_reason = task.stop_reason if task is not None else None
    incomplete = bool(error) or status != "done" or cut_short(stop_reason, diagnosis)
    return {
        "version": VERSION,
        "task_id": str(task.id) if task is not None else None,
        "status": status,
        "incomplete": incomplete,
        "incomplete_reason": (error or stop_reason or (None if status == "done" else status)),
        "summary": str(diagnosis.get("summary") or ""),
        "findings": findings,
        "assumptions": assumptions,
        # a summary can lean on what was not seen: the card says so where assumptions exist
        "summary_has_assumptions": bool(assumptions) and bool(diagnosis.get("summary")),
        "proposal": proposal,
        # the result form "table" of a check order: the rows as the check wrote them
        "table": [r for r in (diagnosis.get("table") or []) if isinstance(r, dict)][:TABLE_ROWS]
        if isinstance(diagnosis.get("table"), list)
        else [],
        "evidence": cards,
        "coverage": {
            "asked": [str(n) for n in (diagnosis.get("coverage") or {}).get("asked") or []],
            "not_asked": [str(n) for n in (diagnosis.get("coverage") or {}).get("not_asked") or []],
        },
        "notes": [str(n) for n in (diagnosis.get("notes") or [])][:6],
        # calls that gave no result (a link that did not load, a refused or failed tool)
        "failures": [f for f in (diagnosis.get("failed_calls") or []) if isinstance(f, dict)][:8],
        "calls": task.tool_calls if task is not None else 0,
    }


# Stops that mean "looked long enough" rather than "could not go on": with findings that rest on
# evidence, the check answered.
SOFT_STOPS = frozenset({"no_progress", "max_tool_calls", "max_turns", "repeated_calls"})


def cut_short(stop_reason: str | None, diagnosis: dict[str, Any] | None) -> bool:
    """Is a check that stopped for ``stop_reason`` incomplete? A soft stop with at least one
    finding backed by evidence is not; any other stop is."""
    if not stop_reason:
        return False
    if stop_reason not in SOFT_STOPS:
        return True
    findings = (diagnosis or {}).get("findings") or []
    return not any(isinstance(f, dict) and f.get("evidence") for f in findings)


def failure_text(f: dict[str, Any]) -> str:
    """One failed call as a line: what was asked for and why it gave nothing."""
    what = f.get("target") or f"{f.get('capability') or '?'} on {f.get('node') or '?'}"
    why = f.get("message") or f.get("code") or "failed"
    return f"{what}: {why}"


def _marks(refs: Sequence[str]) -> str:
    return f" [{', '.join(refs)}]" if refs else ""


def text(report: dict[str, Any]) -> str:
    """The card as Markdown (the stored message content; the browser builds its copy text from
    the structure, in the user's language)."""
    lines: list[str] = []
    if report.get("incomplete"):
        code = str(report.get("incomplete_reason") or "")
        why = stop_reasons.short(code, "en")
        lines.append(f"**Incomplete check{f': {why}' if why else ''}.**")
    if report.get("summary"):
        lines.append(f"**Summary:** {report['summary']}")
        if report.get("summary_has_assumptions"):
            lines.append("_Parts of the summary rest on assumptions, see below._")
    if report.get("findings"):
        lines.append("**Findings:**")
        for f in report["findings"][
            :6
        ]:  # the card keeps at most six findings in the history (T3.8)
            extra = (
                f" — contradicts the notes {', '.join(f['contradicts'])}"
                if f.get("contradicts")
                else ""
            )
            lines.append(f"- [{f['tag']}] {f['claim']}{_marks(f.get('evidence') or [])}{extra}")
    table = table_markdown(report.get("table"))
    if table:
        lines.append(table)
    if report.get("assumptions"):
        lines.append("**Assumptions (not seen, not confirmed):**")
        for a in report["assumptions"]:
            how = f" (to check: {a['confirm_with']})" if a.get("confirm_with") else ""
            lines.append(f"- {a['claim']}{how}")
    p = report.get("proposal")
    if p:
        lines.append(f"**Proposal (advice only, nothing was changed):** {p['text']}")
        if p.get("impact"):
            lines.append(f"- Impact: {p['impact']}")
        if p.get("rollback"):
            lines.append(f"- Rollback: {p['rollback']}")
    coverage = report.get("coverage") or {}
    if coverage.get("not_asked"):
        lines.append(
            f"**Checked on:** {', '.join(coverage.get('asked') or []) or 'none'}; "
            f"**not checked:** {', '.join(coverage['not_asked'])}"
        )
    for f in report.get("failures") or []:
        lines.append(f"**Not retrieved:** {failure_text(f)}")
    for note in report.get("notes") or []:
        lines.append(f"_Note: {note}_")
    if report.get("evidence"):
        refs = ", ".join(
            f"{e['ref']} ({e.get('capability') or '?'} on {e.get('node') or '?'})"
            for e in report["evidence"]
        )
        lines.append(f"**Evidence:** {refs}")
    if report.get("task_id"):
        # the record of this check: the way back to its excerpts, kept out of the chat history
        lines.append(f"Akte: {report['task_id']}")
    return "\n".join(lines)


def unseen_mentions(summary: str, known: Sequence[str], seen: Sequence[str]) -> list[str]:
    """The machines a summary names that the check never looked at (pruefauftrag T3.5): a name in
    ``known`` that appears in the summary while no evidence came from it. Such a sentence is an
    assumption, not a finding."""
    text = summary.lower()
    looked = {s.lower() for s in seen if s}
    out: list[str] = []
    for name in known:
        low = name.lower()
        if not low or low in looked or name in out:
            continue
        if re.search(rf"(?<![\w-]){re.escape(low)}(?![\w-])", text):
            out.append(name)
    return out


def closing_fallback(report: dict[str, Any] | None, language: str) -> str:
    """The sentence under a report card when the model wrote no closing remark of its own: what the
    check found, or that it found nothing usable, and where the details are. Empty without a card.
    The hub writes it so a check never ends in a folded card and silence."""
    if not report:
        return ""
    de = language == "de"
    summary = " ".join(str(report.get("summary") or "").split())
    reason = str(report.get("incomplete_reason") or "")
    why = stop_reasons.short(reason, "de" if de else "en")
    if report.get("incomplete"):
        head = (
            f"Die Prüfung ist nicht ganz fertig geworden ({why})."
            if de
            else f"The check did not finish completely ({why})."
        )
    else:
        head = "Die Prüfung ist fertig." if de else "The check is done."
    if summary:
        lead = "Das hat sich bis dahin gezeigt:" if de else "This is what it showed so far:"
        body = f" {lead} {summary}"
    else:
        body = (
            " Es gibt kein belastbares Ergebnis."
            if de
            else " There is no result I can stand behind."
        )
    tail = (
        " Die Einzelheiten stehen im Bericht darüber."
        if de
        else " The details are in the report above."
    )
    return f"{head}{body}{tail}"
