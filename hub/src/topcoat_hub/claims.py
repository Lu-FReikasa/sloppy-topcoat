"""An answer claims only what the hub saw happen (AGT-01, ARCHITECTURE D42).

D42 turned "I noted that" into a fact of the hub. This does the same for changes. The check behind
a chat answer only reads; a change is prepared for the person to confirm, and it exists only when
the hub has it on record. When the closing answer says that something was set, changed or deleted,
or that a change is ready to confirm, the hub compares that with its record and, if it does not
match, writes the true state under the answer: nothing was changed, and why.

The patterns are strict on purpose. A missed claim costs nothing (the answer reads as before); a
wrong flag puts a warning under a good answer. An offer ("I can set it to dropped"), a question and
a statement about history ("the task was created on 3 May") are not claims."""

import re
from typing import Any

_DE_DONE_VERBS = (
    "gesetzt|geändert|gelöscht|angelegt|erstellt|verschoben|abgeschlossen|aktualisiert|ergänzt|"
    "hinzugefügt|entfernt|verworfen|erledigt|umbenannt|gestartet|neu gestartet"
)
_EN_DONE_VERBS = (
    "set|changed|deleted|created|moved|completed|updated|added|removed|dropped|renamed|"
    "restarted|started|marked"
)
# what makes a passive sentence a report of an action just taken, not a statement about history
_DE_JUST = (
    r"jetzt|nun|soeben|erfolgreich|wie gewünscht|auf\s+[`'„\"]?(dropped|done|erledigt|verworfen)"
)
_EN_JUST = r"now|successfully|as requested|to\s+[`'\"]?(dropped|done|completed)"

_DONE = [
    # "Ich habe die Aufgaben auf dropped gesetzt." / "Ich habe 3 Tasks gelöscht."
    re.compile(rf"\b(ich|wir) haben?\b[^.!?\n]{{0,140}}\b({_DE_DONE_VERBS})\b", re.I),
    # "Die Aufgaben wurden auf `dropped` gesetzt." (needs a sign of the action just taken)
    re.compile(
        rf"\b(wurde|wurden)\b[^.!?\n]{{0,100}}\b({_DE_JUST})\b[^.!?\n]{{0,60}}\b({_DE_DONE_VERBS})\b",
        re.I,
    ),
    re.compile(
        rf"\b(wurde|wurden)\b[^.!?\n]{{0,100}}\b({_DE_DONE_VERBS})\b[^.!?\n]{{0,60}}\b({_DE_JUST})\b",
        re.I,
    ),
    re.compile(rf"\bi('ve| have)\b[^.!?\n]{{0,140}}\b({_EN_DONE_VERBS})\b", re.I),
    re.compile(
        rf"\b(was|were|has been|have been)\b[^.!?\n]{{0,100}}\b({_EN_JUST})\b[^.!?\n]{{0,60}}"
        rf"\b({_EN_DONE_VERBS})\b",
        re.I,
    ),
    re.compile(
        rf"\b(was|were|has been|have been)\b[^.!?\n]{{0,100}}\b({_EN_DONE_VERBS})\b[^.!?\n]{{0,60}}"
        rf"\b({_EN_JUST})\b",
        re.I,
    ),
]
_READY = [
    re.compile(r"\b(bereit|fertig|vorbereitet)\b[^.!?\n]{0,60}\b(bestätig|übernehm)", re.I),
    re.compile(r"\bzur bestätigung\b[^.!?\n]{0,30}\b(bereit|vorbereitet|vorgeschlagen)", re.I),
    re.compile(
        r"\bänderungssatz\b[^.!?\n]{0,60}\b(bereit|vorbereitet|erstellt|vorgeschlagen)", re.I
    ),
    re.compile(r"\bready (for|to) (your )?(confirm|confirmation|review|apply)", re.I),
    re.compile(r"\bchange ?set\b[^.!?\n]{0,60}\b(ready|proposed|prepared|created)\b", re.I),
    re.compile(r"\bawaiting (your )?(confirmation|click)\b", re.I),
]
# an offer, a condition or a question is not a claim
_NOT_CLAIM = re.compile(
    r"\b(soll ich|möchtest du|willst du|wenn du|falls du|kann ich|könnte ich|ich kann|"
    r"shall i|should i|would you like|do you want|if you|i can|i could)\b",
    re.I,
)
_SENTENCES = re.compile(r"(?<=[.!?…])\s+|\n+")
MAX_SENTENCE = 260


