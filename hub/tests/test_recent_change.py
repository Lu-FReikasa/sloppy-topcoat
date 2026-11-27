"""#224: right after the person changed a setting, missing events are "not confirmed yet"."""

import json

import pytest
from fastapi.testclient import TestClient

from tests.conftest import AUTH
from tests.fakenode import online_node
from tests.test_agent import finish, use
from tests.test_chat import new_conv, sent_messages, use_model
from tests.test_chat_tools import ask_with
from tests.test_tools import tools_node
from topcoat_hub import agent_prompts, chat_tools, recent_change

# what omada.events answers for a day with client events on the gateway only (the node's note)
EVENTS = {
    "site": "Default",
    "window": {
        "events": 41,
        "by_key": {"EVENT_CLIENT_CONNECTED": 41},
        "client_events": 41,
        "client_events_on": {"ap": 0, "switch": 0, "gateway": 41},
        "note": (
            "none of the 41 client events in this window happened on an access point: Wi-Fi "
            "clients joining, leaving or roaming are not in this log, so their absence says "
            "nothing about Wi-Fi drops (what is logged is a setting of the controller)"
        ),
    },
    "events": [],
}
ASKED = (
    "I just enabled client event logging in the Omada controller a minute ago. "
    "Can you check that it works?"
)
OFF = "Client event logging is not enabled: there are no client events from an access point."


@pytest.mark.parametrize(
    "text",
    [
        ASKED,
        "I've just turned on the logging, verify it please",
        "I enabled it a minute ago, is it working?",
        "Ich habe gerade das Client-Logging im Omada Controller aktiviert, prüf das bitte",
        "Hab eben die Protokollierung eingeschaltet. Geht es?",
        "Vor einer Minute habe ich das Logging angeschaltet",
    ],
)
def test_a_change_a_moment_ago_is_recognised(text: str) -> None:
    assert recent_change.just_changed(text)


@pytest.mark.parametrize(
    "text",
    [
        "Is client event logging enabled?",
        "Ich habe das Logging letzte Woche aktiviert, warum fehlen Ereignisse?",
        "Why did my phone drop off the Wi-Fi yesterday?",
    ],
)
def test_other_questions_are_not_a_change_just_made(text: str) -> None:
    assert not recent_change.just_changed(text)


@pytest.mark.parametrize(
    "sentence",
    [
        OFF,
        "The logging of client events isn't enabled yet.",
        "It seems to be disabled.",
        "Das Client-Logging ist nicht aktiviert.",
        "Die Protokollierung ist noch ausgeschaltet.",
    ],
)
def test_a_sentence_that_says_the_setting_is_off(sentence: str) -> None:
    assert recent_change.says_off(sentence)


@pytest.mark.parametrize(
    "sentence",
    [
        "The access point ap-1 is online.",
        "Not confirmed yet whether the logging is not enabled.",
        "The container web is not running.",
        # a real "off" of something else stays, also in a check that read the event log
        "The guest network is disabled, so it cannot be joined.",
        "It is not on the 5 GHz band.",
        "Die Firewall-Funktion ist deaktiviert.",
        "Es ist nicht an das Netz angeschlossen.",
    ],
)
def test_other_sentences_stay(sentence: str) -> None:
    assert not recent_change.says_off(sentence)


def test_the_answer_says_not_confirmed_yet_once_and_keeps_the_rest() -> None:
    text = f"{OFF} Turn it on under Logs. The AP is online.\n\n- The logging is off."
    guarded, removed = recent_change.guard(text, "en")
    assert removed == [OFF, "- The logging is off."]
    assert guarded.count("Not confirmed yet") == 1
    assert "The AP is online." in guarded and "not enabled" not in guarded
    assert "next client joining or leaving an access point" in guarded
    assert "check again" in guarded
    assert recent_change.guard("The AP is online.", "en") == ("The AP is online.", [])
    de, _ = recent_change.guard("Das Logging ist nicht aktiviert.", "de")
    assert de.startswith("Noch nicht bestätigt")


