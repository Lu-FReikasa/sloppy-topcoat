"""B-B: a check that asks the same thing again is stopped, long output says where to go on, and a
check that ran out of calls carries on once by itself."""

from typing import Any

import pytest
from fastapi.testclient import TestClient

from tests.conftest import AUTH
from tests.test_agent import finish, use
from tests.test_chat import new_conv
from tests.test_chat_report import world  # noqa: F401 - the fixture: a tool node and a model node
from tests.test_chat_tools import ask_with
from topcoat_hub import chat_policy
from topcoat_hub.agent_loop import call_key, normalise_shell, range_hint


@pytest.mark.parametrize(
    ("a", "b"),
    [
        ("grep -r data-look web/src | head -5", "grep -rn 'data-look' web/src/ | head -20"),
        ("cat web/src/look.ts", "cat  web/src/look.ts | head -n 80"),
        ("grep -rniE foo web 2>&1", "grep foo web"),
    ],
)
def test_shell_calls_that_ask_the_same_have_one_key(a: str, b: str) -> None:
    assert normalise_shell(a) == normalise_shell(b)
    assert call_key("n", "shell.read", {"command": a}) == call_key(
        "n", "shell.read", {"command": b, "timeout_seconds": 30}
    )


@pytest.mark.parametrize(
    ("a", "b"),
    [
        ("grep -r data-look web/src", "grep -r data-look web/css"),
        ("sed -n '1,50p' web/src/looks.css", "sed -n '51,100p' web/src/looks.css"),
        ("cat web/src/look.ts", "cat web/src/looks.css"),
    ],
)
def test_shell_calls_that_ask_something_else_keep_their_own_key(a: str, b: str) -> None:
    assert call_key("n", "shell.read", {"command": a}) != call_key(
        "n", "shell.read", {"command": b}
    )


def test_a_search_is_the_same_after_case_punctuation_and_word_order() -> None:
    from topcoat_hub.knowledge import SEARCH_CAPABILITY

    one = call_key("kb", SEARCH_CAPABILITY, {"query": "Looks CSS data-look?"})
    two = call_key("kb", SEARCH_CAPABILITY, {"query": "data look: looks css"})
    assert one == two
    assert one != call_key("kb", SEARCH_CAPABILITY, {"query": "looks css themes"})
    assert call_key("n", "docker.ps", {"all": True}) != call_key("n", "docker.ps", {})


def test_a_cut_output_says_which_lines_are_shown_and_where_to_go_on() -> None:
    full = "\n".join(f"line {i}" for i in range(1, 731))
    shown = "\n".join(f"line {i}" for i in range(1, 101))
    hint = range_hint(full, shown)
    assert "lines 1-100 of 730" in hint and "sed -n '101,200p'" in hint


