"""The agent's native tool calls (E3-01c): the OpenAI ``tools`` / ``tool_calls`` way of talking to
a model next to the prompt-based JSON protocol, chosen per model, with the fallback."""

import asyncio
import dataclasses
import json
import uuid
from typing import Any

import pytest
from fastapi.testclient import TestClient

from tests.conftest import AUTH, TEST_DB_URL
from tests.test_agent import start, vm, wait_for  # noqa: F401  (vm is a fixture)
from tests.users import make_user
from topcoat_hub import agent_prompts as prompts
from topcoat_hub.agent_loop import Completion, NativeUnsupported
from topcoat_hub.gateway import Candidate, GatewayError


def call(name: str, **args: Any) -> Completion:
    """A reply that is a native tool call."""
    return Completion(
        text="",
        tokens=5,
        model="native-model",
        tool_calls=[
            {
                "id": f"call_{name}",
                "type": "function",
                "function": {"name": name, "arguments": json.dumps(args)},
            }
        ],
    )


def say(text: str) -> Completion:
    return Completion(text=text, tokens=5, model="native-model")


class NativeModel:
    """A scripted model that can be asked natively; records what it was shown."""

    def __init__(self, *replies: Completion | Exception, mode: str = "native") -> None:
        self.replies = list(replies)
        self.mode = mode
        self.calls: list[dict[str, Any]] = []
        self.json_preferred = False

    async def tool_mode(self, model: str | None) -> str:
        return "json" if self.json_preferred else self.mode

    async def prefer_json(self, model: str | None) -> None:
        self.json_preferred = True

    async def complete(self, messages, model, max_tokens, tools=None) -> Completion:
        self.calls.append({"messages": [dict(m) for m in messages], "tools": tools})
        reply = self.replies.pop(0) if self.replies else call("finish", summary="(script ended)")
        if isinstance(reply, Exception):
            raise reply
        return reply


def use_native(client: TestClient, *replies: Any, mode: str = "native") -> NativeModel:
    agents = client.app.state.agents
    model = NativeModel(*replies, mode=mode)
    agents.loop.model = model
    agents.loop.limits = dataclasses.replace(
        agents.loop.limits, model_retry_seconds=0, tools_wait_seconds=0
    )
    return model


# --- the pieces -------------------------------------------------------------------------------

TOOLS = [
    {
        "node": "vm-docker",
        "node_id": "00000000-0000-0000-0000-000000000001",
        "capability": "docker.logs",
        "description": "Logs of one container.",
        "input_schema": {
            "type": "object",
            "required": ["container"],
            "properties": {
                "container": {"type": "string", "maxLength": 128, "pattern": "^[a-z]+$"},
                "lines": {"type": "integer", "minimum": 1, "maximum": 500},
            },
        },
    },
    {
        "node": "vm-two",
        "node_id": "00000000-0000-0000-0000-000000000002",
        "capability": "docker.logs",
        "description": "Logs of one container.",
        "input_schema": {},
    },
]


def test_a_function_per_capability_with_node_and_finish() -> None:
    defs, index = prompts.native_tools(TOOLS)
    names = [d["function"]["name"] for d in defs]
    assert names == ["docker_logs", "finish"] and index == {"docker_logs": "docker.logs"}
    params = defs[0]["function"]["parameters"]
    assert params["properties"]["node"]["enum"] == ["vm-docker", "vm-two"]
    assert params["required"] == ["node", "container"]
    # patterns and lengths stay out of the grammar the server builds
    assert params["properties"]["container"] == {"type": "string"}
    assert params["properties"]["lines"] == {"type": "integer", "minimum": 1, "maximum": 500}
    finish = defs[1]["function"]["parameters"]
    assert finish["required"] == ["summary"]
    assert finish["properties"]["findings"]["items"]["properties"]["tag"]["enum"] == [
        "live",
        "doc",
        "web",
        "assumption",
    ]


