"""The tick of the projects (PPH-04, 04h): due reminders and the daily list of overdue tasks.

A background task of the registry (``background.py``, task ``project_reminders``, D75). Every
minute it looks for ``project_reminders`` with ``fire_at <= now`` and no ``fired_at`` and closes
each through the store (``ProjectStore.fire_reminder``: the mark, the line ``reminder.fired`` made
by the system, and the inbox item, in one transaction). Because the rule is "due and not fired",
the first tick after downtime catches up by itself; what is more than a setting late is closed
without a message (the line says why).

Quiet hours (``users.project_quiet_*``) hold a person's reminders and digest back until the span has
ended. The daily digest (``users.project_digest_hour``) is one inbox item per local day; it is made
once the local hour is reached and the person is not in quiet hours, and only when something is
overdue. Everything here is the system acting for a person: the scan over all people is the one
place that is not scoped by ``user_id``, every action after it is.

The inbox kinds ``project_reminder`` and ``project_digest`` are deliberately **not** in
``inbox.AUTO_RESOLVED``: ``inbox.sync`` resolves the kinds listed there when the thing they are
about is gone, and nothing here is "open" in that sense; the person reads the item and it is filed.

When PLATFORM P3 (``TriggerService``) exists the tick moves onto it.
"""

import asyncio
import logging
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from topcoat_hub import background, clock
from topcoat_hub.config import Settings
from topcoat_hub.inbox import Candidate, InboxStore
from topcoat_hub.models import InboxItem, Project, ProjectNode, ProjectReminder, User
from topcoat_hub.project_base import APPLICATION, ProjectError
from topcoat_hub.project_events import SYSTEM
from topcoat_hub.project_reminders import EXPIRED, FIRED, SKIPPED
from topcoat_hub.project_store import ProjectStore

log = logging.getLogger(__name__)

TASK = "project_reminders"
KIND_REMINDER = "project_reminder"
KIND_DIGEST = "project_digest"
BATCH = 200  # reminders per tick; the rest is next minute's
OPEN = ("todo", "in_progress", "in_review", "blocked")
DIGEST_LINES = 5
FIRST_DELAY = 5.0
MIN_INTERVAL = 10.0


def in_quiet(local: datetime, start: int | None, end: int | None) -> bool:
    """Whether the wall-clock time ``local`` lies in the quiet span ``[start, end)``, minutes after
    midnight; the span may cross midnight (22:00 to 07:00). No span, or an empty one, is never
    quiet."""
    if start is None or end is None or start == end:
        return False
    minute = local.hour * 60 + local.minute
    if start < end:
        return start <= minute < end
    return minute >= start or minute < end


@dataclass
class TickResult:
    fired: int = 0
    expired: int = 0
    skipped: int = 0
    deferred: int = 0  # held back by quiet hours
    digests: int = 0
    failed: int = 0


@dataclass(frozen=True)
class Person:
    zone: ZoneInfo
    quiet_start: int | None
    quiet_end: int | None
    digest_hour: int | None

    def local(self, now: datetime) -> datetime:
        return now.astimezone(self.zone)

    def quiet(self, now: datetime) -> bool:
        return in_quiet(self.local(now), self.quiet_start, self.quiet_end)


def parse_minutes(text: str) -> int:
    """``"22:30"`` as minutes after midnight."""
    try:
        hours, minutes = text.split(":")
        value = int(hours) * 60 + int(minutes)
        if len(minutes) == 2 and 0 <= int(hours) <= 23 and 0 <= int(minutes) <= 59:
            return value
    except ValueError:
        pass
    raise ValueError("a time of day looks like 22:30")


def clock_text(minutes: int | None) -> str | None:
    return None if minutes is None else f"{minutes // 60:02d}:{minutes % 60:02d}"


async def read_prefs(
    sessionmaker: async_sessionmaker[AsyncSession], user_id: uuid.UUID
) -> dict[str, Any]:
    """The person's quiet hours and digest hour, as the API shows them."""
    async with sessionmaker() as db:
        row = (
            await db.execute(
                select(
                    User.project_quiet_start, User.project_quiet_end, User.project_digest_hour
                ).where(User.id == user_id)
            )
        ).first()
    start, end, hour = row if row else (None, None, None)
    return {
        "quiet_start": clock_text(start),
        "quiet_end": clock_text(end),
        "digest_hour": hour,
    }


