"""Hand-over of heavy chat requests to the task queue.

A request that says it may take long ("im Hintergrund", "take your time", "write a report") is not
answered in the chat: the hub files it as a task, answers with a sentence of its own (no model is
asked, so no small model can claim anything) and the result lands in the library. A request that
says it is urgent stays in the chat. Rules, no model call.

Only the user's words decide. The speed of a node does not: a task waits in the queue until any
suitable node is free (no timer, no waiting for a "fast" node), and the level "fast" of the effort
control keeps the request in the chat.
"""

import re

_URGENT = re.compile(r"\b(dringend|sofort|schnell|asap|urgent|urgently|right now|quick)\b", re.I)
_LATER = re.compile(
    r"\b(im hintergrund|in ruhe|darf (?:gern\w* )?(?:länger|etwas länger|lange) dauern|"
    r"kein stress|keine eile|nicht eilig|nicht dringend|bis morgen|über nacht|"
    r"als aufgabe|gib (?:das|es) (?:als aufgabe )?ab|"
    r"in the background|take your time|no rush|not urgent|overnight|as a task|"
    r"hand (?:it|this) (?:over|off))\b",
    re.I,
)
_REPORT = re.compile(
    r"\b(schreib\w* (?:mir )?(?:einen |den )?(?:ausführlichen )?(?:bericht|report|analyse)|"
    r"erstell\w* (?:mir )?(?:einen |den )?(?:ausführlichen )?(?:bericht|report|analyse)|"
    r"write (?:me )?an? (?:detailed |full )?(?:report|analysis)|"
    r"(?:detailed|full) (?:report|analysis))\b",
    re.I,
)

ACKNOWLEDGEMENTS = {
    "de": "Das gebe ich als Aufgabe ab. Das Ergebnis liegt danach in der Ablage.",
    "en": "I'm handing this over as a task. The result will be in the library.",
}


def wanted(text: str) -> bool:
    """Did the user ask to hand it over (or ask for a report)? Urgent wording says no."""
    if _URGENT.search(text):
        return False
    return bool(_LATER.search(text) or _REPORT.search(text))


def reason(level: str, text: str) -> str | None:
    """Why this request goes to the queue: ``"asked"`` (the user said so) or ``None`` (answer in
    the chat). The level ``fast`` of the effort control never hands over."""
    if level == "fast":
        return None
    return "asked" if wanted(text) else None
