"""The hub's own recent warnings and errors, kept in memory (2026-11-17).

An issue reported from the running system ("Problem: …") carries what the hub logged around that
time, so a later check has the error line and not only the person's words. The buffer holds the
last :data:`SIZE` records of level WARNING and above; nothing is written anywhere, a restart
empties it. Secret-looking values are masked before a line is kept."""

import logging
import re
import threading
from collections import deque
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta, tzinfo

SIZE = 400
MAX_LINE = 400
_SECRET = re.compile(
    r"(?i)(bearer\s+|token[=: ]+|password[=: ]+|api[_-]?key[=: ]+|secret[=: ]+)[^\s,;\"']+"
)


@dataclass(frozen=True)
class Line:
    at: datetime
    level: str
    logger: str
    text: str

    def render(self, tz: tzinfo | None = None) -> str:
        at = self.at.astimezone(tz) if tz is not None else self.at
        return f"{at:%H:%M:%S} {self.level} {self.logger}: {self.text}"


class RingHandler(logging.Handler):
    def __init__(self, size: int = SIZE) -> None:
        super().__init__(level=logging.WARNING)
        self._lines: deque[Line] = deque(maxlen=size)
        self._lock = threading.Lock()

    def emit(self, record: logging.LogRecord) -> None:
        try:
            text = record.getMessage()
            if record.exc_info and record.exc_info[1] is not None:
                text += f" ({type(record.exc_info[1]).__name__}: {record.exc_info[1]})"
        except Exception:  # a broken format string must not break logging
            text = str(record.msg)
        text = _SECRET.sub(r"\1***", " ".join(text.split()))[:MAX_LINE]
        line = Line(
            datetime.fromtimestamp(record.created, UTC), record.levelname, record.name, text
        )
        with self._lock:
            self._lines.append(line)

    def recent(self, minutes: int = 30, limit: int = 20) -> list[Line]:
        since = datetime.now(UTC) - timedelta(minutes=minutes)
        with self._lock:
            found = [x for x in self._lines if x.at >= since]
        return found[-limit:]


_handler = RingHandler()


def install() -> RingHandler:
    """Hang the buffer on the root logger once (``serve`` resets the root handlers before the app
    starts, so this runs in the app's start)."""
    root = logging.getLogger()
    if _handler not in root.handlers:
        root.addHandler(_handler)
    return _handler


def recent(minutes: int = 30, limit: int = 20) -> list[Line]:
    return _handler.recent(minutes, limit)
