"""A finish cut off at the output limit (first hardware test of E3-01d/c with gemma-4 on node-amd):
the hub told the model "not a valid action" three times and the task died as bad_format with the
evidence in hand. Now: the summary is rescued, a too-long answer is called too long, the limits are
larger, and a model loaded with too little context is reported."""

import json
from typing import Any

import pytest
from fastapi.testclient import TestClient

from tests.test_agent import start, vm, wait_for  # noqa: F401  (vm is a fixture)
from tests.test_agent_native import call, say, use_native
from topcoat_hub import agent_prompts as prompts
from topcoat_hub.agent_loop import Completion, Limits

SUMMARY = "Der Container web ist zu 86,2 % belegt, der Speicher ist knapp."


def cut(text: str = "", name: str | None = None, args: str = "") -> Completion:
    """A reply that hit max_tokens: JSON text, or the arguments of a native call, cut off."""
    calls = (
        [{"id": "call_cut", "type": "function", "function": {"name": name, "arguments": args}}]
        if name
        else None
    )
    return Completion(
        text=text, tokens=2048, model="native-model", tool_calls=calls, finish_reason="length"
    )


FINISH_ARGS = json.dumps({"summary": SUMMARY, "findings": [{"claim": "x" * 400}], "next": "y"})


def test_limits_leave_room_for_a_finish() -> None:
    lim = Limits()
    assert lim.max_output_tokens >= 1024 and lim.native_output_tokens >= 1024


@pytest.mark.parametrize("cut_at", ["findings", "claim", "next"])
def test_a_cut_finish_with_a_complete_summary_is_rescued(cut_at: str) -> None:
    at = FINISH_ARGS.index({"findings": '"findings"', "claim": '"claim"', "next": '"next"'}[cut_at])
    finish = prompts.rescue_finish(FINISH_ARGS[: at + 12], "finish")
    assert finish is not None and finish.summary == SUMMARY and finish.findings == []
    assert finish.notes == [prompts.CUT_NOTE]
    text = '{"action": "finish", "summary": ' + json.dumps(SUMMARY, ensure_ascii=False) + ', "findi'
    assert prompts.rescue_finish(text).summary == SUMMARY  # type: ignore[union-attr]


def test_nothing_is_rescued_when_the_summary_itself_is_cut_or_it_was_no_finish() -> None:
    assert (
        prompts.rescue_finish('{"summary": "Der Container web ist zu 86,2 % bel', "finish") is None
    )
    assert prompts.rescue_finish('{"summary": ""}', "finish") is None
    assert prompts.rescue_finish(FINISH_ARGS, "docker_logs") is None
    assert prompts.rescue_finish('{"action": "tool", "summary": "x"}') is None
    assert prompts.rescue_finish("no json at all", "finish") is None


def test_a_cut_native_finish_becomes_a_diagnosis_without_findings(client: TestClient, vm) -> None:  # noqa: F811
    use_native(
        client,
        call("system_info", node="vm-docker"),
        cut(name="finish", args=FINISH_ARGS[: FINISH_ARGS.index('"next"') - 3]),
    )
    task = wait_for(client, start(client).json()["id"], "done")
    assert task["summary"] == SUMMARY and task["stop_reason"] is None
    d = task["diagnosis"]
    assert d["findings"] == [] and prompts.CUT_NOTE in d["notes"]
    assert [e["ref"] for e in task["evidence"]] == ["E1"]  # what was seen stays with the task


def test_a_cut_json_finish_in_text_is_rescued_too(client: TestClient, vm) -> None:  # noqa: F811
    text = (
        '{"action": "finish", "summary": '
        + json.dumps(SUMMARY, ensure_ascii=False)
        + ', "findings": [{"claim": "Der Containe'
    )
    use_native(
        client,
        say(json.dumps({"action": "tool", "node": "vm-docker", "capability": "system.info"})),
        cut(text=text),
        mode="json",
    )
    task = wait_for(client, start(client).json()["id"], "done")
    assert task["summary"] == SUMMARY and prompts.CUT_NOTE in task["diagnosis"]["notes"]


def test_a_too_long_answer_is_told_so_not_that_the_action_is_invalid(
    client: TestClient,
    vm,  # noqa: F811
) -> None:
    model = use_native(
        client,
        call("system_info", node="vm-docker"),
        cut(name="finish", args='{"summary": "Der Container web ist zu 86,2 % bel'),
        call("finish", summary="Kurz: web ist knapp.", findings=[]),
    )
    task = wait_for(client, start(client).json()["id"], "done")
    assert task["summary"] == "Kurz: web ist knapp."
    told = [
        m["content"]
        for call_ in model.calls
        for m in call_["messages"]
        if m["role"] in ("user", "tool") and "cut off" in str(m["content"])
    ]
    assert told and all("too long" in t and "not a valid action" not in t for t in told)


def test_answers_that_keep_being_cut_off_say_so_in_the_error(client: TestClient, vm) -> None:  # noqa: F811
    use_native(
        client,
        call("system_info", node="vm-docker"),
        cut(name="finish", args='{"summary": "Der Contai'),
        cut(name="finish", args='{"summary": "Der Contai'),
        cut(name="finish", args='{"summary": "Der Contai'),
    )
    task = wait_for(client, start(client).json()["id"], "failed")
    assert task["stop_reason"] == "bad_format"
    assert "cut off" in task["error"] and "no valid action" not in task["error"]
    assert [e["ref"] for e in task["evidence"]] == ["E1"]


def test_a_model_loaded_with_too_little_context_is_reported_once(client: TestClient, vm) -> None:  # noqa: F811
    low = Completion(
        text="",
        tokens=5,
        model="native-model",
        tool_calls=call("system_info", node="vm-docker").tool_calls,
        context_length=8192,
        node="node-amd",
    )
    low2 = Completion(text="", tokens=5, model="native-model", context_length=8192, node="node-amd",
                      tool_calls=call("finish", summary="ok", findings=[]).tool_calls)  # fmt: skip
    use_native(client, low, low2)
    task = wait_for(client, start(client).json()["id"], "done")
    notes = [s["data"]["text"] for s in task["steps"] if s["kind"] == "note"]
    hits: list[Any] = [n for n in notes if "8192 tokens" in n]
    assert len(hits) == 1 and "node-amd" in hits[0] and "16384" in hits[0]
    assert any("8192 tokens" in n for n in task["diagnosis"]["notes"])


def test_enough_context_gives_no_note(client: TestClient, vm) -> None:  # noqa: F811
    ok = Completion(text="", tokens=5, model="m", context_length=32768, node="node-amd",
                    tool_calls=call("finish", summary="ok", findings=[]).tool_calls)  # fmt: skip
    use_native(client, ok)
    task = wait_for(client, start(client).json()["id"], "done")
    assert not any("context of" in s["data"].get("text", "") for s in task["steps"])
