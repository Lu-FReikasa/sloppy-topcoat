"""The agent's loop limits: reads of one file are counted whatever the range, and the agent's own
model calls carry the thinking switch of the model family (same table as the chat)."""

import asyncio
from typing import Any

import pytest
from fastapi.testclient import TestClient

from tests.test_agent import finish, start, tool, use, vm, wait_for  # noqa: F401
from topcoat_hub import agent_loop
from topcoat_hub.agent_loop import file_read_key
from topcoat_hub.agent_service import GatewayModel
from topcoat_hub.gateway import GatewayError
from topcoat_hub.live_settings import BY_KEY

# --- which calls read "the same file" -------------------------------------------------------


def test_a_workshop_read_is_keyed_by_file_not_by_range() -> None:
    a = file_read_key("dev", "repo.read", {"repo": "app", "path": "a.py", "start_line": 1})
    b = file_read_key("dev", "repo.read", {"repo": "app", "path": "a.py", "start_line": 400})
    other = file_read_key("dev", "repo.read", {"repo": "app", "path": "b.py"})
    assert a is not None and a == b and a != other


def test_a_github_file_read_is_keyed_by_file_and_ref_not_by_range_or_find() -> None:
    base = {"what": "file", "repo": "app", "path": "docs/x.md"}
    a = file_read_key("github", "github.read", {**base, "start_line": 1, "end_line": 50})
    b = file_read_key("github", "github.read", {**base, "find": "needle"})
    on_branch = file_read_key("github", "github.read", {**base, "ref": "dev"})
    assert a is not None and a == b and a != on_branch


def test_calls_that_read_no_single_file_have_no_key() -> None:
    assert file_read_key("github", "github.read", {"what": "prs"}) is None
    assert file_read_key("github", "github.read", {"what": "file", "path": ""}) is None
    assert file_read_key("dev", "repo.list", {"repo": "app", "path": "a.py"}) is None
    assert file_read_key("dev", "repo.read", {"repo": "app"}) is None


# --- the budget in a real run ---------------------------------------------------------------


def test_a_file_read_again_and_again_with_other_ranges_is_refused_after_the_budget(
    client: TestClient,
    vm,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The fake tool node has no workshop; docker.logs of one container stands in for "one file"."""
    _, node = vm
    real = agent_loop.file_read_key

    def key(node_name: str, capability: str, call_input: dict[str, Any]) -> str | None:
        if capability == "docker.logs":
            return f"{node_name}/{call_input.get('container')}"
        return real(node_name, capability, call_input)

    monkeypatch.setattr(agent_loop, "file_read_key", key)
    model = use(
        client,
        tool("vm-docker", "docker.logs", container="web", lines=1),
        tool("vm-docker", "docker.logs", container="web", lines=2),
        tool("vm-docker", "docker.logs", container="web", lines=3),
        finish("Enough."),
        max_file_reads=2,
    )
    wait_for(client, start(client).json()["id"], "done")
    assert len(node.capability_calls) == 2
    refused = model.calls[3][-1]["content"]
    assert "You already read this file 2 times" in refused and "[E1]" in refused


# --- the thinking switch of the agent's own model calls -------------------------------------


class _Capture:
    def __init__(self) -> None:
        self.payloads: list[dict[str, Any]] = []

    async def route(self, model: str) -> Any:
        raise GatewayError(503, "model_unavailable", "the stub knows no routes")

    async def call(self, model: str, path: str, payload: dict[str, Any], **_: Any) -> Any:
        self.payloads.append(payload)
        return {"choices": [{"message": {"content": '{"action":"finish"}'}}], "usage": {}}


def _complete(model_name: str, **settings: Any) -> dict[str, Any]:
    base = {
        "agent_tool_mode": "auto",
        "default_chat_model": "m",
        "agent_min_context": 0,
        "agent_stream": False,
    }
    s = type("S", (), {**base, **settings})()
    gw = _Capture()
    model = GatewayModel(gw, None, s)  # type: ignore[arg-type]
    asyncio.run(model.complete([{"role": "user", "content": "x"}], model_name, 50))
    return gw.payloads[0]


THINKING_OFF = {"gemma": {"fast": {"chat_template_kwargs": {"enable_thinking": False}}}}


def test_the_agent_turns_thinking_off_where_the_effort_table_says_so() -> None:
    payload = _complete("gemma-4-12B@node-amd", chat_effort=THINKING_OFF, agent_effort="fast")
    assert payload["chat_template_kwargs"] == {"enable_thinking": False}


def test_a_level_without_an_entry_and_a_model_without_a_family_send_nothing() -> None:
    off = _complete("gemma-4-12B@node-amd", chat_effort=THINKING_OFF, agent_effort="thorough")
    other = _complete("gpt-oss-20b@node-amd", chat_effort=THINKING_OFF, agent_effort="fast")
    assert "chat_template_kwargs" not in off and "chat_template_kwargs" not in other


def test_the_agent_level_is_a_live_setting_with_the_three_levels() -> None:
    param = BY_KEY["agent_effort"]
    assert param.kind == "enum" and param.choices == ("fast", "balanced", "thorough")
