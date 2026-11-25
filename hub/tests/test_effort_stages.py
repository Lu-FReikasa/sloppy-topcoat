"""The effort stages table (brief R1-01-effort-stages), pinned to quality.py's limits."""

from topcoat_hub import quality
from topcoat_hub.effort_stages import STAGES, stage_for


def test_three_stages_in_the_order_of_the_levels():
    assert list(STAGES) == list(quality.LEVELS)


def test_limits_come_from_the_quality_tables():
    tables = {
        "fast": quality.FAST_LIMITS,
        "balanced": quality.CHECK_LIMITS,
        "thorough": quality.THOROUGH_LIMITS,
    }
    for level, limits in tables.items():
        stage = STAGES[level]
        assert stage.max_turns == limits.max_turns, level
        assert stage.max_tool_calls == limits.max_tool_calls, level
        assert stage.time_budget_seconds == limits.time_budget_seconds, level


def test_labels_are_the_user_words():
    assert [STAGES[level].label for level in ("fast", "balanced", "thorough")] == [
        "Schnell",
        "Normal",
        "Gründlich",
    ]


def test_only_thorough_checks_before_the_answer():
    before = [level for level, s in STAGES.items() if s.second_look == "before"]
    assert before == ["thorough"]


def test_only_balanced_checks_after_the_answer():
    after = [level for level, s in STAGES.items() if s.second_look == "after"]
    assert after == ["balanced"]


def test_fast_has_no_second_look():
    assert STAGES["fast"].second_look == "off"


def test_only_thorough_allows_revision_rounds():
    rounds = {level: s.second_look_rounds for level, s in STAGES.items()}
    assert rounds == {"fast": 0, "balanced": 0, "thorough": 2}


def test_only_thorough_thinks():
    thinking = [level for level, s in STAGES.items() if s.think]
    assert thinking == ["thorough"]


def test_unknown_level_is_balanced():
    assert stage_for("schnell") is STAGES["balanced"]
    assert stage_for(None) is STAGES["balanced"]
    assert stage_for("thorough") is STAGES["thorough"]
