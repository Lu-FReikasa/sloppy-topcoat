"""Changes the Projekte chat prepared and the person confirms (ARCHITECTURE D15).

A chat may read everything of its own application, but it changes nothing by itself: a check that
runs for a chat (``Caller.stage_writes``) answers a writing project tool with "prepared", and this
module keeps the exact call. The person applies it with one click on that one row, or refuses it;
there is no blanket approval. The row is immutable (``content_hash`` covers the capability and the
input) and every statement filters by ``user_id`` (D20): another person's change is "not found".

``preview`` is data for the card, never prose: the target (a project or the ref of a node, such as
``Topcoat 1.3.2``) and the fields that would change. The web side words it in the person's language.
"""

import hashlib
import json
import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from topcoat_hub.models import ProjectChange
from topcoat_hub.project_read_tools import READ_NAMES

READ_ONLY = READ_NAMES
PENDING, APPLIED, REFUSED, FAILED = "pending", "applied", "refused", "failed"


def content_hash(capability: str, tool_input: dict[str, Any]) -> str:
    body = json.dumps({"capability": capability, "input": tool_input}, sort_keys=True)
    return hashlib.sha256(body.encode()).hexdigest()


def view(row: ProjectChange) -> dict[str, Any]:
    return {
        "id": str(row.id),
        "task_id": str(row.task_id) if row.task_id else None,
        "capability": row.capability,
        "input": row.input,
        "preview": row.preview,
        "status": row.status,
        "result": row.result,
        "created_at": row.created_at.isoformat() if row.created_at else None,
        "decided_at": row.decided_at.isoformat() if row.decided_at else None,
    }


class ChangeError(Exception):
    def __init__(self, status: int, code: str, message: str) -> None:
        super().__init__(message)
        self.status, self.code, self.message = status, code, message


class ChangeStore:
    def __init__(self, sessionmaker: async_sessionmaker[AsyncSession]) -> None:
        self.sm = sessionmaker

    async def stage(
        self,
        user_id: uuid.UUID,
        task_id: uuid.UUID | None,
        capability: str,
        tool_input: dict[str, Any],
        preview: dict[str, Any],
    ) -> ProjectChange:
        async with self.sm() as db:
            row = ProjectChange(
                user_id=user_id,
                task_id=task_id,
                capability=capability,
                input=tool_input,
                preview=preview,
                content_hash=content_hash(capability, tool_input),
                status=PENDING,
            )
            db.add(row)
            await db.commit()
            await db.refresh(row)
            return row

    async def for_task(self, user_id: uuid.UUID, task_id: uuid.UUID) -> list[ProjectChange]:
        async with self.sm() as db:
            rows = await db.scalars(
                select(ProjectChange)
                .where(ProjectChange.user_id == user_id, ProjectChange.task_id == task_id)
                .order_by(ProjectChange.created_at, ProjectChange.id)
            )
            return list(rows)

    async def get(self, user_id: uuid.UUID, change_id: uuid.UUID) -> ProjectChange:
        async with self.sm() as db:
            row = await db.scalar(
                select(ProjectChange).where(
                    ProjectChange.id == change_id, ProjectChange.user_id == user_id
                )
            )
        if row is None:
            raise ChangeError(404, "not_found", "there is no such change")
        return row

    async def claim(self, user_id: uuid.UUID, change_id: uuid.UUID) -> ProjectChange:
        """Take a pending change for one decision: a second click finds it already taken."""
        row = await self.get(user_id, change_id)
        if row.content_hash != content_hash(row.capability, row.input):
            raise ChangeError(409, "tampered", "the prepared change no longer matches its hash")
        async with self.sm() as db:
            taken = await db.execute(
                update(ProjectChange)
                .where(
                    ProjectChange.id == change_id,
                    ProjectChange.user_id == user_id,
                    ProjectChange.status == PENDING,
                )
                .values(status="running")
            )
            await db.commit()
        if taken.rowcount != 1:
            raise ChangeError(409, "decided", "this change was already decided")
        return row

    async def finish(
        self, user_id: uuid.UUID, change_id: uuid.UUID, status: str, result: dict[str, Any]
    ) -> ProjectChange:
        async with self.sm() as db:
            await db.execute(
                update(ProjectChange)
                .where(ProjectChange.id == change_id, ProjectChange.user_id == user_id)
                .values(status=status, result=result, decided_at=datetime.now(UTC))
            )
            await db.commit()
        return await self.get(user_id, change_id)

    async def refuse(self, user_id: uuid.UUID, change_id: uuid.UUID) -> ProjectChange:
        await self.claim(user_id, change_id)
        return await self.finish(user_id, change_id, REFUSED, {})
