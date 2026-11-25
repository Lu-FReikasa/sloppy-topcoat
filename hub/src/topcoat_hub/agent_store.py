"""Data access for the agent's tasks, transcript and evidence (E3-01, D20).

Like ``ChatStore`` and ``MemoryStore``: an :class:`AgentStore` belongs to exactly one user and
**every statement it runs filters by that user's id**. A task of somebody else is "not found",
whatever the caller knows about its id. The one cross-user query, :func:`unfinished`, only returns
ids and is for the runner that resumes tasks after a restart.
"""

import uuid
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from topcoat_hub.models import AgentEvidence, AgentStep, AgentTask

TITLE_MAX = 80
ACTIVE = ("queued", "running")
# a project task's block program (project_programs.py): kept as a task, run by the block service;
# the agent runner never starts it, after a restart the project requeues it
PROGRAM_KIND = "program"


def utcnow() -> datetime:
    return datetime.now(UTC)


def title_of(prompt: str) -> str:
    """The first line that says something: the lines of the frame (``depth: ``) and of a
    revision (``revise: ``) are headers, not a title."""
    lines = [ln for ln in prompt.strip().splitlines() if ln.strip()]
    body = [ln for ln in lines if not ln.startswith(("depth: ", "revise: "))]
    line = " ".join((body or lines or [""])[0].split())
    return line[:TITLE_MAX] + ("…" if len(line) > TITLE_MAX else "")


class AgentStore:
    def __init__(self, db: AsyncSession, user_id: uuid.UUID) -> None:
        self.db = db
        self.user_id = user_id

    def _own(self, task: AgentTask) -> None:
        """A store never writes to another user's task, whatever object it is handed."""
        if task.user_id != self.user_id:
            raise LookupError("that task belongs to another user")

    async def create_task(
        self,
        prompt: str,
        model: str | None,
        via: str,
        origin: str = "tasks",
        scope: dict[str, Any] | None = None,
        kind: str = "single",
        parent_id: uuid.UUID | None = None,
        conversation_id: uuid.UUID | None = None,
        deadline_at: datetime | None = None,
        application: str | None = None,
        capabilities: list[str] | None = None,
    ) -> AgentTask:
        task = AgentTask(
            application=application,
            capabilities=capabilities,
            conversation_id=conversation_id,
            deadline_at=deadline_at,
            user_id=self.user_id,
            prompt=prompt,
            title=title_of(prompt),
            model=model,
            via=via,
            origin=origin,
            scope=scope,
            kind=kind,
            parent_id=parent_id,
        )
        self.db.add(task)
        await self.db.commit()
        return task

    async def children(self, task_id: uuid.UUID) -> Sequence[AgentTask]:
        """The checks a coordinator task started, in the order they were started."""
        rows = await self.db.scalars(
            select(AgentTask)
            .where(AgentTask.parent_id == task_id, AgentTask.user_id == self.user_id)
            .order_by(AgentTask.created_at, AgentTask.id)
        )
        return rows.all()

    async def count_active(self) -> int:
        rows = await self.db.scalars(
            select(AgentTask.id).where(
                AgentTask.user_id == self.user_id,
                AgentTask.status.in_(ACTIVE),
                AgentTask.kind != PROGRAM_KIND,  # a program run is no agent run; its blocks are
            )
        )
        return len(rows.all())

    async def get(self, task_id: uuid.UUID) -> AgentTask | None:
        return await self.db.scalar(
            select(AgentTask).where(AgentTask.id == task_id, AgentTask.user_id == self.user_id)
        )

    async def list(self, limit: int = 100, origin: str | None = "tasks") -> Sequence[AgentTask]:
        """The user's tasks, newest first; ``origin=None`` includes the checks of chat answers."""
        where = [AgentTask.user_id == self.user_id]
        if origin:
            where.append(AgentTask.origin == origin)
        rows = await self.db.scalars(
            select(AgentTask)
            .where(*where)
            .order_by(AgentTask.created_at.desc(), AgentTask.id)
            .limit(limit)
        )
        return rows.all()

    async def steps(self, task_id: uuid.UUID) -> Sequence[AgentStep]:
        rows = await self.db.scalars(
            select(AgentStep)
            .where(AgentStep.task_id == task_id, AgentStep.user_id == self.user_id)
            .order_by(AgentStep.seq)
        )
        return rows.all()

    async def evidence(self, task_id: uuid.UUID) -> Sequence[AgentEvidence]:
        rows = await self.db.scalars(
            select(AgentEvidence)
            .where(AgentEvidence.task_id == task_id, AgentEvidence.user_id == self.user_id)
            .order_by(AgentEvidence.id)
        )
        return rows.all()

    async def add_step(self, task: AgentTask, kind: str, data: dict[str, Any]) -> AgentStep:
        """Append to the transcript (``task`` must be this user's; the seq is the next number)."""
        self._own(task)
        task.step_count += 1
        step = AgentStep(
            task_id=task.id, user_id=self.user_id, seq=task.step_count, kind=kind, data=data
        )
        self.db.add(step)
        await self.db.commit()
        return step

    async def add_evidence(
        self, task: AgentTask, tag: str, source: dict[str, Any], excerpt: str, size: int
    ) -> AgentEvidence:
        self._own(task)
        n = len(await self.evidence(task.id)) + 1
        ev = AgentEvidence(
            task_id=task.id,
            user_id=self.user_id,
            ref=f"E{n}",
            tag=tag,
            source=source,
            excerpt=excerpt,
            bytes=size,
        )
        self.db.add(ev)
        await self.db.commit()
        return ev

    async def set_status(self, task: AgentTask, status: str, **fields: Any) -> None:
        self._own(task)
        task.status = status
        for k, v in fields.items():
            setattr(task, k, v)
        if status in ("done", "failed", "canceled"):
            task.finished_at = utcnow()
        await self.db.commit()


async def unfinished(db: AsyncSession) -> list[tuple[uuid.UUID, uuid.UUID]]:
    """(task id, user id) of every task that was queued or running — for the runner after a hub
    restart. Ids only: the tasks themselves are read through the owner's store."""
    rows = await db.execute(
        select(AgentTask.id, AgentTask.user_id)
        .where(AgentTask.status.in_(ACTIVE), AgentTask.kind != PROGRAM_KIND)
        .order_by(AgentTask.created_at)
    )
    return [(r[0], r[1]) for r in rows.all()]