def test_the_check_is_told_only_with_the_event_log_and_a_change_just_made() -> None:
    tools = [{"capability": "omada.events"}, {"capability": "omada.clients"}]
    hint = agent_prompts.settings_change_hint(ASKED, tools)
    assert "not confirmed yet" in hint and "cannot read" in hint
    assert agent_prompts.settings_change_hint("Is logging on?", tools) == ""
    assert agent_prompts.settings_change_hint(ASKED, [{"capability": "docker.ps"}]) == ""


def test_the_task_counts_the_persons_messages_not_the_answers() -> None:
    asked = [("user", ASKED), ("assistant", "Not confirmed yet."), ("user", "and now?")]
    assert recent_change.just_changed_in_task(chat_tools.task_prompt("and now?", asked))
    # the hub's own German paragraph says "eben erst geändert": it must not count as the person's
    plain = [
        ("user", "Ist das Client-Logging an?"),
        ("assistant", recent_change.NOT_CONFIRMED["de"]),
        ("user", "und jetzt?"),
    ]
    assert not recent_change.just_changed_in_task(chat_tools.task_prompt("und jetzt?", plain))
    older = [("user", ASKED), ("assistant", "ok"), ("user", "thanks"), ("assistant", "-")]
    later = chat_tools.task_prompt("Is logging on?", [*older, ("user", "Is logging on?")])
    assert not recent_change.just_changed_in_task(later)  # only the message before counts


@pytest.fixture
def world(client: TestClient, connect):
    """The Omada fake: a tool node whose omada.events has no access-point client events, and a
    model node that answers the chat."""
    node_id, tnode = tools_node(client, connect, "vm-net", extra=("omada.events",))
    tnode.capability_answers["omada.events"] = EVENTS
    model = online_node(client, connect, "node-a", loaded=["small"])
    use_model(client)
    return node_id, tnode, model


def test_right_after_the_change_the_check_and_the_answer_do_not_say_off(
    client: TestClient, world
) -> None:
    _, _, model = world
    look = json.dumps(
        {
            "action": "tool",
            "node": "vm-net",
            "capability": "omada.events",
            "input": {},
            "reason": "see the client events",
        }
    )
    # the model of the check and the one of the answer both conclude "off", as live
    scripted = use(
        client,
        look,
        finish(OFF, [{"claim": OFF, "tag": "live", "evidence": ["E1"]}]),
    )
    model.stream_text_fn = lambda body: f"{OFF} You can switch it on under Settings."
    conv = new_conv(client)
    status, events, _ = ask_with(client, conv, ASKED, "on")
    assert status == 200 and events[-1][0] == "done"

    # the check was told, before it looked
    assert "not confirmed yet" in scripted.calls[0][1]["content"]
    card = next(d for e, d in events if e == "report")["message"]["report"]
    claims = [f["claim"] for f in card["findings"]] + [card["summary"]]
    assert not any(recent_change.says_off(c) for c in claims), claims
    assert any("not confirmed yet" in c.lower() for c in claims)

    answer = events[-1][1]["message"]["content"]
    assert not recent_change.says_off(answer) and "not enabled" not in answer
    assert answer.startswith("Not confirmed yet")
    assert "next client joining or leaving an access point" in answer
    assert "check again" in answer
    stored = client.get(f"/api/conversations/{conv}", headers=AUTH).json()["messages"]
    assert stored[-1]["content"] == answer
    assert sent_messages(model)  # the answer was written by the model, then guarded


def test_without_a_change_just_made_the_answer_stays(client: TestClient, world) -> None:
    _, _, model = world
    look = json.dumps(
        {"action": "tool", "node": "vm-net", "capability": "omada.events", "input": {}}
    )
    use(client, look, finish(OFF, [{"claim": OFF, "tag": "live", "evidence": ["E1"]}]))
    model.stream_text_fn = lambda body: OFF
    conv = new_conv(client)
    _, events, _ = ask_with(client, conv, "Is client event logging enabled on my Omada?", "on")
    assert events[-1][1]["message"]["content"] == OFF
