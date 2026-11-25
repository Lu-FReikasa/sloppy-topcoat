"""The place of a person's waiting tasks in the pool's queue, told on the stream (the desktop plan,
6.3a: Clipper holds a ticket "Nr. 47 · noch 2 vor dir"). Every couple of seconds the queue is
looked at; a task whose place changed gets an ``agent_progress`` event for its owner only:
``{task_id, phase: "queued", ahead}``, and ``phase: "running"`` once it left the queue. Other
people's requests are only counted (``ahead``), never named."""

import asyncio
import logging
import uuid
from collections.abc import Callable
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from topcoat_hub.models import AgentTask

log = logging.getLogger(__name__)


def places(waiting: list[dict[str, Any]]) -> dict[str, int]:
    """Task id -> how many requests are ahead of its first one in the queue."""
    out: dict[str, int] = {}
    for job in waiting:
        tid = job.get("task_id")
        if isinstance(tid, str) and tid not in out:
            out[tid] = int(job.get("ahead") or 0)
    return out


class QueueWatch:
    def __init__(
        self,
        waiting: Callable[[], list[dict[str, Any]]],
        sessionmaker: async_sessionmaker[AsyncSession],
        publish: Callable[[str, dict[str, Any]], None],
        interval: float = 2.0,
    ) -> None:
        self.waiting = waiting
        self.sessionmaker = sessionmaker
        self.publish = publish
        self.interval = interval
        self.last: dict[str, int] = {}
        self.owners: dict[str, str] = {}

    async def _owners(self, ids: list[str]) -> None:
        unknown = [uuid.UUID(i) for i in ids if i not in self.owners]
        if not unknown:
            return
        async with self.sessionmaker() as db:
            rows = await db.execute(
                select(AgentTask.id, AgentTask.user_id).where(AgentTask.id.in_(unknown))
            )
            for tid, uid in rows.all():
                self.owners[str(tid)] = str(uid)

    async def tick(self) -> None:
        now = places(self.waiting())
        await self._owners(list(now))
        for tid, ahead in now.items():
            user = self.owners.get(tid)
            if user and self.last.get(tid) != ahead:
                self.publish(
                    "agent_progress",
                    {"task_id": tid, "phase": "queued", "ahead": ahead, "_user": user},
                )
        for tid in set(self.last) - set(now):
            user = self.owners.pop(tid, None)
            if user:
                self.publish("agent_progress", {"task_id": tid, "phase": "running", "_user": user})
        self.last = now

    async def scheduler(self) -> None:
        while True:
            try:
                await self.tick()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("queue watch: a tick failed")
            await asyncio.sleep(self.interval)
