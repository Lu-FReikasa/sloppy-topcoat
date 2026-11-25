"""Completing a task (PPH-04, 04h): a plain task is done; a repeating one is done *and* comes back.

The finished occurrence stays in the Akte: ``node.completed`` is written first, then
``node.reopened`` with the dates it moved to, in the same transaction and the same group, so the
node never rests in a state that is neither. The arithmetic is ``project_repeat.advance`` (on UTC
instants, whole intervals, closed form). The reopened task is ``todo`` again with its dates moved,
its checklists unticked (``project_progress.reset_checklist``), its percentage back at 0 and its
reminders moved with it. A reminder whose new time already lies in the past is closed at once: the
next occurrence must not announce itself with an echo of the old one.
"""

import uuid
from collections.abc import Awaitable, Callable
from typing import Any

from sqlalchemy import select

from topcoat_hub import clock, project_repeat
from topcoat_hub.models import ProjectNode, ProjectReminder
from topcoat_hub.project_base import (
    Invalid,
    StoreBase,
    attach_blockers,
    bump,
    check_revision,
    get_node,
    get_project,
    owned,
)
from topcoat_hub.project_fields import FieldError, check_dates, iso
from topcoat_hub.project_progress import reset_checklist
from topcoat_hub.project_reminders import move_reminders, recompute_reminders
from topcoat_hub.project_versions import VersionLog, make_diff

DATES = ("start_at", "due_at", "end_at")


class CompleteMixin(StoreBase):
    update_node: Callable[..., Awaitable[ProjectNode]]  # ProjectStore's own

    async def complete_node(
        self, user_id: uuid.UUID, node_id: uuid.UUID, *, revision: int
    ) -> ProjectNode:
        """Mark a task done. A task with a repeat rule is reopened for its next occurrence."""
        async with self._sessionmaker() as db:
            node = await get_node(db, user_id, node_id)
            if node.kind != "task":
                raise Invalid("only a task can be completed")
            if node.status == "done":
                raise Invalid("the task is done already")
            if node.status == "dropped":
                raise Invalid("a dropped task cannot be completed")
            if node.repeat_rule:
                return await self._complete_repeating(db, user_id, node_id, revision)
        return await self.update_node(user_id, node_id, revision=revision, status="done")

    async def _complete_repeating(
        self, db: Any, user_id: uuid.UUID, node_id: uuid.UUID, revision: int
    ) -> ProjectNode:
        node = await get_node(db, user_id, node_id)
        project = await get_project(db, user_id, node.project_id, lock=True)
        await db.refresh(node)  # under the lock: what a parallel writer did meanwhile
        check_revision(node.revision, revision)
        if node.status in ("done", "dropped"):
            raise Invalid("the task is done already")
        now = clock.hub_now()
        try:
            rule = project_repeat.normalize(node.repeat_rule)
            old = {name: getattr(node, name) for name in DATES}
            new = project_repeat.advance(
                rule, old, now=now, done_at=now, default_zone=self.default_zone
            )
            reminders = list(
                await db.scalars(
                    select(ProjectReminder).where(
                        ProjectReminder.node_id == node.id, owned(ProjectReminder, user_id)
                    )
                )
            )
            moved = move_reminders(
                rule, reminders, old, now=now, done_at=now, default_zone=self.default_zone
            )
            check_dates(new["start_at"], new["due_at"], new["end_at"], milestone=node.is_milestone)
        except (project_repeat.RepeatError, FieldError) as exc:
            raise Invalid(str(exc)) from exc

        old_status, old_body, old_title = node.status, node.body, node.title
        group = uuid.uuid4()
        await self._record(
            db,
            user_id,
            project,
            "node.completed",
            node=node,
            group_id=group,
            at=now,
            payload={"status": [old_status, "done"], "done_at": iso(now), "repeat": True},
        )
        node.status, node.done_at, node.percent_done = "todo", None, 0
        for name in DATES:
            setattr(node, name, new[name])
        node.body = reset_checklist(old_body)
        bump(node)
        await recompute_reminders(db, node)
        passed = 0
        for r in reminders:
            if r.fire_at is not None and r.fire_at <= now and r.fired_at is None:
                r.fired_at, passed = now, passed + 1
        await self._record(
            db,
            user_id,
            project,
            "node.reopened",
            node=node,
            group_id=group,
            at=now,
            payload={
                "status": ["done", "todo"],
                "dates": {n: [iso(old[n]), iso(new[n])] for n in DATES if old[n] != new[n]},
                "reminders": {"moved": moved, "passed": passed},
                "checklist_reset": node.body != old_body,
            },
        )
        if node.body != old_body:
            body_event = await self._record(
                db,
                user_id,
                project,
                "body.changed",
                node=node,
                group_id=group,
                at=now,
                payload={
                    "chars": [len(old_body), len(node.body)],
                    "diff": make_diff(old_body, node.body),
                },
            )
            await VersionLog.record(
                db, node, event_id=body_event.id, old_title=old_title, old_body=old_body
            )
        await db.commit()
        await attach_blockers(db, user_id, [node])
        return node
