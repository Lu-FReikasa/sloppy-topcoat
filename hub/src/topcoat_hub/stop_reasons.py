"""Why an agent run ended: one table for the card, the report and the failure message.

A run ends with a code in ``AgentTask.stop_reason`` (``agent_loop``, ``agent_service``). Three
places used to explain it, each with its own keys: the sentence under a report card
(``report._STOPS``), the sentence in the report document (``report_doc`` ``why_*``) and the category
of a failure message (``message_result.FAILURE_REASONS``). They drifted apart: a code added in one
was missing in the others, and the categories listed codes (``turns``, ``steps``) the loop never
emits, so a run that hit ``max_turns`` was filed as "other". A new code is added here, once.

``category`` is what a failure message to another application says (``message_result``):
``node_away`` (try again when the machine is back), ``no_budget`` (a larger budget would do),
``no_sources`` (nothing usable was found), ``other``."""

from dataclasses import dataclass


@dataclass(frozen=True)
class Stop:
    category: str
    short: dict[str, str]  # a clause under a card: "the check did not finish (<short>)"
    long: dict[str, str]  # a sentence in the report document


STOPS: dict[str, Stop] = {
    "repeated_calls": Stop(
        "other",
        {"de": "die Abfragen wiederholten sich", "en": "the calls kept repeating"},
        {
            "de": "dieselbe Abfrage wurde mehrfach hintereinander gestellt, obwohl der Hub sie "
            "abgelehnt hatte",
            "en": "the same request was made several times in a row although the hub had "
            "refused it",
        },
    ),
    "max_turns": Stop(
        "no_budget",
        {"de": "die Schritte reichten nicht aus", "en": "it ran out of steps"},
        {
            "de": "die erlaubte Zahl an Modellrunden war erreicht",
            "en": "the allowed number of model turns was reached",
        },
    ),
    "max_tool_calls": Stop(
        "no_budget",
        {
            "de": "die Zahl der erlaubten Abfragen war erreicht",
            "en": "it used up the calls it may make",
        },
        {
            "de": "die erlaubte Zahl an Werkzeugaufrufen war erreicht",
            "en": "the allowed number of tool calls was reached",
        },
    ),
    "time_budget": Stop(
        "no_budget",
        {"de": "die Zeit reichte nicht aus", "en": "it ran out of time"},
        {"de": "die eingestellte Zeit war aufgebraucht", "en": "the set time was used up"},
    ),
    "no_progress": Stop(
        "no_sources",
        {
            "de": "die Recherche brachte nichts Neues mehr",
            "en": "the research found nothing new any more",
        },
        {
            "de": "mehrere Schritte in Folge brachten keine neue Quelle und keinen neuen Beleg",
            "en": "several steps in a row brought no new source and no new evidence",
        },
    ),
    "bad_format": Stop(
        "other",
        {
            "de": "das Modell antwortete mehrfach ungültig",
            "en": "the model kept answering invalidly",
        },
        {
            "de": "das Modell lieferte mehrfach keine gültige Antwort",
            "en": "the model gave no valid answer several times",
        },
    ),
    "model_unavailable": Stop(
        "node_away",
        {"de": "das Modell war nicht erreichbar", "en": "the model could not be reached"},
        {
            "de": "das Modell war nicht erreichbar oder antwortete nicht",
            "en": "the model could not be reached or did not answer",
        },
    ),
    "no_tools": Stop(
        "node_away",
        {"de": "es war kein Werkzeug verfügbar", "en": "no tool was available"},
        {
            "de": "es war kein Werkzeug-Knoten verfügbar",
            "en": "no tool node was available",
        },
    ),
    "insufficient_memory": Stop(
        "node_away",
        {"de": "dem Rechner fehlte der Arbeitsspeicher", "en": "the machine had too little RAM"},
        {
            "de": "das Modell konnte nicht geladen werden: nicht genug freier Arbeitsspeicher",
            "en": "the model could not be loaded: not enough free RAM",
        },
    ),
    "insufficient_disk": Stop(
        "node_away",
        {"de": "dem Rechner fehlte Speicherplatz", "en": "the machine had too little disk space"},
        {
            "de": "das Modell konnte nicht geladen werden: nicht genug freier Speicherplatz",
            "en": "the model could not be loaded: not enough free disk space",
        },
    ),
    "tools_missing": Stop(
        "node_away",
        {"de": "ein nötiges Werkzeug fehlte", "en": "a tool it needs was missing"},
        {
            "de": "ein für die Frage nötiges Werkzeug war nicht verfügbar",
            "en": "a tool the question needs was not available",
        },
    ),
}

# older names of the same codes (stored tasks, the older failure table, the old report key)
ALIASES = {
    "model_failed": "model_unavailable",
    "budget": "max_tool_calls",
    "steps": "max_turns",
    "turns": "max_turns",
}


def _find(code: str | None) -> Stop | None:
    code = code or ""
    return STOPS.get(ALIASES.get(code, code))


def short(code: str | None, language: str) -> str:
    """The clause for a card; the code itself for a reason this table does not know."""
    stop = _find(code)
    return stop.short.get(language, stop.short["en"]) if stop else str(code or "")


def long(code: str | None, language: str) -> str | None:
    """The sentence for the report document; ``None`` for an unknown reason (the caller says
    "reason: <code>")."""
    stop = _find(code)
    return stop.long.get(language, stop.long["en"]) if stop else None


def category(code: str | None, has_error: bool = False) -> str:
    """What a failure message says: the category of the reason, ``tool_errors`` for a run that
    ended with an error text but no known reason, ``other`` otherwise."""
    stop = _find(code)
    if stop is not None:
        return stop.category
    return "tool_errors" if has_error else "other"
