"""Quality levels of the chat (E3-04): fast, balanced, thorough — per conversation and per user,
the model per level, the limits of the check, and the big model's second look at the answer."""

import json
from typing import Any

import pytest
from fastapi.testclient import TestClient

from tests import test_chat_tools
from tests.conftest import AUTH, TEST_DB_URL
from tests.fakenode import online_node
from tests.policy_helpers import use_model_everywhere
from tests.test_agent import ScriptedModel, finish, use
from tests.test_chat import new_conv, sse_events, use_model
from tests.test_chat_tools import FINDINGS, ask_with
from tests.users import make_user
from topcoat_hub import quality


def three_looks(client: TestClient) -> ScriptedModel:
    looks = [
        json.dumps(
            {
                "action": "tool",
                "node": "vm-docker",
                "capability": "docker.logs",
                "input": {"container": name, "lines": 5},
                "reason": "look",
            }
        )
        for name in ("web", "db", "cache")
    ]
    return use(client, *looks, finish("web ran out of memory.", FINDINGS, "raise the memory limit"))


# --- the pieces ---------------------------------------------------------------------------------


def test_the_limits_grow_with_the_level_and_a_slow_machine_caps_them() -> None:
    fast, balanced, thorough = (quality.check_limits(x, False) for x in quality.LEVELS)
    assert fast.max_tool_calls == 2 < balanced.max_tool_calls < thorough.max_tool_calls
    assert fast.max_turns < balanced.max_turns < thorough.max_turns
    assert quality.check_limits("nonsense", False) == balanced
    slow = quality.check_limits("thorough", True)
    assert (
        slow.max_tool_calls == 2
        and slow.time_budget_seconds > quality.CHECK_LIMITS.time_budget_seconds
    )
    assert quality.check_limits("fast", True).max_tool_calls == 2


def test_a_check_ends_by_progress_with_the_counts_as_a_ceiling() -> None:
    fast, balanced, thorough = (quality.check_limits(x, False) for x in quality.LEVELS)
    for lim, base in ((balanced, quality.CHECK_LIMITS), (thorough, quality.THOROUGH_LIMITS)):
        assert lim.max_stalled_turns == quality.STALL_TURNS > 0
        assert lim.max_tool_calls == base.max_tool_calls * quality.CEILING_FACTOR
        assert lim.max_turns == base.max_turns * quality.CEILING_FACTOR
    assert fast.max_stalled_turns == 0 and fast.max_tool_calls == 2  # a fast check stays short
    slow = quality.check_limits("balanced", True)
    assert slow.max_stalled_turns == 0 and slow.max_tool_calls == 2  # so does one on a slow machine


def test_a_workshop_check_gets_room_for_clone_write_and_test() -> None:
    plain = quality.check_limits("balanced", False)
    shop = quality.check_limits("balanced", False, True)
    assert shop.max_tool_calls > plain.max_tool_calls
    assert shop.time_budget_seconds >= 600
    assert quality.check_limits("fast", True, True).max_tool_calls == 2


def test_the_level_does_not_choose_the_model_only_the_second_look_has_one() -> None:
    # the answer never follows the level: the picked model or the default
    assert quality.models_for("fast", None, "mid") == {"answer": "mid"}
    assert quality.models_for("balanced", None, "mid") == {"answer": "mid"}
    assert quality.models_for("thorough", None, "mid") == {"answer": "mid", "verify": "mid"}
    assert quality.models_for("thorough", "mine", "mid") == {"answer": "mine", "verify": "mine"}
    assert quality.models_for("fast", "mine", "mid") == {"answer": "mine"}


def test_the_verdict_is_read_from_a_json_object() -> None:
    v = quality.parse_verdict(
        'Sure: {"supported": false, "issues": ["no 95 % in the evidence"]}', "big"
    )
    assert v is not None and not v.ok and v.issues == ["no 95 % in the evidence"]
    ok = quality.parse_verdict('{"supported": true, "issues": []}', "big")
    assert ok is not None and ok.ok
    # "supported" with issues listed is not ok; garbage is no verdict
    mixed = quality.parse_verdict('{"supported": true, "issues": ["x"]}', "big")
    assert mixed is not None and not mixed.ok
    assert quality.parse_verdict("looks fine to me", "big") is None
    assert quality.parse_verdict('{"issues": []}', "big") is None


# --- settings -----------------------------------------------------------------------------------


