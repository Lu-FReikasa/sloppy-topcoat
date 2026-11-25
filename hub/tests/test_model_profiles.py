"""The best setting of a model for a kind of work (R6): the admin takes it from the bench, the
gateway adds it to the requests of that kind, and what a caller set itself is kept."""

import base64
import json
from typing import Any

import pytest
from fastapi.testclient import TestClient

from tests.conftest import AUTH
from tests.fakenode import online_node
from topcoat_hub.model_profiles import ProfileError, apply, check, function_of

CHAT = {"model": "small", "messages": [{"role": "user", "content": "Hallo Welt"}]}
TOOLS = {**CHAT, "tools": [{"type": "function", "function": {"name": "x", "parameters": {}}}]}


def test_the_kind_of_work_comes_from_the_request() -> None:
    assert function_of(CHAT) == "answer"
    assert function_of(TOOLS) == "tools"
    assert function_of(CHAT, "quality") == "review"
    assert function_of(CHAT, "memory") == "extract"
    assert function_of({**CHAT, "response_format": {"type": "json_object"}}) == "extract"


def test_a_stored_setting_is_added_and_the_callers_own_keys_stay() -> None:
    profile = {"tools": "nothink", "answer": "cold"}
    assert apply(dict(TOOLS), profile, "tools")["chat_template_kwargs"] == {
        "enable_thinking": False
    }
    assert apply(dict(CHAT), profile, "answer")["temperature"] == 0.0
    # nothing stored for this kind: the request is left as it is
    assert apply(dict(CHAT), profile, "review") == CHAT
    # the caller named a temperature itself: it stays
    assert apply({**CHAT, "temperature": 0.9}, profile, "answer")["temperature"] == 0.9
    assert apply(dict(CHAT), None, "answer") == CHAT


@pytest.mark.parametrize(
    "bad",
    [
        {"m": {"tools": "turbo"}},  # not a preset
        {"m": {"tools": ""}},  # the plain model is the absence of a setting, not a setting
        {"m": {"dreaming": "nothink"}},  # not a kind of work
        {"m@node-a": {"tools": "nothink"}},  # a model name, not a pin
        {"m": "nothink"},
        ["m"],
    ],
)
def test_only_known_presets_and_kinds_can_be_stored(bad: Any) -> None:
    with pytest.raises(ProfileError):
        check(bad)


def sent(node: Any) -> list[dict[str, Any]]:
    return [
        json.loads(base64.b64decode(r["body"]["body"]))
        for r in node.proxy_requests
        if r["body"]["path"] == "/v1/chat/completions"
    ]


def test_the_gateway_adds_the_taken_setting_to_the_requests_of_that_kind(
    client: TestClient, connect
) -> None:
    node = online_node(client, connect, "node-a", loaded=["small"])
    r = client.put(
        "/api/scheduling/model-profile",
        headers=AUTH,
        json={"model": "small", "function": "tools", "variant": "nothink"},
    )
    assert r.status_code == 200, r.text
    small = next(m for n in r.json()["nodes"] for m in n["models"] if m["name"] == "small")
    assert small["profile"] == {"tools": "nothink"}

    assert client.post("/v1/chat/completions", headers=AUTH, json=TOOLS).status_code == 200
    assert client.post("/v1/chat/completions", headers=AUTH, json=CHAT).status_code == 200
    with_tools, plain = sent(node)
    assert with_tools["chat_template_kwargs"] == {"enable_thinking": False}
    assert "chat_template_kwargs" not in plain  # another kind of work: untouched


def test_giving_a_setting_back_and_refusing_a_bad_one(client: TestClient, connect) -> None:
    node = online_node(client, connect, "node-a", loaded=["small"])
    url = "/api/scheduling/model-profile"
    body = {"model": "small", "function": "tools", "variant": "nothink"}
    assert client.put(url, headers=AUTH, json=body).status_code == 200
    assert client.put(url, headers=AUTH, json={**body, "variant": None}).status_code == 200
    client.post("/v1/chat/completions", headers=AUTH, json=TOOLS)
    assert "chat_template_kwargs" not in sent(node)[0]
    bad = client.put(url, headers=AUTH, json={**body, "variant": "turbo"})
    assert bad.status_code == 400 and bad.json()["detail"]["code"] == "invalid_profile"
    assert client.put(url, json=body).status_code == 401  # only an admin


def test_the_thinking_switch_is_said_per_family() -> None:
    """Issue 2.23: gpt-oss ignores enable_thinking and thought through the whole budget."""
    from topcoat_hub.model_profiles import GPT_OSS_THINK_RESERVE, family_of, thinking

    off = {"max_tokens": 600, "chat_template_kwargs": {"enable_thinking": False}}
    gpt = thinking("openai_gpt-oss-20b-MXFP4", off)
    assert gpt["reasoning_effort"] == "low"
    assert gpt["chat_template_kwargs"] == {"reasoning_effort": "low"}
    assert gpt["max_tokens"] == 600 + GPT_OSS_THINK_RESERVE
    on = thinking("openai_gpt-oss-20b-MXFP4", {"chat_template_kwargs": {"enable_thinking": True}})
    assert on["reasoning_effort"] == "medium" and "max_tokens" not in on
    # a caller's own effort stays
    own = {"reasoning_effort": "high", "chat_template_kwargs": {"enable_thinking": False}}
    assert thinking("gpt-oss-20b", own)["reasoning_effort"] == "high"
    # the families whose template reads the switch get it unchanged
    for model in ("Qwen3.5-9B-Q4_K_M", "gemma-4-12B-it-QAT-Q4_0", "Qwen3-Coder-30B-A3B-IQ4_NL"):
        assert thinking(model, off) is off
    assert thinking("gpt-oss-20b", {"max_tokens": 10}) == {"max_tokens": 10}  # no switch: as is
    assert family_of("Qwen3-Coder-30B-A3B-Instruct-IQ4_NL") == "qwen-coder"
    assert family_of("Qwen3.5-9B") == "qwen3.5" and family_of("unknown") == ""
