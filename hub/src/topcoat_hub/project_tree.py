"""The rules of a project's task tree (brief PPH-01-planning-app, 01a).

Pure functions, no database: the store calls them with the rows of one project before it writes, and
the tests run anywhere. A task's parent and its dependencies must belong to the same project, the
parent chain must not loop, and the dependencies must not loop.
"""

from collections.abc import Mapping


class TreeError(ValueError):
    """A change that would break a tree rule. The message names the rule, not the data."""


def check_parent(task_id: str, parent_id: str | None, parents: Mapping[str, str | None]) -> None:
    """``parents`` maps every task id of the project to its parent id, with the change already
    applied. Refuses a parent outside the project, a task below itself, and a loop."""
    if parent_id is None:
        return
    if parent_id not in parents:
        raise TreeError("parent is not in this project")
    current: str | None = parent_id
    for _ in range(len(parents) + 1):
        if current is None:
            return
        if current == task_id:
            raise TreeError("a task cannot sit below itself")
        current = parents.get(current)
    raise TreeError("the task tree is cyclic")


def check_blocking(node_id: str, blockers: list[str], graph: Mapping[str, set[str]]) -> None:
    """Blocking relations may cross projects, so the graph is the person's whole set of open
    ``blocked_by`` / ``precedes`` relations (node -> its blockers), without the change. Refuses a
    node that blocks itself and a loop: the node must not be reachable from one of its blockers."""
    if node_id in blockers:
        raise TreeError("a task cannot block itself")
    seen: set[str] = set()
    stack = list(blockers)
    while stack:
        current = stack.pop()
        if current == node_id:
            raise TreeError("blocking relations would be cyclic")
        if current in seen:
            continue
        seen.add(current)
        stack.extend(graph.get(current, ()))
