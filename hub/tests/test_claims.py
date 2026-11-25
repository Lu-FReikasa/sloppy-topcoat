"""AGT-01: an answer claims only what the hub saw happen."""

import pytest
from fastapi.testclient import TestClient

from tests.conftest import AUTH
from tests.test_agent import use
from tests.test_chat import new_conv
from tests.test_chat_tools import ask_with, finish, script_check, world  # noqa: F401
from topcoat_hub import claims

# --- what counts as a claim ----------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "done", "ready"),
    [
        # the answer of 2026-10-30: three tasks "set to dropped", the change "ready to confirm"
        (
            "Die Aufgaben 2.2, 3.2 und 4.2 wurden auf `dropped` gesetzt. "
            "Der Änderungssatz ist bereit zur Bestätigung.",
            True,
            True,
        ),
        ("Ich habe die drei Duplikate gelöscht.", True, False),
        ("The tasks were set to dropped. The change set is ready for confirmation.", True, True),
        ("I've moved the tasks to the new project.", True, False),
        ("Der Änderungssatz liegt zur Bestätigung bereit.", False, True),
        # live 2026-10-31: nothing was called, the answer says the changes wait for the click
        ("Die Änderungen warten auf deine Bestätigung.", False, True),
        ("The change waits for your confirmation.", False, True),
        # an offer, a question, a statement about history and a plain report are not claims
        ("Ich kann die Aufgaben auf dropped setzen, wenn du willst.", False, False),
        ("Soll ich die Aufgaben auf dropped setzen?", False, False),
        ("Die Aufgabe wurde am 3. Mai angelegt.", False, False),
        ("Das Projekt hat 12 offene Aufgaben, zwei davon sind überfällig.", False, False),
        ("If you like, I can set them to dropped.", False, False),
        ("Es liegt nichts vor, das auf deine Bestätigung wartet.", False, False),
        ("Nothing waits for your confirmation.", False, False),
        ("Wenn du willst, warten wir auf deine Bestätigung, bevor ich etwas ändere.", False, False),
    ],
)
def test_a_claim_of_a_change_is_found_by_its_words(text: str, done: bool, ready: bool) -> None:
    assert claims.claimed_change(text) == (done, ready)


# --- what the hub writes under it ----------------------------------------------------------------

REFUSED = [
    {
        "capability": "project.changeset.propose",
        "node": "",
        "code": "invalid_input",
        "message": "operation update_node has no key reason; allowed: node, title, status",
    }
]


def test_a_refused_proposal_is_named_under_the_answer_that_claims_it_worked() -> None:
    text = "Die Aufgaben wurden auf dropped gesetzt. Der Änderungssatz ist bereit zur Bestätigung."
    note = claims.note("de", text, 0, REFUSED)
    assert note.startswith("\n\nHinweis des Hubs: Es wurde nichts geändert")
    assert "keine Änderung zur Bestätigung" in note
    assert "`project.changeset.propose` (invalid_input: operation update_node has no key" in note
    english = claims.note("en", "I've moved the tasks.", 0, REFUSED)
    assert "Nothing was changed, and no change waits for your confirmation." in english
    assert "Refused: `project.changeset.propose`" in english


LONG_CLAIM = (
    "Die Operation `project.changeset.propose` schlug fehl, da `node_id` als `node` heißt; "
    "ich habe jedoch den Änderungsvorschlag vorbereitet, um die Aufgabe „Test: Aufgabe A“ auf "
    "„dropped“ zu setzen, der nun in der Benutzeroberfläche auf deine Bestätigung wartet."
)


def test_a_claim_in_a_long_sentence_is_held_against_the_record() -> None:
    # live hub, 2026-10-31: one sentence of about 270 characters, skipped by the strict patterns
    assert claims.claimed_change(LONG_CLAIM) == (False, False)
    note = claims.note("de", LONG_CLAIM, 0, REFUSED)
    assert note.startswith("\n\nHinweis des Hubs: Es wurde nichts geändert")
    assert "`project.changeset.propose` (invalid_input" in note
    assert "Nothing was changed" in claims.note(
        "en", "I have prepared the change, which now awaits your confirmation.", 0, REFUSED
    )