async def write_prefs(
    sessionmaker: async_sessionmaker[AsyncSession],
    user_id: uuid.UUID,
    *,
    quiet_start: str | None,
    quiet_end: str | None,
    digest_hour: int | None,
) -> dict[str, Any]:
    """Store them. Quiet hours are both times or neither; the digest hour is 0 to 23 or None
    (no digest). Raises ``ValueError`` with the reason."""
    if (quiet_start is None) != (quiet_end is None):
        raise ValueError("quiet hours need a start and an end, or neither")
    if digest_hour is not None and not 0 <= digest_hour <= 23:
        raise ValueError("the digest hour is a whole hour from 0 to 23")
    start = None if quiet_start is None else parse_minutes(quiet_start)
    end = None if quiet_end is None else parse_minutes(quiet_end)
    async with sessionmaker() as db:
        row = await db.get(User, user_id)
        if row is None:
            raise ValueError("no such person")
        row.project_quiet_start, row.project_quiet_end = start, end
        row.project_digest_hour = digest_hour
        await db.commit()
    return await read_prefs(sessionmaker, user_id)


def reminder_candidate(project: Project, node: ProjectNode, reminder: ProjectReminder) -> Candidate:
    """The inbox item of a fired reminder. One per occurrence: the source names the reminder and the
    instant it was due, so a reminder that fires again for a new date is a new item."""
    fire_at = reminder.fire_at or clock.hub_now()
    return Candidate(
        kind=KIND_REMINDER,
        source_type="project_reminder",
        source_ref=f"{reminder.id}@{int(fire_at.timestamp())}",
        title=node.title,
        summary=f"{project.title} {node.number}",
        application=APPLICATION,
        urgent=False,
        ref={
            "module": "page.project",
            # what the inbox hands to the page: it opens in the project, on the task
            "props": {"project": str(project.id), "node": str(node.id)},
            "project_id": str(project.id),
            "node_id": str(node.id),
            "reminder_id": str(reminder.id),
        },
    )


async def due_reminders(
    sessionmaker: async_sessionmaker[AsyncSession], now: datetime, limit: int = BATCH
) -> list[tuple[uuid.UUID, uuid.UUID, datetime]]:
    """``(user, reminder, fire_at)`` of the reminders that are due and not fired, oldest first."""
    async with sessionmaker() as db:
        rows = await db.execute(
            select(ProjectReminder.user_id, ProjectReminder.id, ProjectReminder.fire_at)
            .where(
                ProjectReminder.application == APPLICATION,
                ProjectReminder.fire_at <= now,
                ProjectReminder.fired_at.is_(None),
            )
            .order_by(ProjectReminder.fire_at)
            .limit(limit)
        )
        return [(u, r, f) for u, r, f in rows]


