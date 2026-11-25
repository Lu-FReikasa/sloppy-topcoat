"""The chat shows what the hub does (E7-01, D56): thoughts, the call that runs now, a call that
failed — as ``activity`` events while the answer grows, and stored with the answer."""

import json
from typing import Any

from fastapi.testclient import TestClient

from tests.conftest import AUTH
from tests.fakenode import online_node
from tests.test_agent import finish, use
from tests.test_chat import answer_text, new_conv, use_model
from tests.test_chat_tools import (  # noqa: F401  (``world`` is a fixture)
    FINDINGS,
    ask_with,
    script_check,
    use_native_model,
    world,
)
from topcoat_hub.agent_loop import ModelFailure

FINISH = finish("web ran out of memory.", FINDINGS, "raise the memory limit")


def activity_of(events: list[tuple[str, dict[str, Any]]]) -> list[dict[str, Any]]:
    """The items as the browser would build them: ``add`` appends, ``update`` replaces by id."""
    items: dict[int, dict[str, Any]] = {}
    for name, data in events:
        if name == "activity":
            items[data["item"]["id"]] = data["item"]
    return list(items.values())


def test_reasoning_of_the_model_is_activity_and_not_part_of_the_answer(
    client: TestClient, connect
) -> None:
    node = online_node(client, connect, "node-a", loaded=["small"])
    node.stream_reasoning = "erst die Lage prüfen dann antworten"
    use_model(client)
    conv = new_conv(client)
    status, events, _ = ask_with(client, conv, "Hallo Welt", "off")
    assert status == 200
    assert "erst die Lage" not in answer_text(events)
    kinds = [e for e, _ in events]
    assert kinds.index("activity") < kinds.index("delta")  # the thought comes first, live
    items = activity_of(events)
    assert [i["kind"] for i in items] == ["thinking"]
    assert (
        items[0]["status"] == "ok" and items[0]["detail"] == "erst die Lage prüfen dann antworten"
    )

    done = events[-1][1]["message"]
    assert [i["kind"] for i in done["activity"]] == ["thinking"]
    stored = client.get(f"/api/conversations/{conv}", headers=AUTH).json()["messages"]
    assert stored[1]["activity"] == done["activity"]  # kept with the answer, shown after a reload
    assert stored[0]["activity"] == []  # the person's own message has none


def test_a_think_block_in_the_text_is_activity_and_not_part_of_the_answer(
    client: TestClient, connect
) -> None:
    node = online_node(client, connect, "node-a", loaded=["small"])
    node.stream_text_fn = lambda req: "<think>zuerst nachdenken</think>Die Antwort ist 42."
    use_model(client)
    conv = new_conv(client)
    _, events, _ = ask_with(client, conv, "Frage", "off")
    assert answer_text(events) == "Die Antwort ist 42."
    items = activity_of(events)
    assert [i["kind"] for i in items] == ["thinking"] and "zuerst nachdenken" in items[0]["detail"]
    assert events[-1][1]["message"]["content"] == "Die Antwort ist 42."


def test_an_answer_without_anything_to_show_has_an_empty_activity(
    client: TestClient, connect
) -> None:
    online_node(client, connect, "node-a", loaded=["small"])
    use_model(client)
    conv = new_conv(client)
    _, events, _ = ask_with(client, conv, "Hallo", "off")
    assert "activity" not in [e for e, _ in events]
    assert events[-1][1]["message"]["activity"] == []


def test_the_lead_in_before_a_check_is_kept_and_the_check_is_a_chain_of_items(
    client: TestClient,
    world,  # noqa: F811
) -> None:
    _, _tnode, node = world
    node.tool_call_fn = lambda req: {"what": "why web restarts"}
    use_native_model(
        client,
        json.dumps(
            {
                "action": "tool",
                "node": "vm-docker",
                "capability": "docker.logs",
                "input": {"container": "web", "lines": 50},
                "reason": "see why web restarts",
            }
        ),
        FINISH,
    )
    conv = new_conv(client)
    _, events, _ = ask_with(client, conv, "Hast du eine Idee, warum bei mir alles so hakt?")
    kinds = [e for e, _ in events]
    assert "discard" in kinds  # the bubble is cleared, as before ...
    items = activity_of(events)
    assert [i["kind"] for i in items] == ["lead_in", "check", "tool"]
    lead, check, tool = items
    assert "Ich schaue nach" in lead["detail"] and lead["status"] == "info"
    assert tool["title"] == "docker.logs @ vm-docker" and tool["status"] == "ok"
    assert tool["detail"] == "see why web restarts" and tool["data"]["ref"] == "E1"
    assert check["status"] == "ok" and check["data"]["calls"] == 1
    # the order of the stream: the lead-in is recorded before the browser is told to drop it
    first = next(i for i, (e, d) in enumerate(events) if e == "activity")
    assert first < kinds.index("discard")
    done = events[-1][1]["message"]
    assert [i["kind"] for i in done["activity"]] == ["lead_in", "check", "tool"]


