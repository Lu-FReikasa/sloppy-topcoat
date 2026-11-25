"""Is the agent's draft really finished, and does it say only what the task saw?

Two checks of a ``finish`` the hub makes itself, no model, no guessing (the same idea as
:mod:`agent_tests` for test results):

* **announced, not done**: a draft whose text promises a step ("I will now check ...", "ich pruefe
  das gleich") has not done it. It goes back once: do it with a tool call, or say plainly that it
  was not done. A finish reports what happened, not what is intended.
* **a branch nobody saw**: the work branch is part of the report the user acts on (the push goes
  to it). A ``topcoat/...`` branch the draft names that no ``repo.*`` result of the task showed and
  the question did not name is invented.
"""

import json
import re
from dataclasses import dataclass, field
from typing import Any

_BRANCH = re.compile(r"\btopcoat/[A-Za-z0-9](?:[A-Za-z0-9._/-]*[A-Za-z0-9])?")

# first person only: "you could run the tests next" is advice to the user, not a promise
_ANNOUNCE = re.compile(
    r"(?:\bich\s+(?:werde|will|m[oö]chte)\b[^.\n]{0,40}?\b"
    r"(?:pr[uü]fen|nachsehen|nachschauen|schauen|ansehen|testen|ausf[uü]hren|starten|lesen|"
    r"[oö]ffnen|suchen|abfragen|kontrollieren|[uü]berpr[uü]fen|durchsuchen)\b"
    r"|\bich\s+(?:pr[uü]fe|schaue|sehe|teste|lese|suche|kontrolliere)\b[^.\n]{0,25}?"
    r"\b(?:jetzt|gleich|noch|als\s+n[aä]chstes|nun)\b"
    r"|\b(?:als\s+n[aä]chstes|im\s+n[aä]chsten\s+schritt)\b[^.\n]{0,20}?\bich\b"
    r"|\blass\s+mich\b[^.\n]{0,30}?\b(?:pr[uü]fen|nachsehen|schauen|testen)\b"
    r"|\bi(?:'ll|\s+will|\s+am\s+going\s+to|\s+shall)\s+(?:now\s+|first\s+|next\s+)?"
    r"(?:check|run|look|verify|test|start|read|open|search|inspect|examine|query)\b"
    r"|\blet\s+me\s+(?:check|run|look|verify|test|start|read|open|search|inspect)\b"
    r"|\bnext,?\s+i(?:'ll|\s+will)\b)",
    re.IGNORECASE,
)
_NEGATION = re.compile(r"\b(?:nicht|kein\w*|nie|not|never|n't|no)\b", re.IGNORECASE)


@dataclass
class Branches:
    """The ``topcoat/...`` branches one task has seen: named by the question, or shown by a
    ``repo.*`` result."""

    seen: set[str] = field(default_factory=set)

    def of_prompt(self, prompt: str) -> None:
        self.seen.update(_BRANCH.findall(prompt))

    def note(self, capability: str, output: Any) -> None:
        if not capability.startswith("repo.") or output is None:
            return
        try:
            blob = json.dumps(output, default=str)
        except (TypeError, ValueError):
            return
        self.seen.update(_BRANCH.findall(blob))
        if isinstance(output, dict) and isinstance(output.get("branch"), str):
            self.seen.add(output["branch"])


def unbacked_branch(texts: list[str], branches: Branches) -> str:
    """Why the draft names a work branch the task never saw, or ``""``."""
    named = {b for t in texts if t for b in _BRANCH.findall(t)}
    invented = sorted(b for b in named if b not in branches.seen)
    if not invented:
        return ""
    shown = ", ".join(sorted(branches.seen)) or "none"
    return (
        f"Your answer names the branch {', '.join(invented)}, but no repo result of this task "
        f"showed it (branches seen: {shown}). Ask repo.status for the real branch and use exactly "
        "that name, or leave the branch out."
    )


def announced(texts: list[str]) -> str:
    """The first sentence of the draft that promises a step still to be taken, or ``""``."""
    for t in texts:
        if not t:
            continue
        for m in _ANNOUNCE.finditer(t):
            if _NEGATION.search(t[max(0, m.start() - 12) : m.end()]):
                continue
            start = max(t.rfind(".", 0, m.start()), t.rfind("\n", 0, m.start())) + 1
            end = t.find(".", m.end())
            return t[start : end if end != -1 else len(t)].strip()[:200]
    return ""


def unfinished(texts: list[str]) -> str:
    """The note that sends a draft back for a promise it did not keep, or ``""``."""
    said = announced(texts)
    if not said:
        return ""
    return (
        f'Your draft announces a step it did not take: "{said}". Take it now with a tool call, '
        "or say plainly that it was not done and why. A finish reports what you did and saw, "
        "not what you intend to do."
    )


# a change the draft says is prepared or made (live 2026-11-18, Projekte chat, gpt-oss: "a
# changeset has been prepared", "The following change sentence will add ...", "Task 3.1.1 updated"
# - and no proposal was ever made)
_CHANGE_NOUN = re.compile(
    r"change ?sets?|change sentence|änderungss[aä]tz\w*|änderungsvorschl\w*|vorschlag|proposal"
    r"|\b(?:task|aufgabe|node|eintrag)\b",
    re.IGNORECASE,
)
_DONE_VERB = re.compile(
    r"\b(?:prepared|vorbereitet|ready|bereit|created|erstellt|angelegt|submitted|eingereicht"
    r"|updated|aktualisiert|geändert|überarbeitet|will add|adds|fügt)\b",
    re.IGNORECASE,
)
_SENTENCE = re.compile(r"[^.!?\n]+")
# "No change set can be prepared", "wurde nicht angelegt": a sentence that says it is NOT done
_NEGATED = re.compile(
    r"\b(?:no|not|cannot|can't|nothing|never|kein\w*|nicht|nichts|noch nicht)\b", re.IGNORECASE
)


def claims_change(texts: list[str]) -> str:
    """The first sentence of a draft that says a change is prepared or made, or ``""``."""
    for text in texts:
        for sentence in _SENTENCE.findall(text or ""):
            if _NEGATED.search(sentence):
                continue
            if _CHANGE_NOUN.search(sentence) and _DONE_VERB.search(sentence):
                return " ".join(sentence.split())[:200]
    return ""
