"""The three effort stages of a chat answer, as the user names them: Schnell, Normal, Gründlich.

Plan: the plan (2d), brief R1-01-effort-stages. This module is the one
table of what each stage means. It is **not wired yet**: the chat still reads the policy of
its flow (``chat_policy.py``) and the limits of ``quality.py``. The tests pin this table to
those limits, so the two cannot drift apart before R2 wires it in.

The internal names are the levels of ``quality.py``: ``fast``, ``balanced``, ``thorough``.
"""

from dataclasses import dataclass, replace
from typing import Any, Literal

from topcoat_hub import quality

# When the second look runs: not at all, after the answer was shown (asynchronous, with the
# traffic light), or before the answer is shown (the agent revises on its findings).
SecondLook = Literal["off", "after", "before"]


@dataclass(frozen=True)
class Stage:
    level: str
    label: str
    second_look: SecondLook
    second_look_rounds: int
    think: bool
    max_turns: int
    max_tool_calls: int
    time_budget_seconds: float


STAGES: dict[str, Stage] = {
    "fast": Stage(
        level="fast",
        label="Schnell",
        second_look="off",
        second_look_rounds=0,
        think=False,
        max_turns=quality.FAST_LIMITS.max_turns,
        max_tool_calls=quality.FAST_LIMITS.max_tool_calls,
        time_budget_seconds=quality.FAST_LIMITS.time_budget_seconds,
    ),
    "balanced": Stage(
        level="balanced",
        label="Normal",
        second_look="after",
        second_look_rounds=0,
        think=False,
        max_turns=quality.CHECK_LIMITS.max_turns,
        max_tool_calls=quality.CHECK_LIMITS.max_tool_calls,
        time_budget_seconds=quality.CHECK_LIMITS.time_budget_seconds,
    ),
    "thorough": Stage(
        level="thorough",
        label="Gründlich",
        second_look="before",
        second_look_rounds=2,
        think=True,
        max_turns=quality.THOROUGH_LIMITS.max_turns,
        max_tool_calls=quality.THOROUGH_LIMITS.max_tool_calls,
        time_budget_seconds=quality.THOROUGH_LIMITS.time_budget_seconds,
    ),
}


def stage_for(value: object, settings: Any = None) -> Stage:
    """The stage of a stored level; anything unknown is ``balanced``, as in ``quality.level_of``.
    With ``settings`` the live revisions of Gründlich (``effort_thorough_revisions``) win."""
    stage = STAGES[quality.level_of(value)]
    if settings is not None and stage.level == "thorough":
        stage = replace(stage, second_look_rounds=int(settings.effort_thorough_revisions))
    return stage
