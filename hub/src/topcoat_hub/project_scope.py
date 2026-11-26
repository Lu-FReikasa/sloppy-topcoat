"""What the project agent may do in one project: the person's grants, per target and action.

A grant has one of two levels. ``free``: the agent does it on its own (for example, it asks
Research for a report). ``confirm``: the agent proposes it and the person confirms each time. An
action that has no grant is not offered to the agent at all.

Some actions are never grantable, whatever the person sets: changing existing files or texts
outside Projekte, pushing, merging, applying an update, deploying, and deleting. For those the
agent may only propose, and Administration or Development applies them after a click by the
person (brief PPH-01-planning-app, "Decided: writes").

The grant is the person's setting; the project agent never sets or changes it (no tool does).
"""

from typing import Any

LEVELS = ("free", "confirm")

# target application -> the actions that can be granted to it from a project
TARGETS: dict[str, tuple[str, ...]] = {
    "research": ("request_report",),
    "dev": ("request_report", "create_branch", "create_prototype"),
    "admin": ("read_released",),  # data from Administration, released per request
    # the project itself: create and change the sub-tasks of a large task (D109). It never deletes
    # a node: a dropped sub-task is closed ("delete" stays in NEVER)
    "project": ("decompose",),
}

# never grantable: the agent cannot be allowed to do these inside a project
NEVER = ("push", "merge", "apply_update", "deploy", "delete")


class ScopeError(ValueError):
    """A scope that names an unknown target or action, a level that does not exist, or an action
    that is never grantable."""


def normalize(scope: Any) -> dict[str, dict[str, str]]:
    """The scope as it is stored: ``{target: {action: level}}``. Empty targets are dropped."""
    if not isinstance(scope, dict):
        raise ScopeError("scope must be an object")
    out: dict[str, dict[str, str]] = {}
    for target, actions in scope.items():
        if target not in TARGETS:
            raise ScopeError(f"unknown target: {target}")
        if not isinstance(actions, dict):
            raise ScopeError(f"the grants of {target} must be an object")
        granted: dict[str, str] = {}
        for action, level in actions.items():
            if action in NEVER:
                raise ScopeError(f"{action} cannot be granted")
            if action not in TARGETS[target]:
                raise ScopeError(f"unknown action for {target}: {action}")
            if level not in LEVELS:
                raise ScopeError(f"level must be one of {', '.join(LEVELS)}")
            granted[action] = level
        if granted:
            out[target] = granted
    return out


def allowed(scope: dict[str, Any] | None, target: str, action: str) -> str | None:
    """``free``, ``confirm``, or ``None`` when the agent may not do this action in the project."""
    if action in NEVER:
        return None
    level = ((scope or {}).get(target) or {}).get(action)
    return level if level in LEVELS else None
