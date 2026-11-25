"""API keys for the gateway (E1-05): each user manages their own keys."""

import secrets
import uuid
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from topcoat_hub.auth import API_KEY_PREFIX, CurrentUser, hash_secret, require_admin
from topcoat_hub.db import get_db
from topcoat_hub.models import ApiKey

router = APIRouter(prefix="/api/keys")


class KeyRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=100)


def _view(key: ApiKey) -> dict[str, Any]:
    return {
        "id": str(key.id),
        "name": key.name,
        "prefix": key.prefix,
        "created_at": key.created_at.isoformat() if key.created_at else None,
        "last_used_at": key.last_used_at.isoformat() if key.last_used_at else None,
    }


@router.get("")
async def list_keys(
    db: AsyncSession = Depends(get_db), user: CurrentUser = Depends(require_admin)
) -> list[dict[str, Any]]:
    rows = await db.scalars(
        select(ApiKey)
        .where(ApiKey.user_id == uuid.UUID(user.id), ApiKey.revoked_at.is_(None))
        .order_by(ApiKey.created_at)
    )
    return [_view(k) for k in rows]


@router.post("", status_code=201)
async def create_key(
    body: KeyRequest,
    db: AsyncSession = Depends(get_db),
    user: CurrentUser = Depends(require_admin),
) -> dict[str, Any]:
    """Create a key. The response is the only time the key is shown."""
    token = API_KEY_PREFIX + secrets.token_urlsafe(30)
    key = ApiKey(
        user_id=uuid.UUID(user.id),
        name=body.name.strip(),
        prefix=token[:10],
        key_hash=hash_secret(token),
    )
    db.add(key)
    await db.commit()
    await db.refresh(key)
    return {**_view(key), "key": token}


@router.delete("/{key_id}", status_code=204)
async def revoke_key(
    key_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    user: CurrentUser = Depends(require_admin),
) -> None:
    key = await db.scalar(
        select(ApiKey).where(
            ApiKey.id == key_id,
            ApiKey.user_id == uuid.UUID(user.id),
            ApiKey.revoked_at.is_(None),
        )
    )
    if key is None:
        raise HTTPException(404, detail={"code": "not_found", "message": "unknown key"})
    key.revoked_at = func.now()
    await db.commit()
