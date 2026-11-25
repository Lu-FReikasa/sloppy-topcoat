"""Guards on a chat answer before it is stored, after the model wrote it.

Two things a small model gets wrong, and the chat must not pass on as fact:

- a tool call written as text (a fenced block with a capability name, or the ``[function=...]``
  markup). Nothing ran, so the text is no answer and is replaced by a plain notice.
- a claim that something was checked, or what its state is, in an answer that had no check in
  this turn. The claim stays, marked as not checked.

Both live here, next to the agent's own checks of its drafts (agent_tests, agent_done), so the
chat and the agent use one definition of an unbacked claim.
"""

import re
from dataclasses import dataclass

# a fenced or inline block that holds only a capability name: ```bash repo.test_status```
_CALL_AS_TEXT = re.compile(r"```[a-z]*\s*[a-z_][a-z0-9_]*(?:\.[a-z0-9_]+)+\s*```", re.I)
_MARKUP = re.compile(r"\[(?:function|parameter)=|</?tool_call>|</?function_calls>", re.I)
# a sentence that says something was checked or what its state is
_CLAIM = re.compile(
    r"\bich habe\b[^.!?\n]{0,50}\b(?:geprüft|nachgesehen|abgefragt|kontrolliert)\b"
    r"|\bi (?:have )?(?:checked|looked|verified)\b"
    r"|\b(?:läuft|laufen|running)\s+(?:noch|gerade|bereits|still)\b"
    r"|\bis still running\b"
    r"|\b(?:ist|sind)\s+(?:fertig|abgeschlossen|erfolgreich)\b"
    r"|\b(?:is|are)\s+(?:done|finished|complete)\b"
    r"|\btests?\s+(?:sind\s+|ist\s+)?(?:bestanden|passed)\b",
    re.I,
)
# an action the answer says was done: the assistant itself never creates, sends or deletes
# anything; what it prepares waits for the person's click (D15). Live 2026-11-12: "Ich habe die
# Übergabe manuell abgeschlossen", "Ich habe eine Aufgabe … angelegt".
_DONE = re.compile(
    r"\b(?:ich habe|habe ich|hab ich|wir haben)\b[^.!?\n]{0,90}?\b(?:angelegt|erstellt|gespeichert|"
    r"abgeschlossen|abgeschickt|übergeben|gesendet|verschickt|eingetragen|gelöscht|freigegeben|"
    r"ausgeführt|erledigt|umgesetzt)\b"
    r"|\b(?:wurde|wurden)\b[^.!?\n]{0,60}?\b(?:abgeschlossen|abgeschickt|übergeben|gesendet|"
    r"verschickt|gelöscht|freigegeben|ausgeführt)\b"
    r"|\bi(?:'ve| have)?\s+(?:created|saved|sent|submitted|completed|deleted|filed|"
    r"handed (?:it )?over|applied|executed)\b",
    re.I,
)
# ... about a thing that can be created or sent in Topcoat (a list "erstellt" in the answer is fine)
_OBJECT = re.compile(
    r"\b(?:aufgabe|projekt|übergabe|notiz|eintrag|termin|erinnerung|datei|ticket|issue|branch|"
    r"commit|pull request|e-?mail|nachricht|änderung|anfrage|auftrag|recherche|task|project|"
    r"hand-?over|note|entry|reminder|file|change|request|message)\w*",
    re.I,
)
# a sentence that says it is prepared and waits: that is what the assistant may do
_PREPARED = re.compile(
    r"vorbereitet|vorgemerkt|wartet auf|warten auf|bestätig|noch nicht|prepared|waits? for|confirm|"
    r"not (?:yet )?sent",
    re.I,
)
# a button written as text: the hub draws the real ones (live 2026-11-12: "[Button: Aufgabe …
# speichern]")
_FAKE_BUTTON = re.compile(r"\[\s*(?:button|knopf|schaltfläche)\s*:[^\]\n]{0,160}\]", re.I)
_NOT_DONE = {
    "de": "Hinweis: Der Assistent legt selbst nichts an und schickt nichts ab. Was er vorbereitet "
    "hat, wartet auf deinen Klick: als Karte an dieser Antwort oder in Start unter „Übergaben“.",
    "en": "Note: the assistant creates and sends nothing itself. What it prepared waits for your "
    'click: as a card at this answer or in Start under "Hand-overs".',
}


def claimed_actions(text: str) -> list[str]:
    """Sentences that say an action was done (not prepared): the assistant cannot have done it."""
    out: list[str] = []
    for part in re.split(r"(?<=[.!?])\s+|\n+", text):
        sentence = part.strip(" -*•\t")
        if (
            sentence
            and _DONE.search(sentence)
            and _OBJECT.search(sentence)
            and not _PREPARED.search(sentence)
        ):
            out.append(sentence)
    return out


