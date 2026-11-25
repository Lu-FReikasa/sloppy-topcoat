"""Time in the hub (decision D4-4 of brief PPH-00-implementation-prep).

Every instant is stored as ``timestamptz`` and leaves the hub with an offset; a naive datetime is
refused at the boundary (:func:`aware`). The hub has one default time zone
(``TOPCOAT_HUB_TIMEZONE``, UTC unless the admin sets one); a person may choose their own
(``users.timezone``), empty = follow the hub. The code tells the two apart: :func:`hub_now` for what
belongs to the installation, :func:`user_now` for what belongs to a person ("today", "overdue",
quiet hours).

Rules that depend on the wall clock (repeat "every day at 09:00", reminders relative to a due date)
store the IANA zone with the rule, so a later change of the person's zone does not silently move
them.

Tests replace the clock with :func:`set_clock`.
"""

from collections.abc import Callable
from datetime import UTC, datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

DEFAULT_ZONE = "UTC"

_clock: Callable[[], datetime] | None = None


def set_clock(clock: Callable[[], datetime] | None) -> None:
    """Use ``clock`` as the source of 'now' (a test); None = the system clock."""
    global _clock
    _clock = clock


def hub_now() -> datetime:
    """The current instant, in UTC."""
    now = _clock() if _clock is not None else datetime.now(UTC)
    return aware(now).astimezone(UTC)


def valid_zone(name: str) -> bool:
    try:
        ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError, OSError):
        return False
    return bool(name)


def zone(name: str | None, fallback: str = DEFAULT_ZONE) -> ZoneInfo:
    """The zone called ``name``; the fallback (and then UTC) when it is empty or unknown."""
    for candidate in (name, fallback, DEFAULT_ZONE):
        if candidate and valid_zone(candidate):
            return ZoneInfo(candidate)
    return ZoneInfo(DEFAULT_ZONE)


def user_now(user_zone: str | None, hub_zone: str = DEFAULT_ZONE) -> datetime:
    """The current instant on the wall clock of a person (their zone, else the hub's)."""
    return hub_now().astimezone(zone(user_zone, hub_zone))


def aware(value: datetime) -> datetime:
    """``value`` itself, or an error when it carries no offset: an instant without a zone is
    not an instant."""
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("a time needs an offset (like +02:00 or Z)")
    return value


def parse(text: str) -> datetime:
    """An ISO 8601 time with an offset (``Z`` is accepted)."""
    try:
        return aware(datetime.fromisoformat(text.replace("Z", "+00:00")))
    except ValueError as exc:
        raise ValueError(
            "a time must be ISO 8601 with an offset, like 2026-10-29T17:00:00+01:00"
        ) from exc


def iso(value: datetime | None) -> str | None:
    return None if value is None else aware(value).isoformat()