def test_the_level_is_set_per_conversation_with_a_default_per_user_and_models_per_level(
    client: TestClient, connect
) -> None:
    online_node(client, connect, "node-a", loaded=["small", "big"])
    settings = client.get("/api/chat/settings", headers=AUTH).json()
    assert settings["user"]["quality"] == "balanced"
    r = client.put("/api/chat/settings", headers=AUTH, json={"quality": "thorough"})
    assert r.status_code == 200 and r.json()["user"]["quality"] == "thorough"
    # a later save without a level keeps it
    r = client.put("/api/chat/settings", headers=AUTH, json={"temperature": 0.3})
    assert r.json()["user"]["quality"] == "thorough"
    assert (
        client.put("/api/chat/settings", headers=AUTH, json={"quality": "max"}).status_code == 422
    )
    conv = new_conv(client)
    assert client.get(f"/api/conversations/{conv}", headers=AUTH).json()["quality"] is None
    r = client.patch(f"/api/conversations/{conv}", headers=AUTH, json={"quality": "fast"})
    assert r.status_code == 200 and r.json()["quality"] == "fast"
    r = client.patch(f"/api/conversations/{conv}", headers=AUTH, json={"quality": None})
    assert r.json()["quality"] is None  # back to the user's default
    assert (
        client.patch(f"/api/conversations/{conv}", headers=AUTH, json={"quality": "x"}).status_code
        == 422
    )


def test_the_level_of_one_user_is_not_the_level_of_another(client: TestClient) -> None:
    _, other = make_user(TEST_DB_URL, "bob")
    client.put("/api/chat/settings", headers=AUTH, json={"quality": "fast"})
    assert client.get("/api/chat/settings", headers=other).json()["user"]["quality"] == "balanced"


# --- answers ------------------------------------------------------------------------------------


def start_events(
    client: TestClient, conv: str, text: str, tools: str = "on"
) -> list[tuple[str, dict[str, Any]]]:
    _, events, _ = ask_with(client, conv, text, tools)
    return events


def test_the_level_does_not_pick_the_model_but_a_picked_model_wins(
    client: TestClient, connect
) -> None:
    online_node(client, connect, "node-a", loaded=["small", "big"])
    use_model_everywhere(client, "big")
    conv = new_conv(client)
    client.patch(f"/api/conversations/{conv}", headers=AUTH, json={"quality": "fast"})
    r = client.post(f"/api/conversations/{conv}/messages", headers=AUTH, json={"content": "hallo"})
    meta = sse_events(r.text)[0][1]
    # a model configured for the level is not used: the level is not coupled to a model
    assert meta["model"] == "big" and meta["quality"] == "fast"
    client.patch(f"/api/conversations/{conv}", headers=AUTH, json={"model": "small"})
    r = client.post(
        f"/api/conversations/{conv}/messages", headers=AUTH, json={"content": "nochmal"}
    )
    assert sse_events(r.text)[0][1]["model"] == "small"  # the conversation's own choice wins
    conv2 = new_conv(
        client
    )  # no level of its own: the user's default (balanced) and the hub default
    r = client.post(f"/api/conversations/{conv2}/messages", headers=AUTH, json={"content": "hallo"})
    meta = sse_events(r.text)[0][1]
    assert meta["model"] == "big" and meta["quality"] == "balanced"


def test_fast_holds_the_check_to_two_calls_and_makes_no_second_look(
    client: TestClient, world
) -> None:
    three_looks(client)
    conv = new_conv(client)
    client.patch(f"/api/conversations/{conv}", headers=AUTH, json={"quality": "fast"})
    events = start_events(client, conv, "Prüf mal meine Container")
    done = next(d for e, d in events if e == "tools_done")
    assert done["calls"] == 2 and events[0][1]["quality"] == "fast"
    assert "verifying" not in [e for e, _ in events]


def test_balanced_is_the_behaviour_of_before(client: TestClient, world) -> None:
    three_looks(client)
    conv = new_conv(client)
    events = start_events(client, conv, "Prüf mal meine Container")
    done = next(d for e, d in events if e == "tools_done")
    assert done["calls"] == 3 and "verifying" not in [e for e, _ in events]


def big_is_loaded(client: TestClient) -> None:
    """Both models are in memory: a second look never loads one to have a checker (SCH-02)."""

    async def both() -> list[dict]:
        nodes = [{"node": "node-a", "loaded": True, "state": "available"}]
        return [{"id": m, "topcoat": {"nodes": nodes}} for m in ("big", "small")]

    client.app.state.gateway.list_models = both


def test_thorough_has_the_big_model_look_at_the_answer_again(client: TestClient, world) -> None:
    _, _, node = world
    seen: list[str] = []

    def verifier(req: dict[str, Any]) -> str | None:
        if req["messages"][0]["content"].startswith("You check an answer"):
            seen.append(req["messages"][-1]["content"])
            return json.dumps({"supported": False, "issues": ["the 95 % is not in the evidence"]})
        return None

    node.answer_fn = verifier
    big_is_loaded(client)
    three_looks(client)
    conv = new_conv(client)
    client.patch(f"/api/conversations/{conv}", headers=AUTH, json={"quality": "thorough"})
    events = start_events(client, conv, "Prüf mal meine Container")
    kinds = [e for e, _ in events]
    done = next(d for e, d in events if e == "tools_done")
    assert done["calls"] == 3 and events[0][1]["verify_model"] == "small"
    assert kinds.index("done") < kinds.index("verifying") < kinds.index("verification")
    v = events[-1][1]
    assert (
        v["available"] is True
        and v["ok"] is False
        and v["issues"] == ["the 95 % is not in the evidence"]
    )
    # another model than the answering one looked at it; no key: nothing to offer
    assert v["model"] == "big" and v["same_model"] is False
    # what the verifier read: the question, the evidence, the answer
    assert (
        "Prüf mal meine Container" in seen[0]
        and "Evidence:" in seen[0]
        and "Answer to check:" in seen[0]
    )
    # stored with the answer
    stored = client.get(f"/api/conversations/{conv}", headers=AUTH).json()["messages"][-1]
    assert stored["verification"]["issues"] == ["the 95 % is not in the evidence"]
    assert stored["verification"]["ok"] is False and stored["verification"]["at"]


