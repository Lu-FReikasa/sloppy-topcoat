"""Right after the person changed a setting, missing events are "not confirmed yet" (#224).

Live 2026-11-27: the person switched on client event logging in the Omada controller and asked a
minute later to verify it. The check read the last 24 h of events, saw no client events of an
access point and answered "client event logging is not enabled", although the source's own note
said that their absence says nothing. Events that a setting starts to log come only when
something happens (the next client joining or leaving an access point), so a log read a minute
after the change cannot show that the setting is off. No tool reads the controller's settings.

Three places use this, all without a model:

- the check's task gets ``hint`` (``agent_loop``), so the run does not conclude "off";
- a finding of the check that still says "off" is rewritten (``qualify_claim``), so the report
  card does not say it either;
- the answer under the card gets the same treatment, sentence by sentence (``guard``).
"""

import re

# the log whose missing events say nothing right after a change (the only one known so far)
EVENTS_LOG = "omada.events"

# the person says they changed a setting a moment ago (first person, "just" or minutes ago)
_JUST_CHANGED = re.compile(
    r"\bi(?:'ve| have)?\s+just\s+(?:now\s+)?(?:enabled|activated|turned|switched|changed|set|"
    r"configured|toggled|ticked|checked)\b"
    r"|\bi\s+(?:enabled|activated|turned|switched|changed|set|configured)\b[^.!?\n]{0,60}?"
    r"\b(?:just now|a (?:minute|moment) ago|(?:a few|\d+|two|three) minutes ago|seconds ago)\b"
    r"|\b(?:just|recently)\s+(?:been\s+)?(?:enabled|activated|turned on|switched on)\b"
    r"|\b(?:habe|hab)\s+(?:ich\s+)?(?:\w+\s+){0,6}?(?:gerade|eben|soeben|grade|grad|grade eben|"
    r"vorhin|jetzt)\b[^.!?\n]{0,80}?\b(?:aktiviert|eingeschaltet|angeschaltet|angemacht|"
    r"eingestellt|umgestellt|geändert|gesetzt|angehakt|freigeschaltet|konfiguriert)\b"
    r"|\b(?:gerade|eben|soeben)\s+(?:erst\s+)?(?:aktiviert|eingeschaltet|angeschaltet|geändert|"
    r"umgestellt)\b"
    r"|\bvor\s+(?:einer|1|zwei|2|drei|3|ein paar|wenigen)\s+minuten?\b[^.!?\n]{0,80}?"
    r"\b(?:aktiviert|eingeschaltet|angeschaltet|eingestellt|umgestellt|geändert)\b",
    re.IGNORECASE,
)
# a statement that a setting or a log is off (what the absence of events cannot show)
_OFF = re.compile(
    r"\b(?:is|are|was|were|seems? to be|appears? to be)\s+(?:still\s+)?(?:not|n't)\s+"
    r"(?:yet\s+)?(?:enabled|activated|active|turned on|switched on|on\b(?!\s+\w)|configured|"
    r"set up)\b"
    r"|\b(?:isn't|aren't|wasn't|weren't)\s+(?:yet\s+)?(?:enabled|activated|active|turned on|"
    r"switched on|on\b(?!\s+\w)|configured)\b"
    r"|\b(?:is|are|was|were|seems? to be)\s+(?:still\s+)?(?:disabled|deactivated|turned off|"
    r"switched off|off|inactive)\b"
    r"|\b(?:has|have)\s+not\s+been\s+(?:enabled|activated|turned on|switched on)\b"
    r"|\bnot\s+(?:been\s+)?(?:enabled|activated|switched on|turned on)\b"
    r"|\b(?:ist|sind|war|waren|scheint|scheinen)\s+(?:\w+\s+){0,5}?(?:noch\s+)?"
    r"(?:nicht|nie)\s+(?:\w+\s+){0,2}?(?:aktiviert|aktiv|eingeschaltet|angeschaltet|"
    r"eingerichtet|an\b(?!\s+\w))\b"
    r"|\b(?:ist|sind|war|waren|scheint|scheinen)\s+(?:\w+\s+){0,5}?(?:noch\s+)?"
    r"(?:deaktiviert|ausgeschaltet|abgeschaltet|aus|inaktiv)\b"
    r"|\bnicht\s+(?:aktiviert|eingeschaltet|angeschaltet)\b",
    re.IGNORECASE,
)
# ... about what is logged or set (not any "off": "the AP is not on the 5 GHz band", "the guest
# network is disabled, so it cannot be joined"): a word for the log or the setting, or a pronoun
# that is the subject of the sentence ("It seems to be disabled.")
_ABOUT_SETTING = re.compile(
    r"\b(?:log\w*|event\w*|record\w*|setting\w*|option|"
    r"protokoll\w*|aufzeichn\w*|ereignis\w*|einstellung\w*|erfass\w*)\b"
    r"|^[\s>*•-]*(?:it|es|sie)\s",
    re.IGNORECASE,
)
# the sentence already says it cannot tell yet
_ALREADY_OPEN = re.compile(
    r"not (?:yet )?confirmed|cannot (?:yet )?(?:tell|say|confirm)|can't (?:yet )?(?:tell|say|"
    r"confirm)|too (?:early|soon)|no new events? yet|not shown yet|"
    r"noch nicht bestätigt|nicht bestätigt|noch nicht sagen|kann (?:ich )?(?:noch )?nicht "
    r"(?:sagen|bestätigen)|zu früh|noch keine neuen",
    re.IGNORECASE,
)
_SENTENCE_END = re.compile(r"(?<=[.!?…])\s+(?=\S)")

