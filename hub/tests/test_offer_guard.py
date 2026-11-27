"""An offer to change something that no action backs is cut from the answer (issue #223; live
2026-11-27: "Shall I enable the setting for you?" with only read access to the Omada controller,
and after "yes": "I cannot")."""

import time
from typing import Any

from fastapi.testclient import TestClient

from tests.fakenode import online_node
from tests.test_chat import new_conv, use_model
from tests.test_chat_tools import ask_with
from tests.test_tools import tools_node
from topcoat_hub import chat, offer_guard

READ_ONLY: list[dict[str, str]] = []  # only read capabilities: nothing to propose
RESTART = {
    "node": "node-amd",
    "capability": "docker.restart",
    "description": "Restart one container (docker restart): it is stopped and started again.",
}
TOGGLE = {
    "node": "node-amd",
    "capability": "omada.set_feature",
    "description": "Enable or disable one feature of a site on the Omada controller.",
}

ANSWER = "I can only read the Omada controller: the setting is off."
OFFER = "Shall I enable the setting for you?"


def test_an_offer_without_an_action_is_cut() -> None:
    text = f"{ANSWER} {OFFER}"
    assert offer_guard.without_unbacked_offer(text, READ_ONLY, "en") == ANSWER
    de = "Ich kann den Controller nur lesen. Soll ich die Einstellung für dich aktivieren?"
    assert offer_guard.without_unbacked_offer(de, READ_ONLY, "de") == (
        "Ich kann den Controller nur lesen."
    )
    for polite in ("Would you like me to disable it?", "Darf ich den Port sperren?"):
        assert offer_guard.without_unbacked_offer(polite, READ_ONLY, "en") == (
            offer_guard.cut_note("en")
        )
    asked = "Die Funktion ist aus. Möchtest du, dass ich sie einschalte?"
    assert offer_guard.without_unbacked_offer(asked, READ_ONLY, "de") == "Die Funktion ist aus."


def test_an_offer_with_a_matching_action_stays() -> None:
    text = f"{ANSWER} {OFFER}"
    assert offer_guard.without_unbacked_offer(text, [TOGGLE], "en") == text
    restart = "The container hangs. Soll ich den Container neu starten?"
    assert offer_guard.without_unbacked_offer(restart, [RESTART], "de") == restart


def test_an_action_of_another_kind_does_not_back_the_offer() -> None:
    # a restart is on the whitelist, enabling a setting is not
    assert offer_guard.without_unbacked_offer(f"{ANSWER} {OFFER}", [RESTART], "en") == ANSWER


def test_offers_to_explain_or_to_look_are_kept() -> None:
    for text in (
        "The setting is off. Shall I explain how to enable it?",
        "Das Logging ist aus. Soll ich dir erklären, wie du das Logging aktivierst?",
        "Soll ich nachsehen, ob es aktiviert ist?",
        "Shall I check whether it is enabled?",
        "Shall I write you a script for that?",
        "The setting is off.",
    ):
        assert offer_guard.without_unbacked_offer(text, READ_ONLY, "en") == text


def test_steps_before_the_offer_stay() -> None:
    # the closing "sentence" runs back to the last full stop: the steps for the person are kept
    steps = "The setting is off. You can enable it here:\n- Open the controller\n- Turn on X"
    assert offer_guard.without_unbacked_offer(f"{steps}\n{OFFER}", READ_ONLY, "en") == steps
    code = "Die Datei:\n```\nport: 8080\n```"
    assert (
        offer_guard.without_unbacked_offer(f"{code}\nSoll ich den Dienst neu starten?", [], "de")
        == code
    )


def test_an_offer_to_look_first_is_kept() -> None:
    for text in (
        "Soll ich prüfen, ob ich es deaktivieren kann?",
        "Shall I look at why it does not restart?",
        "That is all I found. Shall I stop here, or go on with the details?",
    ):
        assert offer_guard.without_unbacked_offer(text, READ_ONLY, "en") == text
    # the change comes first: the offer is a change
    assert offer_guard.without_unbacked_offer(
        "Shall I restart it and check the logs?", READ_ONLY, "en"
    ) == offer_guard.cut_note("en")


def test_a_conversation_with_its_own_way_to_change_keeps_the_offer() -> None:
    text = f"{ANSWER} {OFFER}"
    assert offer_guard.without_unbacked_offer(text, READ_ONLY, "en", keep=True) == text


def test_an_answer_that_is_only_the_offer_says_why() -> None:
    assert offer_guard.without_unbacked_offer(OFFER, READ_ONLY, "en") == offer_guard.cut_note("en")
    assert "keine Aktion" in offer_guard.without_unbacked_offer(
        "Soll ich es aktivieren?", READ_ONLY, "de"
    )


def test_the_prompt_says_what_may_be_offered() -> None:
    none = chat.describe_capabilities(chat.Capabilities(tools=("network devices",)))
    assert "You cannot change anything on these systems" in none
    some = chat.describe_capabilities(
        chat.Capabilities(tools=("containers",), actions=offer_guard.labels([RESTART]))
    )
    assert "Restart one container (node-amd)" in some
    assert "docker.restart" not in some
    assert "You cannot change" not in chat.describe_capabilities(chat.Capabilities())


# --- in the chat: what reaches the person ------------------------------------------------------


def _ask(client: TestClient, connect: Any, actions: list[dict[str, str]], monkeypatch: Any) -> str:
    async def chat_actions(_caller: Any) -> list[dict[str, str]]:
        return list(actions)

    monkeypatch.setattr(client.app.state.agents.loop, "chat_actions", chat_actions)
    tools_node(client, connect, "vm-docker")  # read tools: the person looks at a system
    node = online_node(client, connect, "node-a", loaded=["small"])
    use_model(client)
    node.stream_text_fn = lambda req: f"{ANSWER} {OFFER}"
    status, events, _ = ask_with(client, new_conv(client), "Is the setting on?", "off")
    assert status == 200
    return events[-1][1]["message"]["content"]


def test_with_only_read_access_the_offer_does_not_reach_the_person(
    client: TestClient, connect: Any, monkeypatch: Any
) -> None:
    assert _ask(client, connect, [], monkeypatch) == ANSWER


def test_without_any_tools_the_offer_stays(client: TestClient, connect: Any) -> None:
    # no system to look at: "Shall I enable …" is about the conversation, not a whitelist action
    node = online_node(client, connect, "node-a", loaded=["small"])
    use_model(client)
    node.stream_text_fn = lambda req: f"{ANSWER} {OFFER}"
    status, events, _ = ask_with(client, new_conv(client), "Is the setting on?", "off")
    assert status == 200
    assert events[-1][1]["message"]["content"] == f"{ANSWER} {OFFER}"


def test_with_a_matching_action_the_offer_reaches_the_person(
    client: TestClient, connect: Any, monkeypatch: Any
) -> None:
    toggle = {**TOGGLE, "node": "node-a", "capability": "repo.edit"}
    assert _ask(client, connect, [toggle], monkeypatch) == f"{ANSWER} {OFFER}"


def test_long_runs_of_offer_words_stay_fast() -> None:
    # a model stuck in a loop: every "darf ich" is a start, none may scan to the end (regex-gotchas)
    for text in ("darf ich " * 6000, "Soll ich " * 6000 + "x?y?", "Shall I " + "schalte " * 6000):
        started = time.perf_counter()
        offer_guard.without_unbacked_offer(text, READ_ONLY, "en")
        assert time.perf_counter() - started < 0.5