def _first(patterns: list[re.Pattern[str]], text: str) -> str | None:
    for sentence in _SENTENCES.split(text):
        sentence = sentence.strip()
        if not sentence or sentence.endswith("?") or len(sentence) > MAX_SENTENCE:
            continue
        if _NOT_CLAIM.search(sentence):
            continue
        if any(p.search(sentence) for p in patterns):
            return sentence
    return None


# "... warten auf deine Bestätigung" is a claim only without a negation in its clause
# ("nothing waits for your confirmation" is the honest answer to a refused proposal). Live,
# 2026-10-31: the model wrote "Ich habe ... vorbereitet. Die Änderungen warten auf deine
# Bestätigung." without having called a single writing tool, and the strict patterns above saw
# neither sentence.
_WAITS = [
    re.compile(r"\b(warten|wartet)\b[^.!?\n]{0,40}\b(bestätig|klick|freigabe)", re.I),
    re.compile(r"\b(waits?|waiting) for (your )?(confirm|click|approval)", re.I),
]


def _waits_for_the_click(text: str) -> bool:
    for clause in re.split(r"[.;!?\n]+", text):
        if (
            any(p.search(clause) for p in _WAITS)
            and not _NEGATED.search(clause)
            and not _NOT_CLAIM.search(clause)
        ):
            return True
    return False


# While a change set waits for the click, a plain passive ("drei neue Aufgaben wurden erstellt",
# "Task 3.1.1.1 wurde unter 3.1.1 angelegt", live 2026-11-18) reports the waiting set as done;
# the strict patterns above need "jetzt" or "erfolgreich" and missed both. A sentence that dates
# what it says ("wurde am 3. Mai angelegt", "already created") speaks of history and stays.
# (a dot between digits is a task number, "3.1.1", not the end of the sentence)
_SPAN = r"(?:[^.!?\n]|(?<=\d)\.(?=\d)){0,140}"
_DONE_PASSIVE = [
    re.compile(rf"\b(wurde|wurden)\b{_SPAN}\b({_DE_DONE_VERBS})\b", re.I),
    re.compile(rf"\b(was|were|has been|have been)\b{_SPAN}\b({_EN_DONE_VERBS})\b", re.I),
]
_HISTORY = re.compile(
    r"\b(am|seit|bereits|damals|früher|zuvor|schon|already|earlier|previously|since|ago)\b", re.I
)


def claimed_change(text: str) -> tuple[bool, bool]:
    """(the answer says a change was made, the answer says a change is ready to confirm)."""
    ready = _first(_READY, text) is not None or _waits_for_the_click(text)
    return _first(_DONE, text) is not None, ready


# The patterns above are strict and miss a claim in a long sentence ("... vorbereitet, um die
# Aufgabe auf dropped zu setzen, der nun in der Benutzeroberfläche auf deine Bestätigung wartet").
# Where the record already says that a proposal was refused and nothing is prepared, a clause that
# names a state and a confirmation without a negation is read as the claim as well.
_STATE = re.compile(
    r"vorbereitet|erstellt|angelegt|bereit|fertig|vorgeschlagen|liegt|liegen|wartet|warten|"
    r"prepared|created|ready|proposed|waits|waiting|awaits|awaiting|pending",
    re.I,
)
_CONFIRM = re.compile(r"bestätig|klick|freigab|übernehm|confirm|click|approv|apply", re.I)
_NEGATED = re.compile(
    r"\b(nicht|kein|keine|keinen|keiner|nichts|ohne|not|no|nothing|cannot|can't|couldn't|"
    r"unable|without)\b|fehl|abgelehnt|refused|rejected|failed",
    re.I,
)
_CLAUSES = re.compile(r"[.;!?\n]+")


def _refused_proposal(failed: list[dict[str, Any]]) -> bool:
    """A call that prepares a change was refused or failed."""
    return any(
        "propose" in str(f.get("capability", "")).lower()
        or "changeset" in str(f.get("capability", "")).lower()
        for f in failed
    )


