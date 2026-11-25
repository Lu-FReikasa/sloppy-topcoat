"""The activity of an answer (E7-01, D56): the log, the thinking trace and the splitting of a
``<think>`` block out of the text. No database: these are plain objects."""

import json

import pytest

from topcoat_hub import activity
from topcoat_hub.chat import SseReader, ThinkSplitter


def payload(chunk: str) -> dict:
    assert chunk.startswith("event: activity\ndata: ")
    return json.loads(chunk.split("data: ", 1)[1])


def test_an_item_is_added_changed_and_finished_with_a_chunk_each() -> None:
    log = activity.ActivityLog()
    item_id, chunk = log.add("tool", "docker.ps @ node-a", data={"input": {}})
    assert item_id == 1
    event = payload(chunk)
    assert event["op"] == "add" and event["item"]["status"] == "running"
    assert event["item"]["ms"] is None

    event = payload(log.update(item_id, detail="partial"))
    assert event["op"] == "update" and event["item"]["detail"] == "partial"

    event = payload(log.finish(item_id, "failed", "capability_disabled"))
    assert event["item"]["status"] == "failed" and isinstance(event["item"]["ms"], int)
    assert log.failed() == 1


def test_an_item_that_is_born_finished_has_no_running_time() -> None:
    log = activity.ActivityLog()
    _, chunk = log.add("note", status="info", detail="hello")
    assert payload(chunk)["item"]["ms"] == 0


def test_unknown_kinds_and_statuses_are_made_harmless() -> None:
    log = activity.ActivityLog()
    _, chunk = log.add("something-new", status="weird")
    item = payload(chunk)["item"]
    assert item["kind"] == "note" and item["status"] == "info"
    assert log.update(99, detail="x") == ""  # an id nobody has


def test_long_text_is_clipped_and_data_that_does_not_fit_is_not_kept() -> None:
    log = activity.ActivityLog()
    _, chunk = log.add(
        "thinking",
        "t" * 1000,
        detail="d" * (activity.MAX_DETAIL + 500),
        data={"blob": "x" * (activity.MAX_DATA_JSON + 1)},
    )
    item = payload(chunk)["item"]
    assert len(item["title"]) <= activity.MAX_TITLE
    assert len(item["detail"]) <= activity.MAX_DETAIL and item["detail"].startswith("…")
    assert item["data"] == {"truncated": True}


def test_the_log_is_bounded_and_counts_what_it_dropped() -> None:
    log = activity.ActivityLog()
    for _ in range(activity.MAX_ITEMS + 3):
        log.add("note", status="info")
    assert len(log) == activity.MAX_ITEMS and log.dropped == 3
    assert log.add("note")[0] == 0


def test_settle_marks_what_still_runs_as_stopped_and_snapshot_is_json() -> None:
    log = activity.ActivityLog()
    a, _ = log.add("tool", "x")
    b, _ = log.add("tool", "y")
    log.finish(b, "ok")
    log.settle()
    snap = log.snapshot()
    assert [i["status"] for i in snap] == ["stopped", "ok"]
    json.dumps(snap)  # storable
    snap[0]["status"] = "changed"
    assert log.snapshot()[0]["status"] == "stopped"  # a copy, not the log itself
    assert a == 1


def test_thinking_becomes_one_item_that_grows_and_is_closed_once() -> None:
    log = activity.ActivityLog()
    trace = activity.ThinkingTrace(log, interval=0.0)
    first = trace.feed("let me ")
    assert len(first) == 1 and payload(first[0])["op"] == "add"
    again = trace.feed("see")
    assert payload(again[0])["item"]["detail"] == "let me see"
    closing = trace.end()
    assert payload(closing[0])["item"]["status"] == "ok"
    assert trace.end() == []  # nothing open any more
    assert len(log) == 1


def test_thinking_updates_are_throttled() -> None:
    log = activity.ActivityLog()
    trace = activity.ThinkingTrace(log, interval=60.0)
    assert len(trace.feed("a")) == 1
    assert trace.feed("b") == [] and trace.feed("c") == []  # within the interval: no chunk
    assert payload(trace.end()[0])["item"]["detail"] == "abc"  # but nothing is lost


def test_the_reader_passes_reasoning_apart_from_the_answer() -> None:
    reader = SseReader()
    raw = (
        b'data: {"choices":[{"delta":{"reasoning_content":"hmm"}}]}\n\n'
        b'data: {"choices":[{"delta":{"content":"Hi"}}]}\n\n'
        b'data: {"choices":[{"delta":{"reasoning":"x"}}]}\n\n'
    )
    chunks = reader.feed(raw)
    assert [(c.reasoning, c.text) for c in chunks] == [("hmm", ""), ("", "Hi"), ("x", "")]


def split_all(pieces: list[str]) -> list[tuple[str, str]]:
    splitter = ThinkSplitter()
    out: list[tuple[str, str]] = []
    for p in pieces:
        out += splitter.feed(p)
    out += splitter.flush()
    return out


def joined(parts: list[tuple[str, str]], kind: str) -> str:
    return "".join(t for k, t in parts if k == kind)


def test_a_think_block_at_the_start_is_split_off() -> None:
    parts = split_all(["<think>I should check</think>The answer."])
    assert joined(parts, "think") == "I should check"
    assert joined(parts, "text") == "The answer."


@pytest.mark.parametrize("cut", range(1, 30))
def test_tags_cut_anywhere_by_the_stream_are_still_found(cut: int) -> None:
    whole = "\n <think>deep thought</think>\nDone."
    parts = split_all([whole[:cut], whole[cut:]])
    assert joined(parts, "think") == "deep thought"
    assert joined(parts, "text") == "\nDone."


def test_a_tag_in_the_middle_of_an_answer_is_just_text() -> None:
    parts = split_all(["Use the ", "<think>", " tag like this."])
    assert joined(parts, "think") == ""
    assert joined(parts, "text") == "Use the <think> tag like this."


def test_text_that_only_looks_like_a_tag_is_kept() -> None:
    parts = split_all(["<thinking", " about it"])
    assert joined(parts, "text") == "<thinking about it"


def test_a_block_that_never_closes_stays_thought() -> None:
    parts = split_all(["<think>cut off by the length limit"])
    assert joined(parts, "think") == "cut off by the length limit"
    assert joined(parts, "text") == ""


def test_blanks_alone_are_not_an_answer() -> None:
    assert joined(split_all(["  \n"]), "text") == ""