_NOTICE = {
    "de": "Hinweis: Das wurde in dieser Unterhaltung nicht geprüft.",
    "en": "Note: this was not checked in this conversation.",
}
_REPLACED = {
    "de": (
        "Die Antwort enthielt einen Werkzeugaufruf als Text. Es wurde nichts ausgeführt. "
        "Bitte stelle die Frage noch einmal."
    ),
    "en": (
        "The answer contained a tool call written as text. Nothing was run. "
        "Please ask the question again."
    ),
}


# a guess at a cause ("könnte … vorliegen") or a piece of advice: after a check it must name its
# evidence or be marked; a sentence that hedges itself ("vermutlich") already says it is a guess
_ADVICE = re.compile(
    r"\b(?:könnte|könnten|empfehle|empfehlen|solltest|sollten sie|"
    r"stelle\b[^.!?\n]{0,40}\bein|could be|might be|may be caused|i recommend|you should)\b",
    re.I,
)
# an answer that announces a look instead of giving what was found ("Ich schaue mir … an", "Ich
# führe jetzt … aus"): after the check nothing runs any more (live 2026-11-10, the themes chat)
_ANNOUNCE = re.compile(
    # a dot inside a word (theme.ts) does not end the sentence
    r"\bich (?:schaue|sehe|lese|prüfe|suche|führe)\b(?:[^.!?\n]|\.(?=\S)){0,120}"
    r"\b(?:an|nach|jetzt|gleich|aus)\b"
    r"|\bi(?:'ll| will| am going to)\s+(?:look|check|read|search|run)\b",
    re.I,
)
_ANNOUNCED = {
    "de": "Hinweis: Diese Antwort kündigt eine weitere Prüfung an, es läuft aber keine mehr. Frag "
    "noch einmal, wenn ich nachsehen soll.",
    "en": "Note: this answer announces another look, but nothing is running any more. Ask again if "
    "I should look.",
}
_MARKER = re.compile(r"\[(?:live|doc|web)\b[^\]]*\]|\bE\d+\b")
_MARKED = re.compile(
    r"nicht geprüft|aus dem kopf|ungeprüft|not checked|unverified|from memory", re.I
)
_UNBACKED = {
    "de": "Nicht geprüft (Vermutung oder Rat ohne Beleg): {items}",
    "en": "Not checked (a guess or advice without evidence): {items}",
}


def unbacked_advice(text: str) -> list[str]:
    """Sentences of an answer that guess at a cause or give advice and name no evidence (live
    2026-11-09: "könnte eine fehlerhafte Energieverwaltung vorliegen" after an audio check that
    never looked at power settings). A question ("Soll ich …?") and a sentence that already says
    it was not checked are left alone."""
    out: list[str] = []
    for part in re.split(r"(?<=[.!])\s+|\n+", text):
        sentence = part.strip(" -*•\t")
        if not sentence or sentence.endswith("?") or not _ADVICE.search(sentence):
            continue
        if _MARKER.search(sentence) or _MARKED.search(sentence):
            continue
        out.append(sentence)
    return out


@dataclass(frozen=True)
class Review:
    text: str
    # "", "tool_markup", "claimed_action", "unchecked_claim", "announced_look", "unbacked_advice"
    reason: str


def review(text: str, *, checked: bool, language: str) -> Review:
    """The answer as it may be stored. ``checked``: a server check ran in this turn, so its
    claims are backed by what it found. ``language``: ``de`` or anything else for English."""
    lang = "de" if language == "de" else "en"
    if _CALL_AS_TEXT.search(text) or _MARKUP.search(text):
        return Review(_REPLACED[lang], "tool_markup")
    stripped = _FAKE_BUTTON.sub("", text)
    if stripped != text or claimed_actions(stripped):
        # an action that cannot have happened, or a button that is only text: said plainly
        body = re.sub(r"\n{3,}", "\n\n", stripped).rstrip()
        return Review(f"{body}\n\n_{_NOT_DONE[lang]}_", "claimed_action")
    if not checked and _CLAIM.search(text):
        return Review(f"{text.rstrip()}\n\n_{_NOTICE[lang]}_", "unchecked_claim")
    if checked and _ANNOUNCE.search(text) and not text.rstrip().endswith("?"):
        return Review(f"{text.rstrip()}\n\n_{_ANNOUNCED[lang]}_", "announced_look")
    if checked and (guesses := unbacked_advice(text)):
        # after a check the reader takes every sentence as seen: a guess must say it is one
        items = "; ".join(f"„{g[:90]}“" if lang == "de" else f"“{g[:90]}”" for g in guesses[:2])
        note = _UNBACKED[lang].format(items=items)
        return Review(f"{text.rstrip()}\n\n_{note}_", "unbacked_advice")
    return Review(text, "")
