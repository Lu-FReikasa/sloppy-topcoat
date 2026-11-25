"""Reminders of a task (PPH-04, 04b; fired by 04h).

A reminder is either at an absolute time or ``offset_seconds`` before or after the task's due, start
or end date (a negative offset is before). ``fire_at`` is the instant it fires: for a relative one
it is computed from the date, and recomputed in the transaction that changes the date, so the two
never disagree. A reminder whose date is missing has no ``fire_at`` and waits. ``fired_at`` marks
one as delivered; a reminder that came due while the hub was down is delivered once when it is back
(04h).
"""

import uuid
from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from topcoat_hub import clock, project_repeat
from topcoat_hub.models import Project, ProjectNode, ProjectReminder
from topcoat_hub.project_base import (
    Invalid,
    NotFound,
    StoreBase,
    get_node,
    get_project,
    owned,
)

# what the tick hands to ``fire_reminder`` to put the message in the inbox (the store knows nothing
# of the inbox); it runs inside the transaction that marks the reminder as fired
Deliver = Callable[[AsyncSession, Project, ProjectNode, ProjectReminder], Awaitable[None]]
# the ways a due reminder ends
FIRED, EXPIRED, SKIPPED, NOT_DUE = "fired", "expired", "skipped", "not_due"
MAX_PER_NODE = 10
MAX_OFFSET = 366 * 5 * 86400  # five years, in seconds
BASES = {"due": "due_at", "start": "start_at", "end": "end_at"}


def fire_time(
    at: datetime | None, relative_to: str | None, offset: int | None, node: ProjectNode
) -> datetime | None:
    """When a reminder fires, or None while the date it hangs on is missing. Pure."""
    if at is not None:
        return at
    base = getattr(node, BASES[relative_to or "due"], None)
    return None if base is None else base + timedelta(seconds=offset or 0)


async def recompute_reminders(db: AsyncSession, node: ProjectNode) -> None:
    """Bring ``fire_at`` of the node's reminders in line with its dates. A reminder that has fired
    and is moved into the future by a new date fires again (the task got a new due date)."""
    rows = await db.scalars(select(ProjectReminder).where(ProjectReminder.node_id == node.id))
    for r in rows:
        new = fire_time(r.at, r.relative_to, r.offset_seconds, node)
        if new != r.fire_at:
            r.fire_at = new
            if r.fired_at is not None and new is not None and new > r.fired_at:
                r.fired_at = None


def move_reminders(
    rule: dict[str, Any],
    reminders: list[ProjectReminder],
    old_dates: dict[str, datetime | None],
    *,
    now: datetime,
    done_at: datetime,
    default_zone: str,
) -> int:
    """A repeating task was finished: move the times of its absolute reminders as far as its dates
    moved (relative ones follow the dates by themselves, ``recompute_reminders``). Both go through
    the same arithmetic, on instants, so the distance between a reminder and the date holds. A task
    with no date moves each absolute reminder by its own rule instead. Returns how many moved."""
    absolute = [r for r in reminders if r.at is not None]
    if not absolute:
        return 0
    if any(v is not None for v in old_dates.values()):
        extra = {f"r{i}": r.at for i, r in enumerate(absolute)}
        moved = project_repeat.advance(
            rule, {**old_dates, **extra}, now=now, done_at=done_at, default_zone=default_zone
        )
        for i, r in enumerate(absolute):
            r.at = moved[f"r{i}"]
    else:
        for r in absolute:
            r.at = project_repeat.advance(
                rule, {"due_at": r.at}, now=now, done_at=done_at, default_zone=default_zone
            )["due_at"]
    return len(absolute)