NOT_CONFIRMED = {
    "en": (
        "Not confirmed yet: you changed the setting only a moment ago, and no new event has "
        "come in since, so the log cannot show yet whether it is on. The next client joining or "
        "leaving an access point will show it. I cannot read the controller's setting itself. "
        "Shall I check again in a few minutes?"
    ),
    "de": (
        "Noch nicht bestätigt: Du hast die Einstellung eben erst geändert, und seitdem ist kein "
        "neues Ereignis gekommen, darum kann das Protokoll noch nicht zeigen, ob sie an ist. Das "
        "nächste Gerät, das sich an einem Access Point an- oder abmeldet, zeigt es. Die "
        "Einstellung selbst kann ich im Controller nicht lesen. Soll ich in ein paar Minuten noch "
        "einmal nachsehen?"
    ),
}
CLAIM = {
    "en": "not confirmed yet (changed a moment ago, no new event since)",
    "de": "noch nicht bestätigt (eben erst geändert, seitdem kein neues Ereignis)",
}


def just_changed(text: str) -> bool:
    """The person says they changed (switched on, set) something a moment ago."""
    return bool(_JUST_CHANGED.search(text))


# a message of the person in the conversation note of a check's task, up to the next line of
# the note (``chat_tools.conversation_for_task``)
_USER_LINE = re.compile(r"^user: (.*?)(?=\n(?:assistant: |user: |\()|\)?\Z)", re.M | re.S)


def just_changed_in_task(prompt: str) -> bool:
    """A check's task (``chat_tools.task_prompt``): the person says so in the message or in their
    message before it. The answers in the conversation note do not count: the hub's own German
    "eben erst geändert" would otherwise make every later check in the chat read as one."""
    question, _, rest = prompt.partition("\n\n(")
    if just_changed(question):
        return True
    said = _USER_LINE.findall(rest)
    return bool(said) and just_changed(said[-1])


def says_off(sentence: str) -> bool:
    """The sentence says that a setting or a log is off, and does not already say it cannot tell."""
    return (
        bool(_OFF.search(sentence))
        and bool(_ABOUT_SETTING.search(sentence))
        and not _ALREADY_OPEN.search(sentence)
    )


def hint() -> str:
    """What the check is told when the person has just changed a setting."""
    return (
        "Hint: the person says they changed a setting only a moment ago. Events that a setting "
        "starts to log come only when something happens, so a log read now that holds no such "
        "event does NOT show that the setting is off: report it as not confirmed yet, never as "
        '"not enabled" or "off". Say what will show it (for client event logging: the next '
        "client joining or leaving an access point) and offer to check again later. State the "
        "setting itself only if a tool result in this run shows the setting; no tool here reads "
        "a controller's settings, so say that you cannot read it."
    )


def qualify_claim(claim: str, language: str) -> str:
    """A finding of the check as it may stand: one that says the setting is off becomes "not
    confirmed yet"; anything else stays."""
    if not says_off(claim):
        return claim
    return CLAIM["de" if language == "de" else "en"]


def guard(text: str, language: str) -> tuple[str, list[str]]:
    """The answer with every sentence that says the setting is off taken out, and the hub's own
    "not confirmed yet" paragraph in place of the first one. Returns the text and the sentences
    that were taken out (none: the text is returned unchanged)."""
    removed: list[str] = []
    out_lines: list[str] = []
    placed = False
    for line in text.split("\n"):
        kept: list[str] = []
        for sentence in _SENTENCE_END.split(line):
            if sentence.strip() and says_off(sentence):
                removed.append(sentence.strip())
                if not placed:
                    kept.append(NOT_CONFIRMED["de" if language == "de" else "en"])
                    placed = True
                continue
            kept.append(sentence)
        joined = " ".join(kept)
        if line.strip() and not joined.strip():
            continue  # a line that held only such sentences goes
        out_lines.append(joined)
    if not removed:
        return text, []
    return "\n".join(out_lines), removed