class ReminderTick:
    def __init__(
        self,
        sessionmaker: async_sessionmaker[AsyncSession],
        settings: Settings,
    ) -> None:
        self.sessionmaker = sessionmaker
        self.settings = settings

    # --- what the flow says ----------------------------------------------------------------

    def policy(self) -> background.Background:
        return background.current(self, TASK, self.settings)

    def interval(self) -> float:
        """Seconds between ticks: the setting."""
        return max(MIN_INTERVAL, self.settings.project_reminder_tick_seconds)

    def max_age(self) -> timedelta:
        hours = self.policy().limit("fire", self.settings.project_reminder_catchup_hours)
        return timedelta(hours=hours)

    # --- the tick --------------------------------------------------------------------------

    async def run_once(self, now: datetime | None = None) -> TickResult:
        result = TickResult()
        policy = self.policy()
        if not policy.on:
            return result
        now = clock.aware(now) if now is not None else clock.hub_now()
        if policy.step("fire").on:
            await self._reminders(now, result)
        if policy.step("digest").on:
            await self._digests(now, result)
        return result

    async def _people(self, user_ids: set[uuid.UUID]) -> dict[uuid.UUID, Person]:
        if not user_ids:
            return {}
        async with self.sessionmaker() as db:
            rows = await db.execute(
                select(
                    User.id,
                    User.timezone,
                    User.project_quiet_start,
                    User.project_quiet_end,
                    User.project_digest_hour,
                ).where(User.id.in_(user_ids))
            )
            return {
                uid: Person(clock.zone(tz, self.settings.hub_timezone), start, end, hour)
                for uid, tz, start, end, hour in rows
            }

    async def _reminders(self, now: datetime, result: TickResult) -> None:
        due = await due_reminders(self.sessionmaker, now)
        people = await self._people({u for u, _, _ in due})
        store = ProjectStore(self.sessionmaker, SYSTEM, self.settings.hub_timezone)
        max_age = self.max_age()

        async def deliver(db: AsyncSession, project: Any, node: Any, reminder: Any) -> None:
            await InboxStore(db, project.user_id).add(reminder_candidate(project, node, reminder))

        for user_id, reminder_id, fire_at in due:
            person = people.get(user_id)
            # quiet hours hold a reminder back, unless it is too old to be delivered anyway
            if person is not None and person.quiet(now) and now - fire_at <= max_age:
                result.deferred += 1
                continue
            try:
                outcome = await store.fire_reminder(
                    user_id, reminder_id, now=now, max_age=max_age, deliver=deliver
                )
            except ProjectError:
                result.failed += 1
                log.warning("project reminders: %s could not be closed", reminder_id)
                continue
            except Exception:
                result.failed += 1
                log.exception("project reminders: %s failed", reminder_id)
                continue
            if outcome == FIRED:
                result.fired += 1
            elif outcome == EXPIRED:
                result.expired += 1
            elif outcome == SKIPPED:
                result.skipped += 1

    # --- the digest ------------------------------------------------------------------------

    async def _digests(self, now: datetime, result: TickResult) -> None:
        async with self.sessionmaker() as db:
            ids = set(
                await db.scalars(select(User.id).where(User.project_digest_hour.is_not(None)))
            )
        for user_id, person in (await self._people(ids)).items():
            local = person.local(now)
            if person.digest_hour is None or local.hour < person.digest_hour or person.quiet(now):
                continue
            try:
                if await self._digest(user_id, local, now):
                    result.digests += 1
            except Exception:
                result.failed += 1
                log.exception("project digest of %s failed", user_id)

    async def _digest(self, user_id: uuid.UUID, local: datetime, now: datetime) -> bool:
        day = local.date().isoformat()
        async with self.sessionmaker() as db:
            if await db.scalar(
                select(InboxItem.id).where(
                    InboxItem.user_id == user_id,
                    InboxItem.kind == KIND_DIGEST,
                    InboxItem.source_type == "project_digest",
                    InboxItem.source_ref == day,
                )
            ):
                return False  # today's is made
            overdue = (
                ProjectNode.user_id == user_id,
                ProjectNode.application == APPLICATION,
                ProjectNode.kind == "task",
                ProjectNode.status.in_(OPEN),
                ProjectNode.trashed_at.is_(None),
                ProjectNode.due_at < now,
                Project.status == "active",
                Project.trashed_at.is_(None),
            )
            count = await db.scalar(
                select(func.count()).select_from(ProjectNode).join(Project).where(*overdue)
            )
            if not count:
                return False
            rows = (
                await db.execute(
                    select(Project.title, ProjectNode.number, ProjectNode.title, ProjectNode.due_at)
                    .join(Project, Project.id == ProjectNode.project_id)
                    .where(*overdue)
                    .order_by(ProjectNode.due_at, ProjectNode.number)
                    .limit(DIGEST_LINES)
                )
            ).all()
            lines = [
                f"{p} {n}: {t} (due {d.astimezone(local.tzinfo).date().isoformat()})"
                for p, n, t, d in rows
            ]
            if count > len(lines):
                lines.append(f"… and {count - len(lines)} more")
            _, new = await InboxStore(db, user_id).add(
                Candidate(
                    kind=KIND_DIGEST,
                    source_type="project_digest",
                    source_ref=day,
                    title=f"{count} overdue task{'s' if count != 1 else ''}",
                    summary="\n".join(lines),
                    application=APPLICATION,
                    urgent=False,
                    ref={"module": "page.project"},
                )
            )
            await db.commit()
            return new

    # --- the loop --------------------------------------------------------------------------

    async def scheduler(self) -> None:
        await asyncio.sleep(FIRST_DELAY)
        while True:
            try:
                r = await self.run_once()
                if r.fired or r.expired or r.digests or r.failed:
                    log.info(
                        "project tick: %d reminder(s) delivered, %d expired, %d skipped, "
                        "%d digest(s), %d failed",
                        r.fired,
                        r.expired,
                        r.skipped,
                        r.digests,
                        r.failed,
                    )
            except Exception:
                log.exception("project tick failed")
            await asyncio.sleep(self.interval())