class ReminderMixin(StoreBase):
    async def add_reminder(
        self,
        user_id: uuid.UUID,
        node_id: uuid.UUID,
        *,
        at: datetime | None = None,
        relative_to: str | None = None,
        offset_seconds: int | None = None,
    ) -> ProjectReminder:
        if (at is None) == (relative_to is None):
            raise Invalid("a reminder is either at a time or relative to a date")
        if at is not None:
            try:
                clock.aware(at)
            except ValueError as exc:
                raise Invalid(str(exc)) from exc
            if offset_seconds is not None:
                raise Invalid("an offset belongs to a relative reminder")
        elif relative_to not in BASES:
            raise Invalid("relative_to must be due, start or end")
        elif offset_seconds is None or abs(offset_seconds) > MAX_OFFSET:
            raise Invalid("the offset is a number of seconds, before (negative) or after the date")
        async with self._sessionmaker() as db:
            node = await get_node(db, user_id, node_id)
            if node.kind != "task":
                raise Invalid("only a task has reminders")
            project = await get_project(db, user_id, node.project_id, lock=True)
            count = len(
                list(
                    await db.scalars(
                        select(ProjectReminder.id).where(ProjectReminder.node_id == node_id)
                    )
                )
            )
            if count >= MAX_PER_NODE:
                raise Invalid(f"a task has at most {MAX_PER_NODE} reminders")
            reminder = ProjectReminder(
                id=uuid.uuid4(),
                user_id=user_id,
                application="project",
                node_id=node_id,
                at=at,
                relative_to=relative_to,
                offset_seconds=offset_seconds,
                fire_at=fire_time(at, relative_to, offset_seconds, node),
            )
            db.add(reminder)
            await db.flush()
            await self._record(
                db,
                user_id,
                project,
                "reminder.added",
                node=node,
                payload={
                    "at": clock.iso(at),
                    "relative_to": relative_to,
                    "offset_seconds": offset_seconds,
                    "fire_at": clock.iso(reminder.fire_at),
                },
            )
            await db.commit()
            return reminder

    async def remove_reminder(self, user_id: uuid.UUID, reminder_id: uuid.UUID) -> None:
        async with self._sessionmaker() as db:
            reminder = await db.scalar(
                select(ProjectReminder).where(
                    ProjectReminder.id == reminder_id, owned(ProjectReminder, user_id)
                )
            )
            if reminder is None:
                raise NotFound("reminder")
            node = await get_node(db, user_id, reminder.node_id)
            project = await get_project(db, user_id, node.project_id, lock=True)
            await self._record(
                db,
                user_id,
                project,
                "reminder.removed",
                node=node,
                payload={
                    "at": clock.iso(reminder.at),
                    "relative_to": reminder.relative_to,
                    "offset_seconds": reminder.offset_seconds,
                },
            )
            await db.delete(reminder)
            await db.commit()

    async def reminders_of(self, user_id: uuid.UUID, node_id: uuid.UUID) -> list[ProjectReminder]:
        async with self._sessionmaker() as db:
            await get_node(db, user_id, node_id)
            rows = await db.scalars(
                select(ProjectReminder)
                .where(ProjectReminder.node_id == node_id, owned(ProjectReminder, user_id))
                .order_by(ProjectReminder.fire_at.nulls_last(), ProjectReminder.created_at)
            )
            return list(rows)

    async def fire_reminder(
        self,
        user_id: uuid.UUID,
        reminder_id: uuid.UUID,
        *,
        now: datetime,
        max_age: timedelta,
        deliver: Deliver | None = None,
    ) -> str:
        """Close a due reminder (04h): mark it fired, write ``reminder.fired`` and let ``deliver``
        put the message in the inbox, all in one transaction. The project is locked and the
        reminder read again under the lock, so two ticks (two hub processes) never deliver one
        reminder twice. One that is more than ``max_age`` late (the hub was down), or whose task is
        done, dropped or in the trash, is closed without a message; the line says so. Returns what
        happened: ``fired``, ``expired``, ``skipped`` or ``not_due``."""
        async with self._sessionmaker() as db:
            reminder = await db.scalar(
                select(ProjectReminder).where(
                    ProjectReminder.id == reminder_id, owned(ProjectReminder, user_id)
                )
            )
            if reminder is None:
                raise NotFound("reminder")
            node = await get_node(db, user_id, reminder.node_id)
            project = await get_project(db, user_id, node.project_id, lock=True)
            await db.refresh(reminder)  # what another tick did meanwhile
            fire_at = reminder.fire_at
            if reminder.fired_at is not None or fire_at is None or fire_at > now:
                return NOT_DUE
            reason = ""
            if now - fire_at > max_age:
                reason = "too_old"
            elif (
                node.trashed_at is not None
                or project.trashed_at is not None
                or node.status in ("done", "dropped")
                or project.status == "dropped"
            ):
                reason = "not_open"
            reminder.fired_at = now
            payload: dict[str, Any] = {"fire_at": clock.iso(fire_at), "delivered": not reason}
            if reason:
                payload["reason"] = reason
            elif deliver is not None:
                await deliver(db, project, node, reminder)
            await self._record(
                db, user_id, project, "reminder.fired", node=node, payload=payload, at=now
            )
            await db.commit()
            return {"": FIRED, "too_old": EXPIRED, "not_open": SKIPPED}[reason]
