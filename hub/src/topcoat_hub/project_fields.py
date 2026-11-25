"""The fields of a node beyond title and body (PPH-04, 04b), checked without a database.

``normalize`` turns what a caller sent (a REST body, a tool call) into the values the store keeps:
times with an offset (an ISO string is parsed, a naive time refused), whole numbers inside their
range, a repeat rule that :mod:`project_repeat` accepts. Which fields a node may have depends on its
kind: a task has all of them, a topic only a colour and the favourite flag, a report the date its
content describes and the application it came from.
"""

from collections.abc import Mapping
from datetime import datetime
from typing import Any

from topcoat_hub import clock, project_repeat

TASK_FIELDS = (
    "start_at", "due_at", "end_at", "priority", "percent_done", "color", "estimate_minutes",
    "repeat_rule", "is_milestone", "favorite",
)  # fmt: skip
TOPIC_FIELDS = ("color", "favorite")
REPORT_FIELDS = ("as_of", "origin_app", "color", "favorite")
ALLOWED = {"task": TASK_FIELDS, "topic": TOPIC_FIELDS, "report": REPORT_FIELDS}
TIMES = ("start_at", "due_at", "end_at", "as_of")
MAX_ESTIMATE = 60 * 24 * 366 * 5  # five years, in minutes


class FieldError(ValueError):
    """A value the field does not take."""


def _time(name: str, value: Any) -> datetime | None:
    if value is None:
        return None
    try:
        if isinstance(value, str):
            return clock.parse(value)
        if isinstance(value, datetime):
            return clock.aware(value)
    except ValueError as exc:
        raise FieldError(f"{name}: {exc}") from exc
    raise FieldError(f"{name} must be an ISO 8601 time with an offset")


def _int(name: str, value: Any, low: int, high: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
        raise FieldError(f"{name} must be a whole number from {low} to {high}")
    return value


def _flag(name: str, value: Any) -> bool:
    if not isinstance(value, bool):
        raise FieldError(f"{name} must be true or false")
    return value


def _color(value: Any) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not (
        (
            value.startswith("#")
            and 4 <= len(value) <= 9
            and all(c in "0123456789abcdefABCDEF" for c in value[1:])
        )
        or (value.isalpha() and value.islower() and 3 <= len(value) <= 20)
    ):
        raise FieldError("color must be #rgb, #rrggbb or a lower-case colour name")
    return value


def normalize(kind: str, fields: Mapping[str, Any]) -> dict[str, Any]:
    """The checked fields of a node of ``kind`` (only the ones that were given)."""
    allowed = ALLOWED.get(kind, ())
    out: dict[str, Any] = {}
    for name, value in fields.items():
        if name not in allowed:
            raise FieldError(f"a {kind} has no field {name}")
        if name in TIMES:
            out[name] = _time(name, value)
        elif name == "priority":
            out[name] = _int(name, value, 0, 5)
        elif name == "percent_done":
            out[name] = _int(name, value, 0, 100)
        elif name == "estimate_minutes":
            out[name] = None if value is None else _int(name, value, 0, MAX_ESTIMATE)
        elif name == "repeat_rule":
            try:
                out[name] = None if value is None else project_repeat.normalize(value)
            except project_repeat.RepeatError as exc:
                raise FieldError(str(exc)) from exc
        elif name in ("is_milestone", "favorite"):
            out[name] = _flag(name, value)
        elif name == "color":
            out[name] = _color(value)
        elif name == "origin_app":
            if value is not None and (not isinstance(value, str) or not 1 <= len(value) <= 40):
                raise FieldError("origin_app must be a short text")
            out[name] = value
    return out


def check_dates(
    start: datetime | None, due: datetime | None, end: datetime | None, *, milestone: bool
) -> None:
    """The rules that hold for the dates a task ends up with."""
    if start is not None and end is not None and start > end:
        raise FieldError("the start lies after the end")
    if milestone and due is None and end is None:
        raise FieldError("a milestone needs a due date or an end date")


def iso(value: datetime | None) -> str | None:
    return clock.iso(value)