def test_a_native_call_becomes_an_action() -> None:
    _, index = prompts.native_tools(TOOLS)
    raw = {
        "function": {
            "name": "docker_logs",
            "arguments": json.dumps({"node": "vm-docker", "container": "web", "reason": "why"}),
        }
    }
    a = prompts.parse_native(raw, index)
    assert isinstance(a, prompts.ToolAction)
    assert (a.node, a.capability, a.input, a.reason) == (
        "vm-docker",
        "docker.logs",
        {"container": "web"},
        "why",
    )
    f = prompts.parse_native(
        {
            "function": {
                "name": "finish",
                "arguments": '{"summary":"ok","findings":[{"claim":"c"}]}',
            }
        },
        index,
    )
    assert isinstance(f, prompts.Finish) and f.summary == "ok" and f.findings == [{"claim": "c"}]
    # a dict instead of a JSON string, and a model that writes the capability with its dot
    a = prompts.parse_native(
        {"function": {"name": "docker.logs", "arguments": {"node": "n"}}}, index
    )
    assert isinstance(a, prompts.ToolAction) and a.capability == "docker.logs"
    for bad in (
        {"function": {"name": "rm_rf", "arguments": "{}"}},
        {"function": {"name": "finish", "arguments": "{}"}},  # no summary
        {"function": {"name": "docker_logs", "arguments": "[1]"}},
        {"nothing": 1},
    ):
        with pytest.raises(prompts.ActionError):
            prompts.parse_native(bad, index)


def test_the_transcript_renders_as_tool_calls_or_as_text() -> None:
    call_ = {"id": "c1", "name": "docker_logs", "arguments": '{"node":"n"}'}
    items = [
        {"role": "assistant", "content": '{"action":"tool"}', "call": call_},
        {"role": "user", "content": "[E1] live …", "call_id": "c1"},
        {"role": "assistant", "content": "plain"},
        {"role": "user", "content": "nudge"},
    ]
    native = prompts.render_messages(items, native=True)
    assert native[0]["tool_calls"][0]["id"] == "c1" and native[0]["content"] == ""
    assert native[1] == {"role": "tool", "tool_call_id": "c1", "content": "[E1] live …"}
    assert native[2] == {"role": "assistant", "content": "plain"}
    plain = prompts.render_messages(items, native=False)
    assert [m["role"] for m in plain] == ["assistant", "user", "assistant", "user"]
    assert plain[0]["content"] == '{"action":"tool"}' and "tool_calls" not in plain[0]
    # tool messages are never merged away
    assert len(prompts.merge_roles(native)) == 4


# --- a run ------------------------------------------------------------------------------------


def test_a_run_with_native_tool_calls(client: TestClient, vm) -> None:  # noqa: F811
    _, node = vm
    model = use_native(
        client,
        call("docker_logs", node="vm-docker", container="web", lines=50, reason="it restarts"),
        call(
            "finish",
            summary="Out of memory.",
            findings=[{"claim": "killed for memory", "tag": "live", "evidence": ["E1"]}],
            next="raise the limit",
        ),
    )
    t = wait_for(client, start(client).json()["id"], "done")
    assert t["summary"] == "Out of memory." and t["tool_calls"] == 1
    assert [c["capability"] for c in node.capability_calls] == ["docker.logs"]
    assert t["diagnosis"]["findings"][0] == {
        "claim": "killed for memory",
        "tag": "live",
        "evidence": ["E1"],
        "confirm_with": "",
        "unquoted": True,  # a live finding without a quote keeps its tag, marked
    }
    # the model was offered functions (and must call one)
    first = model.calls[0]
    assert {d["function"]["name"] for d in first["tools"]} >= {"docker_logs", "finish"}
    assert "JSON object" not in first["messages"][0]["content"]
    assert "provided functions" in first["messages"][0]["content"]
    # the second ask shows the call and its answer the OpenAI way
    second = model.calls[1]["messages"]
    assert (
        second[2]["role"] == "assistant" and second[2]["tool_calls"][0]["id"] == "call_docker_logs"
    )
    assert second[3]["role"] == "tool" and second[3]["tool_call_id"] == "call_docker_logs"
    assert second[3]["content"].startswith("[E1] live · docker.logs on vm-docker")
    # stored as the JSON action of the prompt-based protocol, with the native call beside it
    steps = [s for s in t["steps"] if s["kind"] == "assistant"]
    stored = json.loads(steps[0]["data"]["content"])
    assert stored["action"] == "tool" and stored["capability"] == "docker.logs"
    assert steps[0]["data"]["native"]["name"] == "docker_logs"
    obs = [s for s in t["steps"] if s["kind"] == "observation"]
    assert obs[0]["data"]["tool_call_id"] == "call_docker_logs"