def _ready_in_a_long_sentence(text: str) -> bool:
    for clause in _CLAUSES.split(text):
        if (
            len(clause) <= 600
            and _STATE.search(clause)
            and _CONFIRM.search(clause)
            and not _NEGATED.search(clause)
            and not _NOT_CLAIM.search(clause)
        ):
            return True
    return False


def _reasons(failed: list[dict[str, Any]], language: str) -> str:
    out = []
    for f in failed[:3]:
        what = f"`{f.get('capability', '?')}`"
        code, message = f.get("code") or "", f.get("message") or ""
        out.append(f"{what} ({code}: {message[:160]})" if message else f"{what} ({code})")
    if not out:
        return ""
    head = "Abgelehnt:" if language == "de" else "Refused:"
    return f" {head} " + "; ".join(out) + "."


def refused_note(language: str, reason: str) -> str:
    """The line under an answer whose server check was not started because the hub's own limit
    of running tasks was reached: no check was made, and that says nothing about the servers."""
    if reason != "too_many_tasks":
        return ""
    if language == "de":
        return (
            "\n\nHinweis des Hubs: Es wurde keine Server-Prüfung gemacht, weil bereits so viele "
            "Aufgaben von dir laufen, wie der Hub gleichzeitig erlaubt. Über deine Server sagt "
            "das nichts. Sobald eine Aufgabe fertig ist, kannst du die Frage noch einmal stellen."
        )
    return (
        "\n\nNote from the hub: No server check was made, because as many tasks of yours are "
        "running as the hub allows at once. This says nothing about your servers. Ask again "
        "when a task has finished."
    )


# an answer that speaks of written texts: instructions, descriptions, worked-out tasks
_TEXT_CLAIM = re.compile(
    r"\b(anleitung\w*|beschreibung\w*|ausformuliert\w*|ausgearbeitet\w*|detailliert\w*|"
    r"unteraufgabe\w*|instructions?|descriptions?|detailed|worked out)\b",
    re.I,
)


def note(
    language: str,
    text: str,
    prepared: int,
    failed: list[dict[str, Any]],
    texts: tuple[bool, str] | None = None,
) -> str:
    """The line the hub adds under an answer whose claim of a change the record does not back,
    else the empty string. ``prepared``: changes on record that wait for the person's click.
    ``texts``: whether the prepared sets write any text, and what they change; an answer that
    speaks of instructions or descriptions while the sets only rename is told so (live
    2026-11-14: "a proposal with detailed instructions" held three new titles)."""
    if texts is not None and prepared > 0 and not texts[0] and _TEXT_CLAIM.search(text):
        de = language == "de"
        return "\n\n" + (
            "Hinweis des Hubs: Der vorbereitete Änderungssatz schreibt keine Texte, "
            f"er ändert nur: "
            f"{texts[1]}."
            if de
            else f"Note from the hub: the prepared change set writes no text, it only changes: "
            f"{texts[1]}."
        )
    done, ready = claimed_change(text)
    if not done and prepared > 0:
        passive = _first(_DONE_PASSIVE, text)
        done = passive is not None and not _HISTORY.search(passive)
    if not (done or ready) and prepared <= 0 and _refused_proposal(failed):
        ready = _ready_in_a_long_sentence(text)
    if not (done or ready):
        return ""
    de = language == "de"
    if prepared <= 0:
        base = (
            "Es wurde nichts geändert, und es liegt keine Änderung zur Bestätigung vor."
            if de
            else "Nothing was changed, and no change waits for your confirmation."
        )
        return (
            "\n\n"
            + ("Hinweis des Hubs: " if de else "Note from the hub: ")
            + base
            + (_reasons(failed, language))
        )
    if not done:
        return ""  # "ready for confirmation" with a change on record is true
    n = prepared
    one = n == 1
    base = (
        f"Bisher ist nichts geändert. {n} {'Änderung liegt' if one else 'Änderungen liegen'} "
        f"zur Bestätigung bereit und {'wirkt' if one else 'wirken'} erst nach deinem Klick."
        if de
        else f"Nothing is changed yet. {n} {'change waits' if one else 'changes wait'} for your "
        "confirmation and take effect only after your click."
    )
    return "\n\n" + ("Hinweis des Hubs: " if de else "Note from the hub: ") + base