@pytest.mark.parametrize(
    "text",
    [
        # honest: says what failed and asks for something else
        "Der Änderungsvorschlag konnte nicht erstellt werden, da `node` fehlt. Bitte wähle einen "
        "gültigen Status.",
        "Der Aufruf schlug fehl; es liegt keine Änderung zur Bestätigung vor.",
        "The proposal could not be created, so nothing waits for your confirmation.",
        # an offer and a question
        "Soll ich den Vorschlag noch einmal vorbereiten, damit du ihn bestätigen kannst?",
        # nothing about a waiting change
        "Das Projekt hat zwölf Aufgaben, drei davon sind überfällig.",
    ],
)
def test_an_honest_answer_after_a_refused_proposal_gets_no_note(text: str) -> None:
    assert claims.note("de", text, 0, REFUSED) == ""


def test_the_loose_reading_needs_the_record() -> None:
    # nothing was refused: the strict patterns decide alone (a missed claim costs nothing)
    assert claims.note("de", LONG_CLAIM, 0, []) == ""
    # a change is on record: "waits for your confirmation" is true
    assert claims.note("de", LONG_CLAIM, 1, REFUSED) == ""
    # a refused call that prepares nothing (a read) does not count
    read = [{"capability": "project.list", "code": "timeout", "message": "x"}]
    assert claims.note("de", LONG_CLAIM, 0, read) == ""


def test_a_prepared_change_is_not_a_change_that_was_made() -> None:
    done = "Ich habe die Aufgaben auf dropped gesetzt."
    assert "Bisher ist nichts geändert. 2 Änderungen liegen zur Bestätigung bereit" in (
        claims.note("de", done, 2, [])
    )
    assert "Nothing is changed yet. 1 change waits for your confirmation" in (
        claims.note("en", "I've set it to dropped.", 1, [])
    )
    # "ready to confirm" with a change on record is true: no note
    assert claims.note("de", "Der Änderungssatz ist bereit zur Bestätigung.", 1, []) == ""
    assert claims.note("de", "Das Projekt hat zwölf Aufgaben.", 0, REFUSED) == ""


def test_a_passive_report_of_a_waiting_set_is_held_against_the_record() -> None:
    """Live 2026-11-18: "drei neue Aufgaben wurden erstellt" and "wurde unter 3.1.1 angelegt"
    while the set only waited for the click; no "jetzt" in them, so the strict patterns missed."""
    said = (
        "Die Änderungen wurden vorbereitet: drei neue Aufgaben (3.1.3, 3.1.4, 3.1.5) wurden "
        "erstellt und die ursprüngliche Aufgabe 3.1.1 auf „in_review“ gesetzt."
    )
    assert "Bisher ist nichts geändert" in claims.note("de", said, 1, [])
    assert "Bisher ist nichts geändert" in claims.note(
        "de", "Task 3.1.1.1 „eigeneAufgabe“ wurde unter 3.1.1 angelegt.", 1, []
    )
    # history and an honest "prepared" stay without a note
    assert claims.note("de", "Die Aufgabe 3.1.1 wurde am 3. Mai angelegt.", 1, []) == ""
    assert claims.note("de", "Der Änderungssatz wurde vorbereitet und wartet.", 1, []) == ""
    # without a set on record the strict reading decides alone, as before
    assert claims.note("de", "Die Aufgabe wurde unter 3.1 angelegt.", 0, []) == ""


# --- through the chat ----------------------------------------------------------------------------

CLAIM = "Ich habe den Container web neu gestartet. Der Änderungssatz ist bereit zur Bestätigung."


