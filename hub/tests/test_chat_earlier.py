"""A follow-up sees what the conversation's earlier check read (Phase 1.3, 2026-11-11)."""

from topcoat_hub.chat_tools import CheckResult, earlier_block, earlier_for_task
from topcoat_hub.models import AgentEvidence, AgentTask


def _result() -> CheckResult:
    task = AgentTask(prompt="p", title="t", diagnosis={"summary": "Themes sind Daten plus CSS."})
    ev = [
        AgentEvidence(
            ref="E1",
            tag="live",
            source={"capability": "repo.read", "node": "dev", "input": {"path": "docs/THEMES.md"}},
            excerpt=(
                "docs/THEMES.md: lines 1-67 of 67 (end of file)\n# Themes\n"
                "A theme is data plus CSS: layout {kind}, base, css."
            ),
        ),
        AgentEvidence(
            ref="E2",
            tag="live",
            source={
                "capability": "repo.search",
                "node": "dev",
                "input": {"query": "BUILTIN_THEMES"},
            },
            excerpt="web/src/desktop/theme.ts:120: export const BUILTIN_THEMES = [",
        ),
    ]
    return CheckResult(task, ev)


def test_a_follow_up_gets_the_earlier_evidence_with_its_marks_and_summary() -> None:
    block = earlier_block(_result(), 4000)
    assert block.startswith("EARLIER RESULTS")
    assert "[E1] repo.read on dev" in block and "layout {kind}, base, css" in block
    assert "Themes sind Daten plus CSS." in block


def test_nothing_without_evidence_or_room() -> None:
    assert earlier_block(None, 4000) == ""
    assert earlier_block(CheckResult(None, []), 4000) == ""
    assert earlier_block(_result(), 300) == ""


def test_a_new_check_is_told_which_sources_were_read() -> None:
    text = earlier_for_task(_result())
    assert "already read these sources" in text
    assert 'repo.read on dev {"path": "docs/THEMES.md"}' in text
    assert "BUILTIN_THEMES" in text and len(text) < 3000
