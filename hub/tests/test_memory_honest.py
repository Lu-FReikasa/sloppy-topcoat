"""Honesty by mechanism, part (a) (E3-07 D4): when a user says "note this", the hub notes it or says
why not ("Gemerkt: …" / "Nicht gemerkt, weil …") — whatever the memory model did, and whatever the
chat model claims. The exchange that started it: "lil switch ist bewusst aus, notiere das"."""

import json
from typing import Any

import pytest
from fastapi.testclient import TestClient

from tests.conftest import AUTH
from tests.fakenode import online_node
from tests.test_chat import new_conv, use_model
from tests.test_memory import chat_once, note
from topcoat_hub import chat
from topcoat_hub.memory import EXTRACTION_SYSTEM, explicit_request, kind_of_statement

LIL_SWITCH = "lil switch ist bewusst aus, notiere das"


@pytest.fixture
def node(client: TestClient, connect: Any) -> Any:
    n = online_node(client, connect, "node-a", loaded=["small"])
    use_model(client)
    return n


def memory_model(reply: Any) -> Any:
    """A fake memory model answering the extraction with ``reply`` (a dict, a text, or None to
    play a model that says nothing usable)."""

    def answer(req: dict[str, Any]) -> str | None:
        if req.get("stream") or req["messages"][0]["content"] != EXTRACTION_SYSTEM:
            return None
        if reply is None:
            return "I have no idea what you mean."  # not JSON at all
        return reply if isinstance(reply, str) else json.dumps(reply)

    return answer


def memory_event(events: list[Any]) -> dict[str, Any] | None:
    return next((d for e, d in events if e == "memory"), None)


def notes(client: TestClient) -> list[dict[str, Any]]:
    return client.get("/api/memory", headers=AUTH).json()["items"]


# --- recognising the request -------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "statement"),
    [
        ("lil switch ist bewusst aus, notiere das", "lil switch ist bewusst aus"),
        ("Notiere dir bitte: der NAS läuft nur nachts", "der NAS läuft nur nachts"),
        ("Merk dir, dass der Drucker im Flur steht", "dass der Drucker im Flur steht"),
        ("Der Router ist alt. Schreib das bitte auf.", "Der Router ist alt"),
        ("Halte fest: Backups laufen um drei", "Backups laufen um drei"),
        ("Please note that the NAS sleeps at night", "Please that the NAS sleeps at night"),
        ("Remember this: the switch stays off", "the switch stays off"),
        ("notiere das", ""),
        ("Wie spät ist es?", None),
        ("Ich notiere mir selbst nichts, danke", None),
        (
            "kannst du mir erklären, wie die Logik funktioniert, dass du entscheidest, "
            "Informationen in deinem Gedächtnis zu speichern?",
            None,
        ),
        ("Wie speicherst du eigentlich Notizen?", None),
        ("Du sollst nichts mehr zu speichern versuchen", None),
        ("Speichere das: der NAS schläft nachts", "der NAS schläft nachts"),
    ],
)
def test_the_request_is_taken_out_of_the_users_sentence(text: str, statement: str | None) -> None:
    got = explicit_request(text)
    if statement is None:
        assert got is None
    else:
        assert got is not None
        # the statement is the user's own words without the request; "note that" leaves a stray
        # "that" in English, which the model sees as part of the sentence — only the kernel counts
        assert statement.split()[-3:] == got.split()[-3:]


def test_the_kind_of_a_note_the_hub_makes_follows_its_words() -> None:
    assert kind_of_statement("Lil switch ist bewusst aus") == "operational"
    assert kind_of_statement("Der Plex Server läuft auf dem NAS") == "operational"
    assert kind_of_statement("Ich mag Kaffee lieber ohne Milch") == "preference"
    assert kind_of_statement("Meine Tochter heißt Lea") == "person"


def test_the_model_is_not_allowed_to_promise_that_it_noted() -> None:
    assert chat.claims_noted("Alles klar, ich habe mir das notiert.")
    assert chat.claims_noted("Ich habe das gemerkt und gespeichert.")
    assert chat.claims_noted("Ist notiert!")
    assert chat.claims_noted("I've noted that for later.")
    assert not chat.claims_noted("Das Notizbuch liegt auf dem Tisch.")
    assert not chat.claims_noted("Der Switch ist aus.")
    prompt = chat.system_prompt(None, None, chat.Capabilities(memory=True))
    assert "You cannot save notes yourself" in prompt


# --- the exchange that started it ----------------------------------------------------------------


def test_lil_switch_is_noted_as_an_operational_note_even_if_the_model_finds_nothing(
    client: TestClient, node: Any
) -> None:
    node.answer_fn = memory_model({"memories": [], "used": [], "instructions": []})
    events = chat_once(client, LIL_SWITCH)
    event = memory_event(events)
    assert event is not None and "not_noted" not in event
    assert [n["text"] for n in event["remembered"]] == ["Lil switch ist bewusst aus"]
    [item] = [n for n in notes(client) if n["text"] == "Lil switch ist bewusst aus"]
    assert item["kind"] == "operational"
    assert item["expires_at"] is None  # asked for, so it stays: not a passing state
    assert item["evidence"] == "doc"  # the user said it outright