def test_a_blind_native_finish_is_answered_as_a_tool_result(client: TestClient, vm) -> None:  # noqa: F811
    model = use_native(
        client,
        call("finish", summary="Surely the disk."),
        call("system_info", node="vm-docker"),
        call(
            "finish",
            summary="Uptime fine.",
            findings=[{"claim": "up", "tag": "live", "evidence": ["E1"]}],
        ),
    )
    t = wait_for(client, start(client).json()["id"], "done")
    assert t["summary"] == "Uptime fine." and t["tool_calls"] == 1
    # the nudge went back as the answer to the finish call: a valid conversation
    msgs = model.calls[1]["messages"]
    assert msgs[-1]["role"] == "tool" and msgs[-1]["tool_call_id"] == "call_finish"
    assert "You finished without looking" in msgs[-1]["content"]


def test_a_bad_native_call_is_answered_and_the_model_tries_again(client: TestClient, vm) -> None:  # noqa: F811
    model = use_native(
        client,
        call("rm_rf", node="vm-docker"),
        call("system_info", node="nas"),  # no such node among two: listed
        call("system_info", node="vm-docker"),
        call("finish", summary="ok", findings=[{"claim": "x", "tag": "live", "evidence": ["E1"]}]),
    )
    t = wait_for(client, start(client).json()["id"], "done")
    assert t["tool_calls"] == 1
    first_error = model.calls[1]["messages"][-1]
    assert first_error["role"] == "tool" and "no function 'rm_rf'" in first_error["content"]


def calls(*pairs: tuple[str, dict[str, Any]]) -> Completion:
    """One reply that asks for several functions at once."""
    return Completion(
        text="",
        tokens=9,
        model="native-model",
        tool_calls=[
            {
                "id": f"call_{i}",
                "type": "function",
                "function": {"name": name, "arguments": json.dumps(args)},
            }
            for i, (name, args) in enumerate(pairs)
        ],
    )


def test_several_calls_in_one_reply_cost_one_model_turn(client: TestClient, vm) -> None:  # noqa: F811
    _, node = vm
    model = use_native(
        client,
        calls(
            ("docker_logs", {"node": "vm-docker", "container": "web", "reason": "restarts"}),
            ("system_info", {"node": "vm-docker", "reason": "uptime"}),
            ("docker_logs", {"node": "vm-docker", "container": "db"}),
        ),
        call(
            "finish",
            summary="Out of memory.",
            findings=[{"claim": "killed", "tag": "live", "evidence": ["E1", "E2"]}],
        ),
    )
    t = wait_for(client, start(client).json()["id"], "done")
    assert t["tool_calls"] == 3 and len(model.calls) == 2  # two model turns for three looks
    assert [c["capability"] for c in node.capability_calls] == [
        "docker.logs",
        "system.info",
        "docker.logs",
    ]
    # the model is shown every call with its own answer, in its order — a valid conversation
    roles = [m["role"] for m in model.calls[1]["messages"][2:]]
    assert roles == ["assistant", "tool", "assistant", "tool", "assistant", "tool"]
    ids = [
        m.get("tool_call_id") or m["tool_calls"][0]["id"] for m in model.calls[1]["messages"][2:]
    ]
    assert ids == ["call_0", "call_0", "call_1", "call_1", "call_2", "call_2"]
    assert t["diagnosis"]["findings"][0]["evidence"] == ["E1", "E2"]


def test_the_parallel_calls_are_limited_by_the_run_and_by_the_call_budget(
    client: TestClient,
    vm,  # noqa: F811
) -> None:
    _, node = vm
    agents = client.app.state.agents
    agents.loop.limits = dataclasses.replace(agents.loop.limits, max_tool_calls=2)
    use_native(
        client,
        calls(
            ("docker_logs", {"node": "vm-docker", "container": "a"}),
            ("docker_logs", {"node": "vm-docker", "container": "b"}),
            ("docker_logs", {"node": "vm-docker", "container": "c"}),
        ),
        call("finish", summary="Two looks were enough."),
    )
    t = wait_for(client, start(client).json()["id"], "done")
    assert t["tool_calls"] == 2 and len(node.capability_calls) == 2
    assert t["stop_reason"] == "max_tool_calls"


