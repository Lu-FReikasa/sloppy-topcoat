"""The library of reports and documents the agent made, apart from chats (D20: every query is
scoped by ``user_id``). A finished task writes its report here; the person reads it on the
dashboard or in the library and never has to dig it out of an old chat."""

import uuid
from datetime import UTC, datetime

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from topcoat_hub.models import AgentTask, Artifact

TITLE_MAX = 200
# task origins of the hub's own work: a project run, an agent step of a block program. What they
# make is kept, but the Ablage shows it only on request.
AUTO_ORIGINS = frozenset({"batch", "block"})
BODY_MAX = 200_000


def utcnow() -> datetime:
    return datetime.now(UTC)


class ArtifactStore:
    def __init__(self, db: AsyncSession, user_id: uuid.UUID) -> None:
        self.db = db
        self.user_id = user_id

    async def put_report(
        self,
        task_id: uuid.UUID,
        title: str,
        body: str,
        conversation_id: uuid.UUID | None = None,
    ) -> Artifact:
        """The report of a task: one per task, a continued task rewrites it (and it counts as
        unread again)."""
        existing = await self.db.scalar(
            select(Artifact).where(Artifact.task_id == task_id, Artifact.user_id == self.user_id)
        )
        title, body = title.strip()[:TITLE_MAX] or "Report", body[:BODY_MAX]
        if existing is not None:
            existing.title, existing.body = title, body
            existing.updated_at, existing.read_at = utcnow(), None
            return existing
        art = Artifact(
            user_id=self.user_id,
            kind="report",
            title=title,
            body=body,
            task_id=task_id,
            conversation_id=conversation_id,
        )
        self.db.add(art)
        await self.db.flush()
        return art

    async def list(self, limit: int = 300) -> list[tuple[Artifact, str | None, str | None]]:
        """The person's artifacts, newest first, each with its task's application and origin."""
        rows = await self.db.execute(
            select(Artifact, AgentTask.application, AgentTask.origin)
            .outerjoin(AgentTask, AgentTask.id == Artifact.task_id)
            .where(Artifact.user_id == self.user_id)
            .order_by(Artifact.updated_at.desc())
            .limit(limit)
        )
        return [(a, app, origin) for a, app, origin in rows.all()]

    async def get(self, artifact_id: uuid.UUID) -> Artifact | None:
        return await self.db.scalar(
            select(Artifact).where(Artifact.id == artifact_id, Artifact.user_id == self.user_id)
        )

    async def unread_count(self) -> int:
        return (
            await self.db.scalar(
                select(func.count())
                .select_from(Artifact)
                .where(Artifact.user_id == self.user_id, Artifact.read_at.is_(None))
            )
        ) or 0

    async def mark_read(self, artifact_id: uuid.UUID) -> bool:
        art = await self.get(artifact_id)
        if art is None:
            return False
        if art.read_at is None:
            art.read_at = utcnow()
        return True

    async def delete(self, artifact_id: uuid.UUID) -> bool:
        res = await self.db.execute(
            delete(Artifact).where(Artifact.id == artifact_id, Artifact.user_id == self.user_id)
        )
        return bool(res.rowcount)
