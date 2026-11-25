"""A conversation that goes round in circles, seen without a model (live 2026-11-17: "ja bitte" to
"Soll ich dir erklären, wie …?" gave the same diagnosis again, ending with the same offer; the
next "ja" would have done it once more).

Two signs: an answer that ends with the offer the previous answer ended with, and a report whose
summary is the one of the report before it. The first is cut from the stored answer (the offer was
made, the person said yes, offering it again only loops); the second is a finding of the hub's
own check of the answer."""

import re

# an offer at the end of an answer: "Soll ich …?", "Möchtest du, dass ich …?"
OFFER = re.compile(
    r"\b(?:soll ich|möchtest du,? dass ich|willst du,? dass ich|shall i|should i|do you want me to|"
    r"want me to)\b[^?]*\?\s*$",
    re.IGNORECASE,
)
SAME_OFFER = 0.6  # share of words two offers have in common to count as the same
SAME_RESULT = 0.75  # the same for two report summaries

_WORD = re.compile(r"[a-zäöüß0-9]{3,}", re.IGNORECASE)


def _words(text: str) -> set[str]:
    return {w.lower() for w in _WORD.findall(text)}


def similar(a: str, b: str) -> float:
    """Shared words of two texts over all their words (0 when one is empty)."""
    wa, wb = _words(a), _words(b)
    if not wa or not wb:
        return 0.0
    return len(wa & wb) / len(wa | wb)


def last_sentence(text: str) -> str:
    return re.split(r"(?<=[.!?])\s+", text.strip())[-1].strip() if text.strip() else ""


def closing_offer(text: str) -> str:
    """The offer an answer ends with, or ""."""
    sentence = last_sentence(text)
    return sentence if OFFER.search(sentence) else ""


def without_repeated_offer(text: str, earlier_answers: list[str]) -> str:
    """The answer without its closing offer when one of the two answers before it ended with the
    same offer; else the answer as it is."""
    offer = closing_offer(text)
    if not offer:
        return text
    for earlier in earlier_answers[-2:]:
        before = closing_offer(earlier)
        if before and similar(offer, before) >= SAME_OFFER:
            cut = text.rstrip()
            return cut[: len(cut) - len(offer)].rstrip()
    return text


def report_summary(content: str) -> str:
    """The summary line of a stored report card."""
    for line in content.splitlines():
        m = re.match(r"\*\*(?:Summary|Zusammenfassung):\*\*\s*(.+)", line.strip())
        if m:
            return m.group(1).strip()
    return ""


def repeated_result(new_report: str, previous_report: str) -> dict[str, str] | None:
    """A finding of the hub's check when a report says what the report before it said."""
    new, old = report_summary(new_report), report_summary(previous_report)
    if not new or not old or similar(new, old) < SAME_RESULT:
        return None
    return {
        "claim": new[:300],
        "reason": "the same result as the check before it: the last message was probably not "
        "answered, only the earlier question again",
        "evidence": None,
        "severity": "minor",
        "rule": "repeated_result",
    }
