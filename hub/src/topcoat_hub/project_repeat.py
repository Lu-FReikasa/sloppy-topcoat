"""Repeating tasks (PPH-04, 04b; used by 04h): the arithmetic only, no database.

A repeat rule is ``{every, unit, mode}``:

* ``unit`` is ``minute``, ``hour``, ``day``, ``week``, ``month`` or ``year``; ``every`` is 1..1000.
* ``mode`` ``after``: when the task is done, its dates move forward by **whole intervals** until the
  due date lies after now (a task that was left for months does not come back as ten overdue
  copies). ``monthly``: the same with calendar months, anchored to the original day of the month, so
  the 31st comes back as the 30th or 28th and then as the 31st again, with no drift.
  ``from_done``: the next due date is one interval after the moment the task was done.
* ``anchor_day`` (1..31) is the day of the month a month or year rule is anchored to; it is set when
  the rule is saved (:func:`with_anchor`) and kept, so a task that fell on the 28th of February
  comes back on the 31st in March.
* ``tz`` (an IANA name) is the wall clock of the rule: a daily task stays at 09:00 across a daylight
  saving change.

Minutes and hours are fixed lengths of time; days, weeks, months and years are counted on the wall
clock of the zone. All arithmetic is done on instants in UTC: two ``datetime`` values of the same
zone are subtracted and compared on the wall clock by Python, which would make an hour after the
change to summer time come out wrong. The results are instants in UTC. The number of intervals to
skip is computed in closed form (and checked with a few steps), never by looping once per missed
interval: that loop was a published denial of service in another task manager.
"""

import calendar
from datetime import UTC, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from topcoat_hub import clock

UNITS = ("minute", "hour", "day", "week", "month", "year")
MODES = ("after", "monthly", "from_done")
MAX_EVERY = 1000
_SECONDS = {"minute": 60, "hour": 3600}
_MONTHS = {"month": 1, "year": 12}


class RepeatError(ValueError):
    pass


def normalize(rule: Any) -> dict[str, Any]:
    """The checked rule, or a :class:`RepeatError`."""
    if not isinstance(rule, dict):
        raise RepeatError("a repeat rule is an object")
    extra = set(rule) - {"every", "unit", "mode", "tz", "anchor_day"}
    if extra:
        raise RepeatError(f"unknown field in the repeat rule: {sorted(extra)[0]}")
    every, unit = rule.get("every"), rule.get("unit")
    mode, tz, anchor = rule.get("mode", "after"), rule.get("tz"), rule.get("anchor_day")
    if not isinstance(every, int) or isinstance(every, bool) or not 1 <= every <= MAX_EVERY:
        raise RepeatError(f"every must be a whole number from 1 to {MAX_EVERY}")
    if unit not in UNITS:
        raise RepeatError(f"unit must be one of {', '.join(UNITS)}")
    if mode not in MODES:
        raise RepeatError(f"mode must be one of {', '.join(MODES)}")
    if mode == "monthly" and unit not in _MONTHS:
        raise RepeatError("mode monthly needs the unit month or year")
    if tz is not None and (not isinstance(tz, str) or not clock.valid_zone(tz)):
        raise RepeatError("tz must be an IANA time zone name")
    if anchor is not None and (
        not isinstance(anchor, int) or isinstance(anchor, bool) or not 1 <= anchor <= 31
    ):
        raise RepeatError("anchor_day must be a day of the month, 1 to 31")
    out: dict[str, Any] = {"every": every, "unit": unit, "mode": mode}
    if tz:
        out["tz"] = tz
    if anchor is not None:
        out["anchor_day"] = anchor
    return out


def with_anchor(
    rule: dict[str, Any], anchor: datetime | None, default_zone: str = clock.DEFAULT_ZONE
) -> dict[str, Any]:
    """The rule with ``anchor_day`` set from the date it first hangs on, for the units that count
    months (the rule is returned unchanged for the others, and when it has an anchor already)."""
    if rule["unit"] not in _MONTHS or "anchor_day" in rule or anchor is None:
        return rule
    tz = clock.zone(rule.get("tz"), default_zone)
    return {**rule, "anchor_day": anchor.astimezone(tz).day}


