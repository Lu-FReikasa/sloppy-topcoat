"""A short, in-memory history of what the admin did on the scheduling page (policy changes,
pauses, manual starts of background work). It is lost on restart; the hub log keeps the same
lines for longer."""

from collections import deque
from datetime import UTC, datetime
from typing import Any

MAX_EVENTS = 100


class SchedulingLog:
    def __init__(self, max_events: int = MAX_EVENTS) -> None:
        self._events: deque[dict[str, Any]] = deque(maxlen=max_events)

    def add(self, kind: str, actor: str, detail: str = "", ok: bool = True) -> None:
        self._events.append(
            {
                "ts": datetime.now(UTC).isoformat(),
                "kind": kind,
                "actor": actor,
                "detail": detail,
                "ok": ok,
            }
        )

    def recent(self, limit: int = 50) -> list[dict[str, Any]]:
        """Newest first."""
        return list(self._events)[::-1][:limit]