def test_the_closing_answer_of_a_check_cannot_claim_a_change_nobody_prepared(
    client: TestClient,
    world,  # noqa: F811
) -> None:
    _, _, model = world
    use(client, *script_check_actions(), finish("web ran out of memory.", [], None))
    model.stream_text_fn = lambda req: CLAIM
    conv = new_conv(client)
    status, _events, _ = ask_with(client, conv, "Was ist mit web los?", "on")
    assert status == 200
    stored = client.get(f"/api/conversations/{conv}", headers=AUTH).json()["messages"]
    closing = stored[-1]["content"]
    assert closing.startswith(CLAIM)
    assert "Hinweis des Hubs: Es wurde nichts geändert" in closing


def test_a_proposal_on_the_card_is_advice_not_a_change_waiting_for_a_click(
    client: TestClient,
    world,  # noqa: F811
) -> None:
    # found on the live hub (2026-10-31): the card carried a proposal text, so an answer that said
    # "the change is ready to confirm" passed although the hub had refused the call and nothing
    # waited for a click
    _, _, model = world
    script_check(client)  # the check ends with a proposal: "raise the memory limit"
    model.stream_text_fn = lambda req: CLAIM
    conv = new_conv(client)
    ask_with(client, conv, "Was ist mit web los?", "on")
    closing = client.get(f"/api/conversations/{conv}", headers=AUTH).json()["messages"][-1]
    assert "Hinweis des Hubs: Es wurde nichts geändert" in closing["content"]
    assert "keine Änderung zur Bestätigung" in closing["content"]


def test_an_answer_without_a_claim_gets_no_note(
    client: TestClient,
    world,  # noqa: F811
) -> None:
    _, _, model = world
    script_check(client)
    model.stream_text_fn = lambda req: "web hat zu wenig Speicher und wurde vom Kernel beendet."
    conv = new_conv(client)
    ask_with(client, conv, "Was ist mit web los?", "on")
    closing = client.get(f"/api/conversations/{conv}", headers=AUTH).json()["messages"][-1]
    assert "Hinweis des Hubs" not in closing["content"]


def script_check_actions() -> list[str]:
    import json

    return [
        json.dumps(
            {
                "action": "tool",
                "node": "vm-docker",
                "capability": "docker.logs",
                "input": {"container": "web", "lines": 50},
                "reason": "see why web restarts",
            }
        )
    ]


def test_a_check_the_hub_refused_is_not_reported_as_a_failed_check(
    client: TestClient,
    world,  # noqa: F811
) -> None:
    _, _, model = world
    script_check(client)
    model.stream_text_fn = lambda req: "Ich kann gerade nicht in deine Server sehen."
    client.app.state.settings.agent_max_active_per_user = 0  # every start is refused
    conv = new_conv(client)
    status, events, _ = ask_with(client, conv, "Was ist mit web los?", "on")
    assert status == 200
    done = next(d for e, d in events if e == "tools_done")
    assert done["status"] != "done" and done.get("calls", 0) == 0
    closing = client.get(f"/api/conversations/{conv}", headers=AUTH).json()["messages"][-1]
    assert "Hinweis des Hubs: Es wurde keine Server-Prüfung gemacht" in closing["content"]
    assert "sagt das nichts" in closing["content"]


def test_only_the_limit_of_the_hub_is_told_that_way() -> None:
    assert claims.refused_note("de", "no_capabilities") == ""
    assert "No server check was made" in claims.refused_note("en", "too_many_tasks")


def test_an_answer_that_speaks_of_texts_while_the_set_only_renames_is_told_so() -> None:
    text = "Ich habe einen Änderungsvorschlag mit detaillierten Anleitungen vorbereitet."
    note = claims.note("de", text, 1, [], (False, "1.5: title; 1.6: title"))
    assert "schreibt keine Texte" in note and "1.5: title; 1.6: title" in note
    assert claims.note("de", text, 1, [], (True, "1.5: body")) == ""
    assert claims.note("de", "Die Titel sind neu.", 1, [], (False, "1.5: title")) == ""
