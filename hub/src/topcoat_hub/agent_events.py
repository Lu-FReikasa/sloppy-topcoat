"""What the stream says about a task (``agent_task``): enough for the browser to refresh its pages
and for the desktop client's herald (the desktop plan, 5b) to tell the person that a task they
started is done, without a second request: its title, where it came from, its application, and how
long it ran. Only the task's owner receives it (``_user``)."""

from datetime import datetime
from typing import Any

from topcoat_hub.models import AgentTask


def _seconds(start: datetime | None, end: datetime | None) -> int | None:
    if start is None or end is None:
        return None
    return max(0, int((end - start).total_seconds()))


def task_event(task: AgentTask) -> dict[str, Any]:
    return {
        "id": str(task.id),
        "status": task.status,
        "step_count": task.step_count,
        "title": (task.title or "")[:120],
        "origin": task.origin,
        "kind": task.kind,
        "parent_id": str(task.parent_id) if task.parent_id else None,
        "application": task.application,
        "seconds": _seconds(task.started_at, task.finished_at),
        "_user": str(task.user_id),
    }
