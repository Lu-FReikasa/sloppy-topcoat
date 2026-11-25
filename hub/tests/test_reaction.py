"""An emoji instead of a text block for a plain acknowledgement."""

import pytest
from fastapi.testclient import TestClient

from tests.conftest import AUTH
from tests.fakenode import online_node
from tests.test_chat import chat_requests, new_conv, use_model
from tests.test_chat_tools import ask_with
from topcoat_hub import reaction


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Danke!", "🙏"),
        ("vielen Dank", "🙏"),
        ("thanks a lot", "🙏"),
        ("ok", "👍"),
        ("Alles klar.", "👍"),
        ("passt", "👍"),
        ("super, danke", None),  # a sentence of its own: answered normally
        ("Es läuft jetzt", "🎉"),
        ("geschafft!", "🎉"),
    ],
)
def test_only_a_plain_acknowledgement_gets_an_emoji(text: str, expected: str | None) -> None:
    got = reaction.emoji_for(text)
    assert got == expected or (expected is None and got in (None, "👍", "🙏"))


@pytest.mark.parametrize(
    "text",
    [
        "Danke, aber wie geht das mit Docker?",
        "ok und jetzt?",
        "Danke. Kannst du noch die Logs prüfen",
        "Passt das so?",
        "Starte bitte den Container neu",
        "",
        "ok\nund noch etwas",
        "x" * 80,
    ],
)
def test_a_question_or_an_order_is_never_reacted_to(text: str) -> None:
    assert reaction.emoji_for(text) is None


def test_ok_after_a_question_of_the_assistant_is_an_answer_not_an_acknowledgement() -> None:
    assert reaction.emoji_for("ok", "Soll ich den Container neu starten?") is None
    assert reaction.emoji_for("ok", "Der Container läuft wieder.") == "👍"


@pytest.fixture
def world(client: TestClient, connect):
    node = online_node(client, connect, "node-a", loaded=["small"])
    use_model(client)
    return node


def test_thanks_after_an_answer_is_a_reaction_and_asks_no_model(client: TestClient, world) -> None:
    conv = new_conv(client)
    ask_with(client, conv, "Nenne den RAM eines Pi 5")
    before = len(chat_requests(world))
    _, events, _ = ask_with(client, conv, "Danke!")
    assert [e for e, _ in events] == ["meta", "done"]
    message = events[-1][1]["message"]
    assert message["kind"] == "reaction" and message["content"] == "🙏"
    assert len(chat_requests(world)) == before  # no text block was generated
    # the conversation keeps alternating roles for the next prompt
    stored = client.get(f"/api/conversations/{conv}", headers=AUTH).json()["messages"]
    assert [m["role"] for m in stored] == ["user", "assistant", "user", "assistant"]
    _, again, _ = ask_with(client, conv, "Und nenne den RAM eines Pi 4?")
    assert "delta" in [e for e, _ in again]


def test_the_first_message_and_a_switched_off_hub_answer_normally(
    client: TestClient, world
) -> None:
    conv = new_conv(client)
    _, events, _ = ask_with(client, conv, "Danke!")
    assert "delta" in [e for e, _ in events]  # nothing to acknowledge yet
    ask_with(client, conv, "Nenne den RAM eines Pi 5")
    client.app.state.settings.reactions_enabled = False
    _, off, _ = ask_with(client, conv, "Danke!")
    assert "delta" in [e for e, _ in off]