def test_lil_switch_is_noted_when_the_model_marks_it_volatile_or_is_unreachable(
    client: TestClient, node: Any
) -> None:
    # the model hears "ist aus" as a passing state and words it differently
    node.answer_fn = memory_model(
        {"memories": [note("Der lil switch ist gerade ausgeschaltet", volatile=True)]}
    )
    chat_once(client, LIL_SWITCH)
    # the model's paraphrase lost "bewusst": the user's own sentence is noted as well, and it stays
    own = [n for n in notes(client) if n["text"] == "Lil switch ist bewusst aus"]
    assert len(own) == 1 and own[0]["expires_at"] is None and own[0]["kind"] == "operational"

    # and with the memory model gone (not a JSON answer at all) the hub still notes it
    node.answer_fn = memory_model(None)
    conv = new_conv(client)
    events = chat_once(client, "Der Drucker ist kaputt und bleibt aus, merk dir das", conv)
    event = memory_event(events)
    assert event is not None
    assert [n["text"] for n in event["remembered"]] == ["Der Drucker ist kaputt und bleibt aus"]


def test_what_the_model_found_is_used_and_not_doubled(client: TestClient, node: Any) -> None:
    node.answer_fn = memory_model(
        {"memories": [note("Lil switch ist bewusst aus", kind="operational")]}
    )
    events = chat_once(client, LIL_SWITCH)
    event = memory_event(events)
    assert event is not None
    assert len(event["remembered"]) == 1
    assert len([n for n in notes(client) if "lil switch" in n["text"].lower()]) == 1


def test_saying_it_again_confirms_the_note_instead_of_making_a_second(
    client: TestClient, node: Any
) -> None:
    node.answer_fn = memory_model(None)
    chat_once(client, LIL_SWITCH)
    events = chat_once(client, LIL_SWITCH)
    event = memory_event(events)
    assert event is not None and event["remembered"] == [] and len(event["confirmed"]) == 1
    assert "not_noted" not in event
    assert len([n for n in notes(client) if "lil switch" in n["text"].lower()]) == 1


# --- and when it cannot be noted, the reason ---------------------------------------------------


def test_a_secret_is_not_noted_and_the_reason_says_so(client: TestClient, node: Any) -> None:
    node.answer_fn = memory_model(None)
    events = chat_once(client, "Das WLAN-Passwort ist hunter2hunter2, notiere das")
    event = memory_event(events)
    assert event is not None and event["not_noted"] == "secret" and event["blocked"] >= 1
    assert event["remembered"] == []
    assert not any("hunter2" in n["text"] for n in notes(client))


def test_nothing_to_note_in_the_message_is_said(client: TestClient, node: Any) -> None:
    node.answer_fn = memory_model(None)
    events = chat_once(client, "Notiere das bitte unbedingt")
    event = memory_event(events)
    assert event is not None and event["not_noted"] == "no_statement"


def test_too_long_to_be_a_note_is_said(client: TestClient, node: Any) -> None:
    node.answer_fn = memory_model(None)
    events = chat_once(client, "Der Server " + "läuft sehr zuverlässig und " * 20 + ", notiere das")
    event = memory_event(events)
    assert event is not None and event["not_noted"] == "too_long"


def test_with_memory_off_the_user_is_told_that_nothing_was_noted(
    client: TestClient, node: Any
) -> None:
    client.put("/api/chat/settings", headers=AUTH, json={"memory_enabled": False})
    events = chat_once(client, LIL_SWITCH)
    event = memory_event(events)
    assert event is not None and event["not_noted"] == "memory_off"
    assert event["remembered"] == []
    assert not any("lil switch" in n["text"].lower() for n in notes(client))


def test_a_claim_of_the_model_without_a_note_is_contradicted_by_the_hub(
    client: TestClient, node: Any
) -> None:
    """The chat model says "ich habe es notiert" although the user never asked and nothing was
    noted: the hub's own line says what is true."""
    node.answer_fn = memory_model({"memories": []})
    node.stream_text_fn = lambda body: "Alles klar, ich habe mir das notiert."
    events = chat_once(client, "Das Wetter ist heute wirklich sehr schön draußen")
    event = memory_event(events)
    assert event is not None and event["not_noted"] == "nothing_found"


def test_an_ordinary_exchange_gets_no_note_line_at_all(client: TestClient, node: Any) -> None:
    node.answer_fn = memory_model({"memories": []})
    events = chat_once(client, "Erkläre mir bitte kurz, wie ein Router arbeitet")
    assert memory_event(events) is None
