"""Effort by quality level (prototype): besides the steps of the check, the level can tell a model
family how long to think. Nothing is built in; the settings name the families. A model without an
entry is asked exactly as before."""

from typing import Any

from fastapi.testclient import TestClient

from tests.conftest import AUTH
from tests.test_chat import new_conv
from tests.test_sampling import asked_with
from topcoat_hub import chat

TABLE: dict[str, dict[str, dict[str, Any]]] = {
    "qwen": {
        "fast": {"reasoning_effort": "low"},
        "thorough": {"reasoning_effort": "high", "chat_template_kwargs": {"enable_thinking": True}},
    },
    "qwen3-big": {"fast": {"reasoning_effort": "none"}},
}


def test_nothing_is_sent_without_an_entry() -> None:
    assert chat.effort_params("qwen2.5-7b", "fast") == {}
    assert chat.effort_params("qwen2.5-7b", "balanced", TABLE) == {}  # level without entry
    assert chat.effort_params("gemma-4-12b", "fast", TABLE) == {}  # family without entry
    assert chat.effort_families(None) == []


def test_the_level_picks_the_parameters_of_the_family() -> None:
    assert chat.effort_params("Qwen2.5-7B", "fast", TABLE) == {"reasoning_effort": "low"}
    assert chat.effort_params("qwen2.5-7b", "thorough", TABLE) == {
        "reasoning_effort": "high",
        "chat_template_kwargs": {"enable_thinking": True},
    }
    # the longest family name wins
    assert chat.effort_params("qwen3-big-q4", "fast", TABLE) == {"reasoning_effort": "none"}
    assert chat.effort_families(TABLE) == ["qwen", "qwen3-big"]


def test_only_documented_parameters_with_sane_values_get_through() -> None:
    bad = {
        "qwen": {
            "fast": {"reasoning_effort": "ludicrous", "temperature": 9, "top_k": 1},
            "balanced": {"chat_template_kwargs": {1: "x"}},
            "thorough": "high",
        }
    }
    assert chat.effort_params("qwen", "fast", bad) == {}
    assert chat.effort_params("qwen", "balanced", bad) == {}
    assert chat.effort_params("qwen", "thorough", bad) == {}


def test_the_request_carries_what_the_level_says(client: TestClient, connect: Any) -> None:
    client.app.state.settings.chat_effort = TABLE
    client.put("/api/chat/settings", headers=AUTH, json={"quality": "fast"})
    body = asked_with(client, connect, "qwen2.5-7b-instruct")
    assert body["reasoning_effort"] == "low"


def test_a_request_without_an_entry_is_unchanged(client: TestClient, connect: Any) -> None:
    body = asked_with(client, connect, "qwen2.5-7b-instruct")
    assert "reasoning_effort" not in body and "chat_template_kwargs" not in body


def test_the_settings_name_the_families(client: TestClient) -> None:
    new_conv(client)
    assert client.get("/api/chat/settings", headers=AUTH).json()["hub"]["effort_families"] == []
    client.app.state.settings.chat_effort = TABLE
    hub = client.get("/api/chat/settings", headers=AUTH).json()["hub"]
    assert hub["effort_families"] == ["qwen", "qwen3-big"]
