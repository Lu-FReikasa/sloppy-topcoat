"""The short answer under a report card says only what the evidence holds (#222). No model.

Live 2026-11-27: the report kept its findings and proposals apart, but the answer the person read
under it named a menu path in another product ("Settings → Client event logging → Log Client
Events") that no tool result held: the model filled the gap from general knowledge and wrote it
down as if it had been seen. The prompt asks the model to mark such advice (``chat.py``,
``REPORT_SHOWN_RULES``); this is the mechanical part for when it does not.

What can be compared literally is compared: a menu path (two or more labels joined by ``>``,
``→``, ``\u203a`` or ``»``) and a quoted label in a sentence about a menu, a tab, a setting or a
switch. Such a pointer is backed when its labels stand in the sources (the check's results, the
conversation, the notes: the prompt the answer was written from). A sentence with a pointer that
is not backed is marked as not checked, in place, unless it already says so itself. Wording
without anything literal is left alone, like ``citation_check.py`` does.
"""

import re

MARK = {"de": "*Nicht geprüft:* ", "en": "*Not checked:* "}
MAX_MARKED = 5

# a word of a label is at most 40 characters: unbounded, a long run like "A/A/A/..." made every
# start position scan to the end of the line (quadratic, 20 s for 40 000 characters)
_LABEL = r"[A-ZÄÖÜ][\w&'/()+-]{0,40}(?:[ \t]+[\w&'/()+-]{1,40}){0,4}"
_SEP = r"[ \t]*(?:→|->|=>|\u203a|»|>)[ \t]*"
_MENU_PATH = re.compile(rf"(?<![\w>-]){_LABEL}(?:{_SEP}{_LABEL})+")
_QUOTED = re.compile(r"[\"“„«]([^\"“”„«»\n]{3,60})[\"”“»]")
# a sentence that is about a place in a GUI: then its quoted words are labels there
_UI_WORDS = re.compile(
    r"(?i)\b(menu|menü|tab|reiter|setting|settings|einstellung\w*|option|switch|schalter|button|"
    r"schaltfläche|knopf|toggle|checkbox|page|seite|dialog|section|abschnitt)\b"
)
# a sentence that leads somewhere in a GUI: only there is "A → B" a menu path ("OOM → restart",
# "Client → Proxy → Web" and German nouns in a chain of causes are no menu)
_NAV_WORDS = re.compile(
    r"(?i)\b(go to|open|navigate|click|select|choose|under|gehe?|öffne|klicke?|wähle|unter|"
    r"navigiere)\b"
)
# the sentence marks itself as unchecked already
_SAYS_UNCHECKED = re.compile(
    r"(?i)(not checked|unchecked|not verified|unverified|\[assumption\]|assumption|"
    r"nicht geprüft|ungeprüft|nicht verifiziert|\[annahme\]|annahme)"
)
_FENCE = re.compile(r"^\s*(```|~~~)")
_SENTENCE_END = re.compile(r"(?<=[.!?…])\s+(?=\S)")
_LIST_MARKER = re.compile(r"^(\s*(?:[-*+]|\d+[.)])\s+|\s*>\s*|\s*#+\s+)?")
_ESCAPE = re.compile(r"\\u([0-9a-fA-F]{4})")


def _norm(text: str) -> str:
    return " ".join(text.replace("`", "").replace("*", "").lower().split())


def _haystack(sources: str) -> str:
    # tool results come as JSON: "Fähigkeiten" stands for "Fähigkeiten"
    return _norm(_ESCAPE.sub(lambda m: chr(int(m.group(1), 16)), sources))


def _backed_path(path: str, haystack: str) -> bool:
    """Every label of the path stands in the sources. The first label may start with words of the
    sentence ("Open Settings > Me" holds "Open Settings"), the last may run on ("Nodes and pick
    one"): there a part of it is enough, from its end or from its start."""
    labels = [_norm(x) for x in re.split(_SEP, path)]
    if not labels or _norm(path) in haystack:
        return True
    for i, label in enumerate(labels):
        words = label.split()
        if i == 0:
            options = [" ".join(words[k:]) for k in range(len(words))]
        elif i == len(labels) - 1:
            options = [" ".join(words[: k + 1]) for k in range(len(words))]
        else:
            options = [label]
        if not any(o and o in haystack for o in options):
            return False
    return True


def _pointers(sentence: str, haystack: str) -> list[str]:
    """The menu paths and labels of one sentence that the sources do not hold."""
    out: list[str] = []
    plain = sentence.replace("`", "").replace("**", "")
    about_gui = bool(_UI_WORDS.search(plain))
    if about_gui or _NAV_WORDS.search(plain):
        for m in _MENU_PATH.finditer(plain):
            if not _backed_path(m.group(0), haystack):
                out.append(m.group(0).strip())
    if about_gui:
        for m in _QUOTED.finditer(plain):
            label = m.group(1).strip()
            if label and _norm(label) not in haystack and not any(label in p for p in out):
                out.append(label)
    return out


def mark(answer: str, sources: str, language: str) -> tuple[str, list[str]]:
    """The answer with every sentence marked that names a menu path or a setting no source holds,
    and those pointers (the first ``MAX_MARKED``, for the log; every such sentence is marked).
    Code blocks are left as they are (a command is not a claim about a GUI); a sentence
    that already says it is not checked keeps its words."""
    haystack = _haystack(sources)
    prefix = MARK.get(language, MARK["en"])
    found: list[str] = []
    lines = answer.split("\n")
    in_fence = False
    for n, line in enumerate(lines):
        if _FENCE.match(line):
            in_fence = not in_fence
            continue
        if in_fence or not line.strip():
            continue
        head = _LIST_MARKER.match(line)
        start = head.end() if head else 0
        body = line[start:]
        parts = _SENTENCE_END.split(body)
        changed = False
        for k, sentence in enumerate(parts):
            if _SAYS_UNCHECKED.search(sentence):
                continue
            missing = _pointers(sentence, haystack)
            if not missing:
                continue
            found += [p for p in missing if p not in found]
            parts[k] = prefix + sentence
            changed = True
        if changed:
            # the spaces between the sentences are one space now; the line's own start stays
            lines[n] = line[:start] + " ".join(parts)
    return "\n".join(lines), found[:MAX_MARKED]
