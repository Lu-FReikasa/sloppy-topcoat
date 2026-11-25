"""The library (reports and documents of the agent), apart from chats. Every route is scoped to
the caller (D20): the artifact of another user is 404, whatever its id."""

import uuid
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from topcoat_hub.artifact_store import AUTO_ORIGINS, ArtifactStore
from topcoat_hub.auth import CurrentUser, api_user
from topcoat_hub.db import get_db
from topcoat_hub.models import Artifact

router = APIRouter(prefix="/api/artifacts")


def _iso(value: Any) -> str | None:
    return value.isoformat() if value is not None else None


def artifact_view(
    a: Artifact, body: bool = False, application: str | None = None, origin: str | None = None
) -> dict[str, Any]:
    out: dict[str, Any] = {
        "id": str(a.id),
        "kind": a.kind,
        "application": application,
        "auto": origin in AUTO_ORIGINS,
        "title": a.title,
        "task_id": str(a.task_id) if a.task_id else None,
        "conversation_id": str(a.conversation_id) if a.conversation_id else None,
        "unread": a.read_at is None,
        "created_at": _iso(a.created_at),
        "updated_at": _iso(a.updated_at),
    }
    if body:
        out["body"] = a.body
    return out


def _not_found() -> HTTPException:
    return HTTPException(404, detail={"code": "not_found", "message": "unknown artifact"})


@router.get("")
async def list_artifacts(
    user: CurrentUser = Depends(api_user), db: AsyncSession = Depends(get_db)
) -> dict[str, Any]:
    store = ArtifactStore(db, user.uuid)
    return {
        "artifacts": [
            artifact_view(a, False, app, origin) for a, app, origin in await store.list()
        ],
        "unread": await store.unread_count(),
    }


@router.get("/{artifact_id}")
async def get_artifact(
    artifact_id: uuid.UUID,
    user: CurrentUser = Depends(api_user),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    art = await ArtifactStore(db, user.uuid).get(artifact_id)
    if art is None:
        raise _not_found()
    return artifact_view(art, body=True)


@router.post("/{artifact_id}/read", status_code=204)
async def mark_read(
    artifact_id: uuid.UUID,
    user: CurrentUser = Depends(api_user),
    db: AsyncSession = Depends(get_db),
) -> None:
    if not await ArtifactStore(db, user.uuid).mark_read(artifact_id):
        raise _not_found()
    await db.commit()


@router.delete("/{artifact_id}", status_code=204)
async def delete_artifact(
    artifact_id: uuid.UUID,
    user: CurrentUser = Depends(api_user),
    db: AsyncSession = Depends(get_db),
) -> None:
    if not await ArtifactStore(db, user.uuid).delete(artifact_id):
        raise _not_found()
    await db.commit()
