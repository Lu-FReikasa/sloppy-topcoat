"""The figures of the model switches (P2a): loads and switches as the nodes served them."""

from datetime import UTC, datetime

from topcoat_hub.model_switches import review_load_share, summarize


def job(minute: int, node: str, model: str, *, loaded: bool, work: str = "chat", **over):
    base = {
        "created_at": datetime(2026, 11, 6, 9, minute, tzinfo=UTC),
        "chosen_node": node,
        "chosen_model": model,
        "model_loaded": loaded,
        "work": work,
        "reason": "",
        "waited_ms": 0,
    }
    return {**base, **over}


def test_a_load_is_a_request_that_found_its_model_unloaded():
    out = summarize([job(0, "node-a", "big", loaded=False), job(1, "node-a", "big", loaded=True)])
    assert out["totals"]["requests"] == 2
    assert out["totals"]["loads"] == 1


def test_a_switch_is_a_change_of_model_on_the_same_node_only():
    out = summarize(
        [
            job(0, "node-a", "big", loaded=True),
            job(1, "node-b", "small", loaded=True),  # another node: not a switch on node-a
            job(2, "node-a", "small", loaded=False),  # node-a changes from big to small
            job(3, "node-a", "small", loaded=True),
        ]
    )
    assert out["totals"]["switches"] == 1


def test_loads_caused_by_the_second_look_are_counted_apart():
    out = summarize(
        [
            job(0, "node-a", "big", loaded=True),
            job(1, "node-a", "other", loaded=False, work="review", waited_ms=4000),
            job(2, "node-a", "big", loaded=False, work="chat", waited_ms=1000),
        ]
    )
    totals = out["totals"]
    assert totals["review_loads"] == 1
    assert totals["loads_by_work"] == {"review": 1, "chat": 1}
    assert totals["waited_seconds_on_loads"] == 5.0
    assert review_load_share(totals) == 0.5


def test_rows_without_node_or_model_are_refused_requests_and_say_nothing():
    out = summarize([job(0, "", "big", loaded=False), job(1, "node-a", "", loaded=False)])
    assert out["totals"]["requests"] == 0
    assert out["cells"] == []


def test_cells_split_by_day_and_node_and_the_reason_of_each_load_is_kept():
    late = job(5, "node-a", "big", loaded=False, reason="fallback")
    late["created_at"] = datetime(2026, 11, 7, 1, 0, tzinfo=UTC)
    out = summarize(
        [job(0, "node-a", "big", loaded=False), job(1, "node-b", "big", loaded=True), late]
    )
    cells = {(c["date"], c["node"]): c for c in out["cells"]}
    assert cells[("2026-11-06", "node-a")]["loads"] == 1
    assert cells[("2026-11-06", "node-b")]["loads"] == 0
    assert cells[("2026-11-07", "node-a")]["loads"] == 1
    assert out["totals"]["loads_by_reason"] == {"-": 1, "fallback": 1}


def test_no_loads_means_no_share():
    assert review_load_share(summarize([job(0, "node-a", "big", loaded=True)])["totals"]) is None
