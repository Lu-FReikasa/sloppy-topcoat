"""The result of an application is written by code from the stored evidence (message_result.py)."""

from datetime import UTC, datetime
from types import SimpleNamespace as NS

from topcoat_hub import message_result, messages


def row(ref: str, excerpt: str, url: str | None = "https://docs.example.org/page") -> NS:
    source = {"capability": "web.fetch", "node": "web", "at": "2026-10-29T09:00:00+00:00"}
    if url:
        source["url"] = url
    return NS(ref=ref, excerpt=excerpt, source=source)


def task() -> NS:
    return NS(
        step_count=7,
        tool_calls=5,
        model="m",
        created_at=datetime(2026, 10, 29, 9, tzinfo=UTC),
        started_at=datetime(2026, 10, 29, 9, 1, tzinfo=UTC),
        finished_at=None,
        diagnosis=None,
        stop_reason=None,
        error=None,
        summary=None,
    )


def test_a_quote_is_a_literal_passage_of_the_excerpt() -> None:
    long_line = "The server answers within 30 ms under load. " * 80
    findings, open_points = message_result.findings_of(
        [{"claim": "answers within 30 ms", "evidence": ["E1"]}], [row("E1", long_line)]
    )
    assert open_points == []
    quote = findings[0]["quote"]
    assert quote in long_line and not quote.endswith("…") and len(quote) <= 2000


def test_a_claim_without_a_row_or_an_excerpt_becomes_an_open_point() -> None:
    findings, open_points = message_result.findings_of(
        [
            {"claim": "No mark", "evidence": []},
            {"claim": "Unknown mark", "evidence": ["E5"]},
            {"claim": "Empty excerpt", "evidence": ["E1"]},
        ],
        [row("E1", "   ")],
    )
    assert findings == []
    assert len(open_points) == 3 and all(p.startswith("Not backed") for p in open_points)


def test_the_first_usable_mark_carries_the_claim_and_a_source_without_url_is_named() -> None:
    findings, _ = message_result.findings_of(
        [{"claim": "disk is full", "evidence": ["E1", "E2"]}],
        [row("E1", ""), row("E2", "disk is full at 98 %", url=None)],
    )
    assert findings[0]["source_url"] == "web.fetch@web#E2"
    assert "source_host" not in findings[0]


def test_the_stats_count_distinct_sources_and_hosts() -> None:
    findings = [
        {"source_url": "https://a.example.org/1", "source_host": "a.example.org"},
        {"source_url": "https://a.example.org/2", "source_host": "a.example.org"},
        {"source_url": "tool@node#E1"},
    ]
    stats = message_result.stats_of(task(), findings)
    assert (stats["sources"], stats["hosts"], stats["steps"], stats["tool_calls"]) == (3, 1, 7, 5)
    assert stats["queued_at"].startswith("2026-10-29T09:00")
    assert stats["finished_at"] is None


def test_a_payload_built_by_code_passes_the_registry() -> None:
    payload, findings = message_result.result_payload(
        summary="Short.",
        claims=[{"claim": "disk is full", "evidence": ["E1"]}],
        evidence=[row("E1", "disk is full at 98 %")],
        coverage={"disk": {"findings": [0, 4], "note": "n"}, "ghost": "nope"},
        must_cover=["disk", "ram"],
        as_of="2026-10-29",
    )
    messages.check_payload("research.result", payload)
    assert len(findings) == 1
    assert payload["coverage"] == {"disk": {"findings": [0], "note": "n"}}  # ram was not claimed


def test_a_failure_payload_maps_the_stop_reason() -> None:
    t = task()
    t.stop_reason, t.error = "no_tools", "no node"
    payload = message_result.failure_payload(t)
    messages.check_payload("research.failure", payload)
    assert payload["reason"] == "node_away" and payload["retryable"] is True
    t.stop_reason, t.error = "weird", None
    assert message_result.failure_payload(t)["reason"] == "other"