def test_a_call_that_fails_shows_as_failed_at_once_and_stays_in_the_record(
    client: TestClient,
    world,  # noqa: F811
) -> None:
    _, tnode, _ = world
    tnode.capability_answers["docker.logs"] = {
        "error": {"code": "capability_failed", "message": "boom"}
    }
    script_check(client)
    conv = new_conv(client)
    _, events, _ = ask_with(client, conv, "Was ist mit web los?", "on")
    items = activity_of(events)
    tool = next(i for i in items if i["kind"] == "tool")
    assert tool["status"] == "failed" and tool["detail"] == "capability_failed"
    # while it ran, the stream already said so: an ``add`` with ``running`` came before the result
    running = [
        d["item"]["status"] for e, d in events if e == "activity" and d["item"]["id"] == tool["id"]
    ]
    assert running[0] == "running" and running[-1] == "failed"
    done = events[-1][1]["message"]
    stored_tool = next(i for i in done["activity"] if i["kind"] == "tool")
    assert stored_tool["status"] == "failed"


def test_a_check_that_could_not_start_is_a_failed_check_item(
    client: TestClient,
    world,  # noqa: F811
) -> None:
    reason = "no node could serve 'big': not enough free RAM"
    use(client, ModelFailure(reason, reason="insufficient_memory"))
    conv = new_conv(client)
    _, events, _ = ask_with(client, conv, "Was ist mit web los?", "on")
    check = next(i for i in activity_of(events) if i["kind"] == "check")
    assert check["status"] == "failed" and "not enough free RAM" in check["detail"]


def test_an_answer_that_ends_on_an_announcement_is_marked_as_not_done(
    client: TestClient, connect
) -> None:
    node = online_node(client, connect, "node-a", loaded=["small"])
    node.stream_text_fn = lambda req: "Gerne, ich prüfe das jetzt."
    use_model(client)
    conv = new_conv(client)
    _, events, _ = ask_with(client, conv, "Wie voll ist die Platte?", "off")
    items = activity_of(events)
    assert [(i["kind"], i["status"]) for i in items] == [("announce", "failed")]
    assert items[0]["detail"] == "Gerne, ich prüfe das jetzt."
    done = events[-1][1]["message"]
    assert done["status"] == "complete" and done["content"] == "Gerne, ich prüfe das jetzt."
    assert [i["kind"] for i in done["activity"]] == ["announce"]  # kept with the answer


def test_a_finished_answer_and_an_offer_are_not_marked(client: TestClient, connect) -> None:
    node = online_node(client, connect, "node-a", loaded=["small"])
    use_model(client)
    conv = new_conv(client)
    node.stream_text_fn = lambda req: "Die Platte ist zu 40 Prozent voll."
    _, events, _ = ask_with(client, conv, "Wie voll ist die Platte?", "off")
    assert "activity" not in [e for e, _ in events]
    node.stream_text_fn = lambda req: "Wenn du willst, schaue ich nach den Logs."
    _, events, _ = ask_with(client, conv, "Und die Logs?", "off")
    assert "activity" not in [e for e, _ in events]


def test_a_check_that_ran_is_not_flagged_for_what_the_closing_words_say(
    client: TestClient,
    world,  # noqa: F811
) -> None:
    _, _tnode, node = world
    script_check(client)
    node.stream_text_fn = lambda req: "Ich schaue noch einmal gleich nach."
    conv = new_conv(client)
    _, events, _ = ask_with(client, conv, "Was ist mit web los?", "on")
    assert "announce" not in [i["kind"] for i in activity_of(events)]
