"""Announced, not done: an answer that ends on "I will look at that now" (E7-02, D57).

A model sometimes writes that it is about to do something and then ends its turn: the person waits
for a result that never comes. This module finds that case by mechanism, the way ``claims_noted``
finds a promise to have noted something (D42): it reads only the *end* of a finished answer that
used no tool, and says which sentence announces an action. The hub then shows it as what it is
("announced, not done") and offers to do it, instead of letting the answer pass as complete.

It is deliberately narrow. An offer ("shall I look?", "if you like, I will check") is not an
announcement, a question is not one, and an announcement followed by its content ("I will explain
the steps now: 1. …") is an introduction, not a broken promise: only a sentence the answer *ends
on* counts. A missed case costs nothing (the answer reads as before); a wrong flag costs a
warning under a good answer, so the patterns are kept strict.
"""

import re

# the sentence is a plain announcement of an action that follows at once
_DE_VERBS = (
    "schaue|sehe|prüfe|überprüfe|checke|rufe|führe|starte|lese|suche|hole|frage|teste|öffne|"
    "erstelle|mache|lasse|gehe|sammle|ermittle|analysiere|durchsuche|kümmere|beginne|fange"
)
_DE_NOW = r"jetzt|nun|gleich|sofort|zunächst|zuerst|als nächstes|direkt|kurz|mal"
_EN_NOW = r"now|first|next|right away|straight away|immediately"

_ANNOUNCE = [
    # "Ich werde das jetzt prüfen." / "Ich will zuerst nachsehen"
    re.compile(rf"\bich (werde|will|möchte|muss|fange an)\b[^.!?\n]{{0,120}}\b({_DE_NOW})\b", re.I),
    # "Ich schaue mal nach den Logs." / "Ich prüfe das jetzt."
    re.compile(
        rf"\bich ({_DE_VERBS})(e|n)?\b[^.!?\n]{{0,100}}\b(nach|an|durch|auf|aus|{_DE_NOW})\b", re.I
    ),
    # "Einen Moment." / "Einen Augenblick bitte" / "Ich melde mich gleich"
    re.compile(r"^(einen |ein )?(moment|augenblick)\b", re.I),
    re.compile(
        r"\bich (melde mich|gebe dir|sage dir)\b[^.!?\n]{0,40}\b(gleich|sofort|bescheid)\b", re.I
    ),
    # "I'll check that now." / "Let me look at the logs." / "I'm going to run it"
    re.compile(
        rf"\b(i'll|i will|i am going to|i'm going to)\b[^.!?\n]{{0,100}}\b({_EN_NOW})\b", re.I
    ),
    re.compile(r"\blet me (check|look|see|run|try|fetch|search|find|pull|open|read)\b", re.I),
    re.compile(r"^(one moment|just a moment|give me a moment|hold on)\b", re.I),
]

# an offer, a condition or a question is not an announcement
_NOT_ANNOUNCE = re.compile(
    r"\b(soll ich|möchtest du|willst du|wenn du (willst|möchtest)|falls du|auf wunsch|"
    r"kann ich dir|shall i|should i|would you like|do you want|if you (like|want|wish)|"
    r"i can (also )?(check|look|run))\b",
    re.I,
)

_SENTENCES = re.compile(r"(?<=[.!?…:])\s+|\n+")
_BULLETS = "".join(chr(c) for c in (0x2013, 0x2022))  # en dash and bullet
_LEAD = re.compile(rf"^[\s>*#\-{_BULLETS}\d.)]+")  # bullets and quotes at a line start

TAIL_CHARS = 260  # only the end of the answer is read
MAX_SENTENCE = 220


def _last_sentences(text: str, count: int = 2) -> list[str]:
    tail = text.strip()[-TAIL_CHARS:]
    parts = [_LEAD.sub("", p).strip() for p in _SENTENCES.split(tail)]
    return [p for p in parts if p][-count:]


FILLER = 40  # a closing line this short ("Danke!") does not carry out what was announced


def _announces(sentence: str) -> bool:
    if sentence.endswith("?") or len(sentence) > MAX_SENTENCE:
        return False
    if _NOT_ANNOUNCE.search(sentence):
        return False
    return any(p.search(sentence) for p in _ANNOUNCE)


def announced_action(text: str) -> str | None:
    """The sentence an answer ends on when it announces an action the answer does not carry out,
    else ``None``. The last sentence counts; so does the one before it when only a short filler
    line follows ("Ich schaue nach. Einen Moment bitte.")."""
    sentences = _last_sentences(text)
    if not sentences:
        return None
    last = sentences[-1]
    if _announces(last):
        return last
    if len(sentences) == 2 and len(last) <= FILLER and _announces(sentences[0]):
        return sentences[0]
    return None
