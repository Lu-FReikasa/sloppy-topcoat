"""The hooks of an agent run, as a program states them (PRG-00 R5).

The agent loop has fixed places where checks run. A block program switches them per run in the
``hooks`` of its ``agent`` block; a hook that is not named keeps what the application's frame gives.

``step_check``     a small model looks at every tool result (``step_check.py``); default off
``claims``         rules on the answer before it is delivered: a claim that tests passed needs a
                   test run of this task, a branch needs a repo result, an announced step needs
                   to be taken
``second_reader``  a second model reads the draft against the tool results (``PreAnswerCheck``)
``repeat_guard``   the same call this many times ends the run (1 to 20)
``stall_guard``    this many model turns without new evidence end the run (0 = off, to 50)
``max_calls``      at most this many tool calls (1 to 60), then the run writes what it has: a run
                   of one small question ends by itself instead of at the time limit

The tool ceiling, the rights of the person and the node whitelist are not hooks and cannot be
changed here."""

from dataclasses import replace
from typing import Any

BOOLEANS = ("step_check", "claims", "second_reader")
NUMBERS = {"repeat_guard": (1, 20), "stall_guard": (0, 50), "max_calls": (1, 60)}
HOOK_KEYS = frozenset({*BOOLEANS, *NUMBERS})


class HookError(ValueError):
    """The hooks are not valid."""


def check(hooks: Any, where: str) -> dict[str, Any]:
    """The hooks as written in a program, or an error naming where."""
    if hooks is None:
        return {}
    if not isinstance(hooks, dict) or set(hooks) - HOOK_KEYS:
        raise HookError(f"{where}: hooks has {', '.join(sorted(HOOK_KEYS))}")
    for key in BOOLEANS:
        if key in hooks and not isinstance(hooks[key], bool):
            raise HookError(f"{where}.{key}: true or false")
    for key, (low, high) in NUMBERS.items():
        value = hooks.get(key)
        if key in hooks and (
            isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high
        ):
            raise HookError(f"{where}.{key}: a whole number from {low} to {high}")
    return dict(hooks)


def apply(limits: Any, hooks: dict[str, Any] | None, step_check_model: str = "") -> Any:
    """``limits`` (an ``agent_loop.Limits``) with the hooks of a program on top."""
    if not hooks:
        return limits
    changes: dict[str, Any] = {}
    if "step_check" in hooks:
        changes["step_check_model"] = step_check_model if hooks["step_check"] else ""
    if "claims" in hooks:
        changes["check_claims"] = hooks["claims"]
    if "second_reader" in hooks:
        changes["second_reader"] = hooks["second_reader"]
    if "repeat_guard" in hooks:
        changes["max_repeats"] = hooks["repeat_guard"]
    if "stall_guard" in hooks:
        changes["max_stalled_turns"] = hooks["stall_guard"]
    if "max_calls" in hooks:  # never more than the application's own frame allows
        changes["max_tool_calls"] = min(hooks["max_calls"], limits.max_tool_calls)
    return replace(limits, **changes)
