"""A node comes online: what stopped for lack of it goes on (docs/PLATFORM.md, section 4).

A task that ended because no tool node was reachable (``no_tools``, ``tools_missing``) used to stay
ended until somebody pressed "continue". When a node that was offline comes online, those tasks are
continued by the hub, once per task and arrival, a few at a time, and the person finds a note about
it in the inbox. Nothing here is new power: continuing a task is what the person's own "continue"
does, with the same limits (the number of active tasks), on a task the person started, and a task
is only taken up again a few times so one that keeps failing does not loop.

The first use of the triggers of the platform plan; the trigger itself (a node's state changes from
offline to online, read from ``node_updated``) is meant to move into the flow editor as a node."""

import asyncio
import logging
import uuid
from collections import defaultdict
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from topcoat_hub.agent_service import AgentError, AgentService
from topcoat_hub.auth import CurrentUser
from topcoat_hub.background import current
from topcoat_hub.config import Settings
from topcoat_hub.inbox import Candidate, InboxStore
from topcoat_hub.live import EventBus
from topcoat_hub.models import AgentTask

log = logging.getLogger(__name__)

STUCK_REASONS = ("no_tools", "tools_missing")


class StuckTaskResumer:
    def __init__(
        self,
        bus: EventBus,
        sessionmaker: async_sessionmaker[AsyncSession],
        agents: AgentService,
        settings: Settings,
    ) -> None:
        self.bus, self.sessionmaker, self.agents, self.settings = (
            bus,
            sessionmaker,
            agents,
            settings,
        )
        self._states: dict[str, str] = {}
        self._task: asyncio.Task[None] | None = None

    # --- the event ---------------------------------------------------------------------------

    def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._run(), name="resume-stuck-tasks")

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
            self._task = None

    async def _run(self) -> None:
        queue = self.bus.subscribe()
        try:
            while True:
                event, data = await queue.get()
                try:
                    await self.handle(event, data)
                except Exception:
                    log.exception("resuming stuck tasks failed")
        finally:
            self.bus.unsubscribe(queue)

    def came_online(self, event: str, data: dict[str, Any]) -> str | None:
        """The id of the node when this event says a node that was offline (or not seen yet since
        the hub started) is online now; None for anything else. Heartbeat-like updates of a node
        that stays online do not count."""
        if event != "node_updated":
            return None
        node_id = str(data.get("id") or "")
        state = str(data.get("state") or "")
        if not node_id or not state:
            return None
        before = self._states.get(node_id)
        self._states[node_id] = state
        if state != "offline" and before in (None, "offline"):
            return node_id
        return None

    async def handle(self, event: str, data: dict[str, Any]) -> int:
        """React to one event of the bus; returns how many tasks were continued."""
        node_id = self.came_online(event, data)
        bg = current(self, "resume")
        if node_id is None or not self.settings.resume_stuck_enabled:
            return 0
        if not (bg.on and bg.step("resume").on):
            return 0
        return await self.resume_all(node_id, str(data.get("name") or ""))

    # --- what goes on ------------------------------------------------------------------------

    async def _stuck(self, db: AsyncSession) -> dict[uuid.UUID, list[AgentTask]]:
        since = datetime.now(UTC) - timedelta(hours=self.settings.resume_stuck_hours)
        rows = await db.scalars(
            select(AgentTask)
            .where(
                AgentTask.status == "failed",
                AgentTask.stop_reason.in_(STUCK_REASONS),
                AgentTask.finished_at >= since,
                AgentTask.runs < self.settings.resume_stuck_attempts,
            )
            .order_by(AgentTask.finished_at)
        )
        by_user: dict[uuid.UUID, list[AgentTask]] = defaultdict(list)
        for task in rows:
            by_user[task.user_id].append(task)
        return by_user

    async def resume_all(self, node_id: str, node_name: str) -> int:
        async with self.sessionmaker() as db:
            stuck = await self._stuck(db)
        total = 0
        for user_id, tasks in stuck.items():
            user = CurrentUser(id=str(user_id), name="", role="user")
            done: list[tuple[str, str]] = []  # (task id, title)
            for task in tasks[
                : current(self, "resume").limit("resume", self.settings.resume_stuck_max)
            ]:
                try:
                    await self.agents.resume(user, task.id)
                except AgentError as exc:  # too many tasks running, or not resumable any more
                    log.info("stuck task %s not continued: %s", task.id, exc.code)
                    if exc.code == "too_many_tasks":
                        break
                    continue
                done.append((str(task.id), task.title or task.prompt[:60]))
            if not done:
                continue
            total += len(done)
            await self._tell(user_id, node_id, node_name, done)
        if total:
            log.info("agent: %d task(s) continued because a node came online", total)
        return total

    async def _tell(
        self, user_id: uuid.UUID, node_id: str, node_name: str, tasks: list[tuple[str, str]]
    ) -> None:
        """A note in the inbox: what continues and why; it opens the first of the tasks."""
        titles = [title for _, title in tasks]
        stamp = datetime.now(UTC).isoformat(timespec="seconds")
        async with self.sessionmaker() as db:
            await InboxStore(db, user_id).add(
                Candidate(
                    kind="tasks_resumed",
                    source_type="node_online",
                    source_ref=f"{node_id}:{stamp}",
                    title=f"{len(titles)} task(s) continue"
                    + (f": {node_name} is online" if node_name else ""),
                    summary="; ".join(titles)[:500],
                    application="admin",
                    ref={"module": "page.tasks", "props": {"id": tasks[0][0]}},
                )
            )
            await db.commit()
        self.bus.publish("inbox", {"_user": str(user_id)})