def test_the_prompt_asks_for_short_plans_and_together_calls() -> None:
    native = prompts.system_prompt_native(TOOLS)
    assert "at most 8 words" in native and "together in one reply" in native
    assert "at most 4 findings" in native and "compact" in native
    plain = prompts.system_prompt(TOOLS)
    assert "at most 8 words" in plain and "at most 4 findings" in plain
    defs, _ = prompts.native_tools(TOOLS)
    assert (
        "at most 8 words"
        in defs[0]["function"]["parameters"]["properties"]["reason"]["description"]
    )


# --- modes and the fallback -------------------------------------------------------------------


def test_a_model_that_answers_in_text_falls_back_to_json_in_auto(client: TestClient, vm) -> None:  # noqa: F811
    tool_json = json.dumps(
        {"action": "tool", "node": "vm-docker", "capability": "system.info", "input": {}}
    )
    model = use_native(
        client,
        say("I think the server is fine."),
        say("Yes, definitely fine."),
        say(tool_json),  # in JSON mode now
        say(json.dumps({"action": "finish", "summary": "done", "findings": []})),
        mode="auto",
    )
    t = wait_for(client, start(client).json()["id"], "done")
    assert t["summary"] == "done" and t["tool_calls"] == 1
    notes = [s["data"]["text"] for s in t["steps"] if s["kind"] == "note"]
    assert any("Switching to the prompt-based JSON protocol" in n for n in notes)
    assert model.json_preferred is True
    # the third ask is a JSON-protocol ask: no tools, the JSON system prompt
    assert (
        model.calls[2]["tools"] is None
        and "JSON object" in model.calls[2]["messages"][0]["content"]
    )
    assert model.calls[0]["tools"] is not None


def test_a_server_that_refuses_tools_switches_to_json_in_auto(client: TestClient, vm) -> None:  # noqa: F811
    model = use_native(
        client,
        NativeUnsupported("the model answered 400: tools param requires --jinja"),
        say(json.dumps({"action": "tool", "node": "vm-docker", "capability": "system.info"})),
        say(json.dumps({"action": "finish", "summary": "done", "findings": []})),
        mode="auto",
    )
    t = wait_for(client, start(client).json()["id"], "done")
    assert t["summary"] == "done" and model.calls[1]["tools"] is None


def test_forced_native_does_not_fall_back(client: TestClient, vm) -> None:  # noqa: F811
    use_native(client, NativeUnsupported("the model answered 400: no tools"), mode="native")
    t = wait_for(client, start(client).json()["id"], "failed")
    assert "no tools" in t["error"]
    use_native(client, say("talk"), say("talk"), say("talk"), mode="native")
    t = wait_for(client, start(client).json()["id"], "failed")
    assert t["stop_reason"] == "bad_format"


def test_json_mode_is_what_it_was(client: TestClient, vm) -> None:  # noqa: F811
    model = use_native(
        client,
        say(json.dumps({"action": "tool", "node": "vm-docker", "capability": "system.info"})),
        say(json.dumps({"action": "finish", "summary": "json", "findings": []})),
        mode="json",
    )
    t = wait_for(client, start(client).json()["id"], "done")
    assert t["summary"] == "json" and all(c["tools"] is None for c in model.calls)


# --- the gateway model ------------------------------------------------------------------------


class StubGateway:
    def __init__(self, *answers: Any) -> None:
        self.answers = list(answers)
        self.payloads: list[dict[str, Any]] = []
        self.min_contexts: list[int] = []
        self.loaded: int | None = None  # the context the stub model is "loaded" with

    async def route(self, model: str) -> Any:
        if self.loaded is None:
            raise GatewayError(503, "model_unavailable", "no route in this stub")
        return type(
            "R", (), {"candidates": [Candidate(uuid.uuid4(), "node-amd", 0, True, self.loaded)]}
        )()

    async def call(
        self,
        model: str,
        path: str,
        payload: dict[str, Any],
        min_context: int = 0,
        label: str = "",
    ) -> dict[str, Any]:
        self.min_contexts.append(min_context)
        self.payloads.append(json.loads(json.dumps(payload)))
        answer = self.answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer


def _model(gateway: StubGateway, default_mode: str = "auto"):
    from topcoat_hub.agent_service import GatewayModel

    settings = type(
        "S",
        (),
        {
            "agent_tool_mode": default_mode,
            "default_chat_model": "m",
            "agent_min_context": 16384,
            "agent_stream": False,
        },
    )()
    model = GatewayModel(gateway, None, settings)  # type: ignore[arg-type]

    async def modes() -> dict[str, str]:
        return model.test_modes

    model.test_modes = {}  # type: ignore[attr-defined]
    model._modes = modes  # type: ignore[method-assign]
    return model


NATIVE_ANSWER = {
    "choices": [
        {
            "message": {
                "content": None,
                "tool_calls": [
                    {"id": "c1", "function": {"name": "docker_logs", "arguments": '{"node":"a"}'}},
                    "junk",
                ],
            }
        }
    ],
    "usage": {"prompt_tokens": 10, "completion_tokens": 3},
}


def test_the_gateway_model_asks_for_a_tool_call_and_reads_it() -> None:
    gw = StubGateway(NATIVE_ANSWER)
    defs, _ = prompts.native_tools(TOOLS)
    c = asyncio.run(_model(gw).complete([{"role": "user", "content": "x"}], "big", 100, tools=defs))
    assert gw.payloads[0]["tool_choice"] == "required" and gw.payloads[0]["tools"] == defs
    assert "response_format" not in gw.payloads[0]
    assert (
        c.tool_calls and c.tool_calls[0]["id"] == "c1" and len(c.tool_calls) == 1 and c.tokens == 13
    )


def test_the_gateway_model_lets_the_server_reuse_its_prompt_cache() -> None:
    gw = StubGateway(NATIVE_ANSWER)
    defs, _ = prompts.native_tools(TOOLS)
    asyncio.run(_model(gw).complete([{"role": "user", "content": "x"}], "big", 100, tools=defs))
    assert gw.payloads[0]["cache_prompt"] is True


def test_tool_choice_required_is_dropped_once_the_server_refuses_it() -> None:
    refuse = GatewayError(502, "model_error", "the model answered 400: bad tool_choice")
    gw = StubGateway(refuse, NATIVE_ANSWER, NATIVE_ANSWER)
    defs, _ = prompts.native_tools(TOOLS)
    model = _model(gw)
    asyncio.run(model.complete([{"role": "user", "content": "x"}], "big", 100, tools=defs))
    assert "tool_choice" in gw.payloads[0] and "tool_choice" not in gw.payloads[1]
    asyncio.run(model.complete([{"role": "user", "content": "x"}], "big", 100, tools=defs))
    assert "tool_choice" not in gw.payloads[2]  # remembered


def test_a_server_that_refuses_tools_altogether_is_native_unsupported() -> None:
    refuse = GatewayError(502, "model_error", "the model answered 400: tools need --jinja")
    gw = StubGateway(refuse, refuse)
    defs, _ = prompts.native_tools(TOOLS)
    with pytest.raises(NativeUnsupported):
        asyncio.run(_model(gw).complete([{"role": "user", "content": "x"}], "big", 100, tools=defs))


def test_the_tool_mode_is_per_model_with_a_default_and_a_memory() -> None:
    model = _model(StubGateway(), "auto")
    model.test_modes = {"small": "json", "big": "native"}  # type: ignore[attr-defined]

    async def run() -> list[str]:
        out = [await model.tool_mode(m) for m in ("small", "big", "other")]
        await model.prefer_json("other")
        out.append(await model.tool_mode("other"))
        out.append(await model.tool_mode("big"))  # a forced mode is not overridden by the memory
        return out

    assert asyncio.run(run()) == ["json", "native", "auto", "json", "native"]


# --- the setting ------------------------------------------------------------------------------


