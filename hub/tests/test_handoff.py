"""Hand-over of heavy chat requests to the task queue (Aufgaben statt Chats, step 5)."""

import pytest
from fastapi.testclient import TestClient

from tests.conftest import AUTH
from tests.fakenode import online_node
from tests.test_agent import finish, task, use, wait_for
from tests.test_chat import chat_requests, new_conv, use_model
from tests.test_chat_tools import FINDINGS, ask_with
from tests.test_tools import tools_node
from topcoat_hub import handoff


@pytest.mark.parametrize(
    "text",
    [
        "Mach das bitte im Hintergrund, kein Stress",
        "Das darf länger dauern: vergleiche die Backup-Strategien",
        "Schreib mir einen ausführlichen Bericht über meine Container",
        "Take your time and write a detailed report on this",
        "Gib das als Aufgabe ab",
    ],
)
def test_requests_that_may_take_long_are_handed_over(text: str) -> None:
    assert handoff.wanted(text)


@pytest.mark.parametrize(
    "text",
    [
        "Wie spät ist es?",
        "Schreib mir bitte dringend einen Bericht",
        "Mach das schnell im Hintergrund",
        "Explain TCP",
    ],
)
def test_plain_and_urgent_requests_stay_in_the_chat(text: str) -> None:
    assert not handoff.wanted(text)


@pytest.fixture
def world(client: TestClient, connect):
    tools_node(client, connect, "vm-docker")
    model = online_node(client, connect, "node-a", loaded=["small"])
    use_model(client)
    return model


def test_a_heavy_request_becomes_a_task_with_a_sentence_of_the_hubs_own(
    client: TestClient, world
) -> None:
    use(client, finish("done", FINDINGS, "nothing"))
    conv = new_conv(client)
    status, events, _ = ask_with(client, conv, "Schreib mir einen ausführlichen Bericht über web")
    assert status == 200
    names = [e for e, _ in events]
    assert names == ["meta", "done"]
    message = events[-1][1]["message"]
    assert message["kind"] == "handoff" and "Aufgabe" in message["content"]
    assert message["agent_task_id"] and message["report"]["task_id"] == message["agent_task_id"]
    assert chat_requests(world) == []  # no model was asked for the chat answer
    t = task(client, message["agent_task_id"])
    assert t["conversation_id"] == conv and t["deadline_at"] is None
    assert t["origin"] == "tasks"  # listed on the Tasks page, its report goes to the library


def test_an_urgent_request_is_answered_in_the_chat(client: TestClient, world) -> None:
    conv = new_conv(client)
    _, events, _ = ask_with(client, conv, "Schreib mir dringend einen Bericht über web")
    assert "done" in [e for e, _ in events]
    assert all(d.get("message", {}).get("kind") != "handoff" for _, d in events)
    assert client.get("/api/agent/tasks", headers=AUTH).json() == []


def test_without_a_tool_node_the_chat_answers_as_usual(client: TestClient, connect) -> None:
    online_node(client, connect, "node-a", loaded=["small"])
    use_model(client)
    conv = new_conv(client)
    _, events, _ = ask_with(client, conv, "Mach das im Hintergrund: erkläre TCP")
    assert [d["message"]["kind"] for e, d in events if e == "done"] == ["say"]


def test_a_handed_over_task_has_no_deadline_and_no_start_now_button_endpoint(
    client: TestClient, world
) -> None:
    use(client, finish("done", FINDINGS, "nothing"))
    conv = new_conv(client)
    _, events, _ = ask_with(client, conv, "Im Hintergrund bitte: prüfe web")
    task_id = events[-1][1]["message"]["agent_task_id"]
    # the task is in the queue like any other: it waits for a free node, with no timer to end
    assert task(client, task_id)["deadline_at"] is None
    wait_for(client, task_id, "done", "failed")
    assert client.post(f"/api/agent/tasks/{task_id}/start-now", headers=AUTH).status_code == 404


# --- the effort control decides (fast / balanced / thorough) ----------------------------------


@pytest.mark.parametrize(
    ("level", "text", "expected"),
    [
        ("fast", "Schreib mir einen ausführlichen Bericht", None),
        ("fast", "Erkläre TCP", None),
        ("balanced", "Erkläre TCP", None),
        ("balanced", "Schreib mir einen ausführlichen Bericht", "asked"),
        ("thorough", "Erkläre TCP", None),
        ("thorough", "Schreib mir dringend einen Bericht", None),
        ("thorough", "Mach das im Hintergrund", "asked"),
    ],
)
def test_only_the_users_words_and_the_level_decide_who_hands_over(level, text, expected) -> None:
    assert handoff.reason(level, text) == expected


def test_a_slow_node_does_not_hand_a_plain_request_over(client: TestClient, world) -> None:
    """The speed of a node is no reason to queue: the request waits for a free node in the pool."""
    use(client, finish("done", FINDINGS, "nothing"))
    conv = new_conv(client)
    _, events, _ = ask_with(client, conv, "Erkläre mir bitte TCP")
    assert events[-1][1]["message"]["kind"] == "say"


def test_fast_answers_in_the_chat_even_when_a_report_is_asked_for(
    client: TestClient, world
) -> None:
    conv = new_conv(client)
    client.patch(f"/api/conversations/{conv}", headers=AUTH, json={"quality": "fast"})
    _, events, _ = ask_with(client, conv, "Schreib mir einen ausführlichen Bericht über web")
    assert events[-1][1]["message"]["kind"] == "say"