def test_a_check_that_ran_out_of_calls_goes_on_once_by_itself(
    client: TestClient,
    world,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def look(lines: int) -> str:
        import json

        return json.dumps(
            {
                "action": "tool",
                "node": "vm-docker",
                "capability": "docker.logs",
                "input": {"container": "web", "lines": lines},
                "reason": "see why web restarts",
            }
        )

    # the limit of a check is stated by the chat policy now (policy_limits); one call is enough here
    async def one_call(_sessionmaker: Any, _effort: str) -> chat_policy.ChatPolicy:
        return chat_policy.ChatPolicy(max_tool_calls=1)

    monkeypatch.setattr(chat_policy, "load", one_call)
    findings: list[dict[str, Any]] = [
        {"claim": "web was killed for memory", "tag": "live", "evidence": ["E1"]}
    ]
    model = use(
        client,
        look(50),  # run 1: one call, then the limit
        finish("partial", findings, "look further"),
        look(60),  # the run goes on by itself
        finish("web ran out of memory.", findings, "raise the memory limit"),
    )
    conv = new_conv(client)
    status, events, _ = ask_with(client, conv, "Was ist mit web los?", "on")
    assert status == 200 and events[-1][0] == "done"
    assert len(model.calls) == 4  # not a third run, not a user message in between
    assert [e for e, _ in events].count("tools_start") == 1
    done = next(d for e, d in events if e == "tools_done")
    assert done["status"] == "done" and done["stop_reason"] is None and done["calls"] == 2
    stored = client.get(f"/api/conversations/{conv}", headers=AUTH).json()["messages"]
    assert [m["role"] for m in stored].count("user") == 1


def test_a_repeated_workshop_read_is_a_new_call_after_the_workshop_changed() -> None:
    from topcoat_hub.agent_loop import WORKSHOP_CHANGES, call_key

    assert "repo.write" in WORKSHOP_CHANGES and "repo.read" not in WORKSHOP_CHANGES
    a = call_key("dev", "repo.list", {"repo": "test"})
    assert a == call_key("dev", "repo.list", {"repo": "test"})
    assert a.startswith('["dev", "repo.')


def test_the_refusal_of_a_repeat_lists_what_was_already_asked() -> None:
    from topcoat_hub.agent_loop import _asked_before

    seen = {
        call_key("n", "web.search", {"query": q}): f"E{i}"
        for i, q in enumerate(["ls option a", "ls option b", "ls option c"])
    }
    seen[call_key("n", "shell.read", {"command": "ls"})] = "E9"
    text = _asked_before(seen, "n", "web.search")
    assert "Already asked" in text and "ls option a" in text
    assert _asked_before(seen, "n", "shell.read") == ""  # one call is no list


def test_a_call_cut_off_at_the_output_limit_is_replayed_with_valid_arguments() -> None:
    """A llama-server answers HTTP 500 to a history that holds half a JSON text as the arguments
    of a tool call; the run must not end as "model unavailable" because the model wrote too much."""
    from topcoat_hub.agent_loop import replayable_call

    ok = {"id": "c1", "name": "t", "arguments": '{"a": 1}'}
    assert replayable_call(ok) is ok
    cut = {"id": "c2", "name": "research_report_section", "arguments": '{"markdown":"### Gru'}
    assert replayable_call(cut) == {**cut, "arguments": "{}"}
    assert replayable_call({"id": "c3", "name": "t", "arguments": "[1]"})["arguments"] == "{}"
    assert replayable_call({"id": "c4", "name": "t", "arguments": {"a": 1}})["arguments"] == {
        "a": 1
    }


def test_the_prompt_budget_never_exceeds_the_slot_of_the_loaded_model() -> None:
    from dataclasses import replace

    from topcoat_hub.agent_loop import AgentLoop, Completion, Limits, _Run

    run = object.__new__(_Run)
    run.limits = replace(Limits(), context_chars=48000, native_output_tokens=6144)
    run.mode, run.tools_chars, run.task_id = "native", 12000, "t"
    done = Completion(text="", tokens=5, model="m", context_length=16384, node="n")
    AgentLoop._fit_to_context(run, done)
    assert run.limits.native_output_tokens == 2048
    used = run.limits.context_chars // 3 + 12000 // 3 + run.limits.native_output_tokens
    assert used < 16384 and run.limits.context_chars < 48000
    # a big slot changes nothing
    run.limits = replace(Limits(), context_chars=48000, native_output_tokens=6144)
    AgentLoop._fit_to_context(run, replace(done, context_length=131072))
    assert run.limits.context_chars == 48000 and run.limits.native_output_tokens == 6144


def test_the_budget_follows_the_token_count_the_model_reported() -> None:
    from dataclasses import replace

    from topcoat_hub.agent_loop import AgentLoop, Completion, Limits, _Run

    run = object.__new__(_Run)
    run.limits = replace(Limits(), context_chars=48000, native_output_tokens=6144)
    run.mode, run.tools_chars, run.task_id = "native", 20000, "t"
    run.sent_chars = 22000  # the model counted 10000 tokens for it: 2.2 characters per token
    done = Completion(
        text="", tokens=10100, model="m", context_length=16384, node="n", prompt_tokens=10000
    )
    AgentLoop._fit_to_context(run, done)
    assert round(run.chars_per_token, 1) == 2.2
    tokens = run.limits.context_chars / 2.2 + 20000 / 2.2 + run.limits.native_output_tokens
    assert tokens < 16384