def add_months(dt: datetime, months: int, tz: ZoneInfo, day: int | None = None) -> datetime:
    """``dt`` plus whole calendar months on the wall clock of ``tz``; a day that the month does not
    have becomes its last day. ``day`` is the day of the month to aim for (default: the day of
    ``dt``)."""
    local = dt.astimezone(tz)
    total = local.year * 12 + (local.month - 1) + months
    year, month = divmod(total, 12)
    day = min(day or local.day, calendar.monthrange(year, month + 1)[1])
    return local.replace(year=year, month=month + 1, day=day).astimezone(UTC)


def _wall(dt: datetime, tz: ZoneInfo, **delta: int) -> datetime:
    """``dt`` plus a number of days/weeks on the wall clock (the time of day stays the same)."""
    local = dt.astimezone(tz).replace(tzinfo=None) + timedelta(**delta)
    return local.replace(tzinfo=tz).astimezone(UTC)


def shift(rule: dict[str, Any], dt: datetime, intervals: int, tz: ZoneInfo) -> datetime:
    """``dt`` moved forward by ``intervals`` repeat intervals."""
    every, unit = rule["every"], rule["unit"]
    n = every * intervals
    if unit in _SECONDS:
        return dt.astimezone(UTC) + timedelta(seconds=_SECONDS[unit] * n)
    if unit == "day":
        return _wall(dt, tz, days=n)
    if unit == "week":
        return _wall(dt, tz, weeks=n)
    return add_months(dt, _MONTHS[unit] * n, tz, rule.get("anchor_day"))


def intervals_to_skip(rule: dict[str, Any], anchor: datetime, now: datetime, tz: ZoneInfo) -> int:
    """How many intervals to move ``anchor`` so that it lies after ``now`` (at least one)."""
    every, unit = rule["every"], rule["unit"]
    anchor, now = anchor.astimezone(UTC), now.astimezone(UTC)
    if unit in _SECONDS:
        step = _SECONDS[unit] * every
        k = int((now - anchor).total_seconds() // step) + 1
    elif unit in ("day", "week"):
        days = every * (7 if unit == "week" else 1)
        k = (now.astimezone(tz).date() - anchor.astimezone(tz).date()).days // days
    else:
        a, n = anchor.astimezone(tz), now.astimezone(tz)
        months = (n.year - a.year) * 12 + (n.month - a.month)
        k = months // (every * _MONTHS[unit])
    k = max(k, 1)
    # the estimate is within a step of the answer: settle it (at most a handful of rounds)
    for _ in range(4):
        if k > 1 and shift(rule, anchor, k - 1, tz) > now:
            k -= 1
        elif shift(rule, anchor, k, tz) <= now:
            k += 1
        else:
            return k
    raise RepeatError("the repeat rule could not be settled")  # pragma: no cover - a bug, not input


def advance(
    rule: dict[str, Any],
    dates: dict[str, datetime | None],
    *,
    now: datetime,
    done_at: datetime | None = None,
    default_zone: str = clock.DEFAULT_ZONE,
) -> dict[str, datetime | None]:
    """The dates of the next occurrence. ``dates`` has ``start_at``, ``due_at`` and ``end_at``
    (any may be None); they all move by the same number of intervals, so their distances stay.
    The anchor is the due date, else the end, else the start. A task without any date comes back
    with no dates (it only reopens)."""
    rule = normalize(rule)
    tz = clock.zone(rule.get("tz"), default_zone)
    now = now.astimezone(UTC)
    dates = {k: (None if v is None else v.astimezone(UTC)) for k, v in dates.items()}
    anchor = dates.get("due_at") or dates.get("end_at") or dates.get("start_at")
    if anchor is None:
        return dict(dates)
    if rule["mode"] == "from_done":
        base = (done_at or now).astimezone(UTC)
        # one interval after the moment of completion: the anchor moves there, the others with it
        target = shift(rule, base, 1, tz)
        delta = target - anchor
        return {k: (None if v is None else v + delta) for k, v in dates.items()}
    k = intervals_to_skip(rule, anchor, now, tz)
    return {k_: (None if v is None else shift(rule, v, k, tz)) for k_, v in dates.items()}
