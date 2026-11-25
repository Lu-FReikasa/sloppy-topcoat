"""The second node's note on a tool step reaches the chat as one activity line (PLAN 7)."""

from types import SimpleNamespace

from topcoat_hub.activity import ActivityLog, CheckTrace
from topcoat_hub.chat_tools import step_events


def step(kind: str, data: dict) -> SimpleNamespace:
    return SimpleNamespace(seq=7, kind=kind, data=data)


def test_a_step_check_step_becomes_a_step_event() -> None:
    events = step_events(step("step_check", {"tool": "repo.read", "note": "the file is cut"}))
    assert events == [
        {"seq": 7, "kind": "step_check", "tool": "repo.read", "note": "the file is cut"}
    ]


def test_a_long_note_is_cut_for_the_chat() -> None:
    events = step_events(step("step_check", {"tool": "t", "note": "x" * 900}))
    assert len(events[0]["note"]) == 300


def test_the_trace_makes_one_info_line_of_it() -> None:
    log = ActivityLog()
    trace = CheckTrace(log)
    chunks = trace.event(
        "tool_step",
        {"kind": "step_check", "tool": "repo.read", "note": "the file is cut", "seq": 7},
    )
    assert len(chunks) == 1
    [item] = log.snapshot()
    assert item["kind"] == "step_check" and item["status"] == "info"
    assert item["title"] == "repo.read" and item["detail"] == "the file is cut"
