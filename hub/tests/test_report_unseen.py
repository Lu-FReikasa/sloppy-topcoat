"""A check's card says what it did not look at (pruefauftrag T3.5) and ends with its record line
(T3.8). The card is built from plain stand-ins for the task and the evidence."""

import uuid
from types import SimpleNamespace

from topcoat_hub import report


def _task(summary: str, coverage: dict | None = None) -> SimpleNamespace:
    diagnosis = {"summary": summary, "findings": [], "coverage": coverage or {}}
    return SimpleNamespace(
        id=uuid.uuid4(), diagnosis=diagnosis, status="done", stop_reason=None, tool_calls=1
    )


def _evidence(ref: str, node: str) -> SimpleNamespace:
    return SimpleNamespace(
        ref=ref,
        tag="live",
        source={"node": node, "capability": "windows.events"},
        excerpt="ok",
        bytes=2,
    )


def test_a_machine_named_in_the_summary_without_evidence_becomes_an_assumption():
    card = report.build(
        _task(
            "node-amd had audio events; tools-host is fine.", {"asked": ["node-amd", "tools-host"]}
        ),
        [_evidence("E1", "node-amd")],
    )
    assert card is not None
    claims = [a["claim"] for a in card["assumptions"]]
    assert "tools-host was not asked" in claims
    assert "node-amd was not asked" not in claims
    assert card["summary_has_assumptions"] is True


def test_a_name_inside_a_longer_name_is_not_a_mention():
    assert report.unseen_mentions("tools-host-2 is fine", ["tools-host"], []) == []
    assert report.unseen_mentions("tools-host is fine", ["tools-host"], []) == ["tools-host"]


def test_the_card_text_ends_with_the_record_line_and_keeps_six_findings():
    task = _task("Summary.")
    task.diagnosis["findings"] = [
        {"claim": f"claim {i}", "tag": "live", "evidence": ["E1"]} for i in range(8)
    ]
    card = report.build(task, [_evidence("E1", "node-amd")])
    text = report.text(card)
    assert text.splitlines()[-1] == f"Akte: {task.id}"
    assert text.count("[live]") == 6


def test_topcoat_state_of_a_machine_counts_as_seen_for_that_machine():
    state = SimpleNamespace(
        ref="E1",
        tag="live",
        source={"node": "topcoat", "capability": "topcoat.state", "input": {"what": "node-amd"}},
        excerpt="online",
        bytes=6,
    )
    card = report.build(_task("node-amd is online."), [state])
    assert card is not None
    assert [a["claim"] for a in card["assumptions"]] == []