def test_the_tool_mode_per_model_is_a_setting_only_an_admin_changes(client: TestClient) -> None:
    r = client.get("/api/agent/settings", headers=AUTH)
    assert r.status_code == 200
    assert r.json() == {
        "tool_modes": {},
        "default_tool_mode": "auto",
        "modes": ["auto", "native", "json"],
    }
    put = client.put(
        "/api/agent/settings",
        headers=AUTH,
        json={"tool_modes": {"qwen-7b": "json", "gpt-oss-20b": "native"}},
    )
    assert put.status_code == 200, put.text
    assert client.get("/api/agent/settings", headers=AUTH).json()["tool_modes"] == {
        "qwen-7b": "json",
        "gpt-oss-20b": "native",
    }
    bad = client.put("/api/agent/settings", headers=AUTH, json={"tool_modes": {"x": "telepathy"}})
    assert bad.status_code == 422 and bad.json()["detail"]["code"] == "invalid_tool_mode"
    # a normal user reads it but cannot change it
    _, who = make_user(TEST_DB_URL, "anna")
    assert client.get("/api/agent/settings", headers=who).status_code == 200
    assert (
        client.put("/api/agent/settings", headers=who, json={"tool_modes": {}}).status_code == 403
    )
    # removing the last entry
    assert (
        client.put("/api/agent/settings", headers=AUTH, json={"tool_modes": {}}).status_code == 200
    )
    assert client.get("/api/agent/settings", headers=AUTH).json()["tool_modes"] == {}


def test_the_native_finish_offers_the_result_table() -> None:
    """A model that answers with native calls can only send what the finish function names: the
    result table of a task (projects, PPH-04) must be among them, or it never arrives."""
    from topcoat_hub.agent_prompts import FINISH_FUNCTION, MAX_TABLE_ROWS, native_tools

    defs, _ = native_tools([])
    finish = next(d for d in defs if d["function"]["name"] == FINISH_FUNCTION)
    table = finish["function"]["parameters"]["properties"]["table"]
    assert table["type"] == "array" and table["maxItems"] == MAX_TABLE_ROWS


def test_development_gets_the_code_prompt_not_the_server_prompt() -> None:
    # live 2026-11-17: a code research of task 3.1.2 ran with "the diagnosis agent of a home-lab
    # operator" and its health.check rules; Development now reads code, with the shared protocol
    for build in (prompts.system_prompt_native, prompts.system_prompt):
        server = build(TOOLS)
        code = build(TOOLS, None, "dev")
        assert "home-lab operator" in server and "home-lab operator" not in code
        assert "health.check" not in code and "files.list and files.read" not in code
        assert "code agent of Topcoat" in code and "wiki.read" in code and "path:line" in code
        # the protocol stays: evidence marks, data fences, the language, the tool list
        for shared in ("E1, E2", "<<<DATA", "language of the task", "Tools (node:"):
            assert shared in code
        assert len(code) < len(server)
    assert prompts.system_prompt_native(TOOLS, None, "chat") == prompts.system_prompt_native(TOOLS)


def test_at_a_limit_only_finish_is_offered(client: TestClient, vm) -> None:  # noqa: F811
    # live 2026-11-18: gpt-oss, offered its tools at the stop, searched once more instead of
    # finishing, and the run ended without findings
    agents = client.app.state.agents
    agents.loop.limits = dataclasses.replace(agents.loop.limits, max_tool_calls=1)
    model = use_native(
        client,
        call("docker_logs", node="vm-docker", container="a"),
        call("finish", summary="One look was enough."),
    )
    t = wait_for(client, start(client).json()["id"], "done")
    assert t["stop_reason"] == "max_tool_calls" and "One look" in (t["summary"] or "")
    last = model.calls[-1]["tools"]
    assert [d["function"]["name"] for d in last] == ["finish"]


def test_projekte_gets_the_role_that_prepares_changes() -> None:
    # live 2026-11-18: with the server prompt ("You cannot change anything") gpt-oss answered "I
    # cannot create the change set myself" and researched the task's questions instead
    for build in (prompts.system_prompt_native, prompts.system_prompt):
        text = build(TOOLS, None, "project")
        assert "project agent of Topcoat" in text and "You cannot change anything" not in text
        assert "project.changeset.propose" in text and "the change IS the work" in text
        assert "Do not research the content of a task" in text and "health.check" not in text
        for shared in ("E1, E2", "<<<DATA", "language of the task", "Tools (node:"):
            assert shared in text