def test_a_verifier_that_cannot_answer_leaves_the_answer_alone(client: TestClient, world) -> None:
    _, _, node = world
    node.answer_fn = lambda req: (
        "I cannot say." if req["messages"][0]["content"].startswith("You check an answer") else None
    )
    three_looks(client)
    conv = new_conv(client)
    client.patch(f"/api/conversations/{conv}", headers=AUTH, json={"quality": "thorough"})
    events = start_events(client, conv, "Prüf mal meine Container")
    assert events[-1][0] == "verification" and events[-1][1]["available"] is False
    stored = client.get(f"/api/conversations/{conv}", headers=AUTH).json()["messages"][-1]
    assert stored["verification"] is None and stored["status"] == "complete"


def test_a_plain_answer_gets_a_second_look_only_at_thorough(client: TestClient, world) -> None:
    _, _, node = world
    seen: list[str] = []

    def answer(req: dict) -> str | None:
        if req["messages"][0]["content"].startswith("You review an answer"):
            seen.append(req["messages"][-1]["content"])
            return json.dumps({"supported": False, "issues": ["the version number is a guess"]})
        return None

    node.answer_fn = answer
    big_is_loaded(client)
    for level, looked in (("fast", False), ("balanced", False), ("thorough", True)):
        conv = new_conv(client)
        client.patch(f"/api/conversations/{conv}", headers=AUTH, json={"quality": level})
        _, events, _ = ask_with(client, conv, "Erzähl mir etwas über Pinguine.", "off")
        assert ("verifying" in [e for e, _ in events]) is looked, level
        if looked:
            stored = client.get(f"/api/conversations/{conv}", headers=AUTH).json()["messages"][-1]
            assert stored["verification"]["issues"] == ["the version number is a guess"]
            # another model than the answering one looked at it
            assert stored["verification"]["model"] != "small"
            assert stored["verification"]["same_model"] is False
    # no evidence is sent along: the reviewer only marks what looks wrong
    # Gründlich holds its answer until the check passes: this checker never passes, so the answer
    # is checked, revised twice (the stage's revisions), and checked again: three looks, all plain
    assert len(seen) == 3 and all("Evidence:" not in s and "Answer to check:" in s for s in seen)


world = test_chat_tools.world  # the tool node and the model node of the chat-check tests


@pytest.fixture(autouse=True)
def _default_model(client: TestClient) -> None:
    use_model(client)


def test_when_no_other_model_exists_the_same_one_checks_and_says_so(
    client: TestClient, world
) -> None:
    _, _, node = world
    node.answer_fn = lambda req: (
        json.dumps({"supported": True, "issues": []})
        if req["messages"][0]["content"].startswith("You review an answer")
        else None
    )

    async def only_small() -> list[dict]:
        return [{"id": "small", "topcoat": {"nodes": [{"node": "node-a", "loaded": True}]}}]

    client.app.state.gateway.list_models = only_small
    conv = new_conv(client)
    client.patch(f"/api/conversations/{conv}", headers=AUTH, json={"quality": "thorough"})
    ask_with(client, conv, "Erzähl mir etwas über Pinguine.", "off")
    stored = client.get(f"/api/conversations/{conv}", headers=AUTH).json()["messages"][-1]
    assert (
        stored["verification"]["same_model"] is True and stored["verification"]["model"] == "small"
    )


def test_a_second_look_never_loads_a_model_to_have_a_checker(client: TestClient, world) -> None:
    """ "big" is on disk only: the answering model looks again and the verification says so."""
    _, _, node = world
    node.answer_fn = lambda req: (
        json.dumps({"supported": True, "issues": []})
        if req["messages"][0]["content"].startswith("You review an answer")
        else None
    )

    async def big_on_disk() -> list[dict]:
        loaded = [{"node": "node-a", "loaded": True, "state": "available"}]
        on_disk = [{"node": "node-a", "loaded": False, "state": "available"}]
        return [
            {"id": "big", "topcoat": {"nodes": on_disk}},
            {"id": "small", "topcoat": {"nodes": loaded}},
        ]

    client.app.state.gateway.list_models = big_on_disk
    conv = new_conv(client)
    client.patch(f"/api/conversations/{conv}", headers=AUTH, json={"quality": "thorough"})
    ask_with(client, conv, "Erzähl mir etwas über Pinguine.", "off")
    stored = client.get(f"/api/conversations/{conv}", headers=AUTH).json()["messages"][-1]
    assert stored["verification"]["model"] == "small" and stored["verification"]["same_model"]
