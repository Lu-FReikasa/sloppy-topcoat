"""What the live tests of the applications judge (scripts/dev/live_judge.py) is plain code."""

import importlib.util
import sys
from pathlib import Path
from types import ModuleType

DEV = Path(__file__).resolve().parents[2] / "scripts" / "dev"


def load(name: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, DEV / f"{name}.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.path.insert(0, str(DEV))
    spec.loader.exec_module(module)
    return module


judge = load("live_judge")
live_dev = load("live_dev")

GOOD_META = {
    "chapters": ["a", "b", "c"],
    "searches": 5,
    "pages": 3,
    "sources": 6,
    "hosts": ["x.org", "y.org"],
    "claims_total": 8,
    "claims_backed": 7,
}


def test_a_thorough_research_with_sources_and_backed_claims_passes() -> None:
    assert judge.judge_research({"status": "done"}, GOOD_META, "x" * 5000) == []


def test_a_research_that_read_nothing_or_stopped_on_a_limit_fails_with_reasons() -> None:
    meta = {**GOOD_META, "pages": 0, "sources": 1, "claims_backed": 1}
    problems = judge.judge_research(
        {"status": "done", "stop_reason": "max_tool_calls"}, meta, "short"
    )
    text = " | ".join(problems)
    assert "max_tool_calls" in text and "pages: 0" in text and "sources: 1" in text
    assert "chars" in text and "only 1 of 8 claims" in text


def test_a_research_task_that_did_not_finish_fails() -> None:
    assert judge.judge_research({"status": "failed"}, GOOD_META, "x" * 5000)


ANSWER = {
    "status": "complete",
    "content": "Python. VERSION: 0.2.1. The newest commit updates the handover.",
    "model": "Qwen3-Q4_K_M",
    "node": "node-nvidia",
}
TASK = {"status": "done", "stop_reason": None, "tool_calls": 5}


def test_a_dev_answer_from_the_repository_passes() -> None:
    assert judge.judge_dev(ANSWER, TASK, "Qwen3-Q4_K_M", ["node-nvidia"]) == []
    assert live_dev.judge(ANSWER, TASK, "Qwen3-Q4_K_M", ["node-nvidia"]) == []


def test_a_dev_check_the_progress_guard_ended_passes_but_a_repeat_does_not() -> None:
    stalled = {**TASK, "stop_reason": "no_progress"}
    assert judge.judge_dev(ANSWER, stalled, "Qwen3-Q4_K_M", ["node-nvidia"]) == []
    repeated = {**TASK, "stop_reason": "repeated_calls"}
    assert judge.judge_dev(ANSWER, repeated, "Qwen3-Q4_K_M", ["node-nvidia"])


def test_a_dev_answer_that_hit_a_limit_fails_even_when_it_says_complete() -> None:
    cut = {
        **ANSWER,
        "content": "Suche früh beendet: maximale Anzahl an Werkzeugaufrufen erreicht. 0.2.1",
    }
    problems = judge.judge_dev(
        cut, {**TASK, "stop_reason": "max_tool_calls"}, "Qwen3-Q4_K_M", ["node-nvidia"]
    )
    assert any("cut off by a limit" in p for p in problems)
    assert any("max_tool_calls" in p for p in problems)


def test_a_dev_answer_without_tools_or_version_fails() -> None:
    answer = {**ANSWER, "content": "I think it is a Python project."}
    problems = judge.judge_dev(answer, {**TASK, "tool_calls": 0}, "Qwen3-Q4_K_M", ["node-nvidia"])
    assert any("no version number" in p for p in problems)
    assert any("no tool was called" in p for p in problems)
    assert any(
        "no agent task" in p for p in judge.judge_dev(ANSWER, None, "Qwen3-Q4_K_M", ["node-nvidia"])
    )


def test_a_dev_answer_from_another_model_or_node_fails() -> None:
    problems = judge.judge_dev(
        {**ANSWER, "model": "other", "node": "node-amd"}, TASK, "Qwen3-Q4_K_M", ["node-nvidia"]
    )
    assert len(problems) == 2


def test_a_block_run_must_file_a_real_draft_and_leave_a_verdict() -> None:
    view = {"status": "ok", "filed": ["r1"], "variables": {"draft": "x" * 3000, "v": {"ok": True}}}
    assert judge.judge_blocks(view) == []
    bad = {"status": "failed", "error": "node gone", "filed": [], "variables": {"draft": "short"}}
    text = " | ".join(judge.judge_blocks(bad))
    assert (
        "node gone" in text
        and "nothing was filed" in text
        and "2000" in text
        and "no verdict" in text
    )
