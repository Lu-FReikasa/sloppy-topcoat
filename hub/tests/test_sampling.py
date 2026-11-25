"""Sampling defaults by model family (E3-07 D4 (c)): "die Lage Lage Lage …" — loops of some models.
The hub sends a mild repetition penalty with chat requests for the families known to need it; the
settings change or remove it. (Whether the value cures the loops of the user's own model is
manual check; the hub only guarantees that it is sent, and to whom.)"""

from typing import Any

import pytest
from fastapi.testclient import TestClient

from tests.fakenode import online_node
from tests.test_chat import ask as ask_chat
from tests.test_chat import chat_requests, new_conv, use_model
from topcoat_hub import chat


@pytest.mark.parametrize(
    "model",
    ["gemma-4-12b-it-q4", "Gemma-4-27B", "bartowski/gemma-4-12b-GGUF", "gemma-3-4b"],
)
def test_gemma_gets_a_repetition_penalty_by_default(model: str) -> None:
    assert chat.sampling_params(model) == {"repeat_penalty": 1.1}


@pytest.mark.parametrize("model", ["qwen2.5-7b-instruct", "llama-3.1-8b", "small", ""])
def test_other_models_are_left_to_their_server(model: str) -> None:
    assert chat.sampling_params(model) == {}


def test_the_settings_replace_a_default_or_add_a_family() -> None:
    overrides = {"gemma": {"repeat_penalty": 1.2, "repeat_last_n": 128}, "qwen": {"top_k": 20}}
    assert chat.sampling_params("gemma-4-12b", overrides) == {
        "repeat_penalty": 1.2,
        "repeat_last_n": 128,
    }
    assert chat.sampling_params("qwen2.5-7b", overrides) == {"top_k": 20}
    assert chat.sampling_params("gemma-4-12b", {"gemma": {}}) == {}  # switched off
    # the longest family name wins: a particular model can differ from its family
    assert chat.sampling_params("gemma-4-12b", {"gemma-4": {"repeat_penalty": 1.05}}) == {
        "repeat_penalty": 1.05
    }
    assert chat.sampling_params("GEMMA-4-12B", {"Gemma-4": {"repeat_penalty": 1.3}}) == {
        "repeat_penalty": 1.3
    }


def asked_with(client: TestClient, connect: Any, model: str) -> dict[str, Any]:
    node = online_node(client, connect, "node-a", loaded=[model])
    use_model(client, model)
    status, _events, _ = ask_chat(
        client, new_conv(client), "Erkläre mir bitte kurz, was ein Router macht"
    )
    assert status == 200
    return chat_requests(node)[-1]


def test_a_gemma_chat_request_carries_the_repetition_penalty(
    client: TestClient, connect: Any
) -> None:
    body = asked_with(client, connect, "gemma-4-12b-it")
    assert body["repeat_penalty"] == 1.1 and body["stream"] is True


def test_a_request_to_another_model_carries_none(client: TestClient, connect: Any) -> None:
    body = asked_with(client, connect, "qwen2.5-7b-instruct")
    assert "repeat_penalty" not in body


def test_the_chosen_temperature_is_kept_next_to_it(client: TestClient, connect: Any) -> None:
    from tests.conftest import AUTH

    node = online_node(client, connect, "node-a", loaded=["gemma-4-12b-it"])
    use_model(client, "gemma-4-12b-it")
    client.put("/api/chat/settings", headers=AUTH, json={"temperature": 0.4})
    ask_chat(client, new_conv(client), "Erkläre mir bitte kurz, was ein Router macht")
    body = chat_requests(node)[-1]
    assert body["temperature"] == 0.4 and body["repeat_penalty"] == 1.1


def test_the_settings_of_the_hub_change_what_is_sent(client: TestClient, connect: Any) -> None:
    client.app.state.settings.chat_sampling = {"gemma": {"repeat_penalty": 1.25}}
    body = asked_with(client, connect, "gemma-4-12b-it")
    assert body["repeat_penalty"] == 1.25
