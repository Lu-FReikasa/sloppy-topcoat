"""The machines the nodes run on (docs/OVERVIEW.md; the Administration overview)."""

from typing import Any

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from topcoat_hub import machines
from topcoat_hub.auth import CurrentUser, api_user, require_admin
from topcoat_hub.chat_store import get_hub_setting, set_hub_setting
from topcoat_hub.db import get_db
from topcoat_hub.models import Node

router = APIRouter(prefix="/api")


class MachinesBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    machines: list[dict[str, Any]] = []
    assign: dict[str, str] = {}


async def _names(db: AsyncSession) -> list[str]:
    return list((await db.scalars(select(Node.name).order_by(Node.name))).all())


async def _view(db: AsyncSession) -> dict[str, Any]:
    mapping = machines.load(await get_hub_setting(db, machines.SETTING_KEY))
    return machines.view(mapping, await _names(db))


@router.get("/machines")
async def get_machines(
    db: AsyncSession = Depends(get_db), _user: CurrentUser = Depends(api_user)
) -> dict[str, Any]:
    """The machines with the nodes on them, and for every node its machine."""
    return await _view(db)


@router.put("/machines")
async def put_machines(
    body: MachinesBody,
    db: AsyncSession = Depends(get_db),
    _admin: CurrentUser = Depends(require_admin),
) -> Any:
    try:
        mapping = machines.parse({"machines": body.machines, "assign": body.assign})
        machines.check_nodes(mapping, set(await _names(db)))
    except machines.MachineError as exc:
        return JSONResponse(
            {"detail": {"code": "invalid_machines", "message": str(exc)}}, status_code=400
        )
    await set_hub_setting(db, machines.SETTING_KEY, mapping.to_json() or None)
    await db.commit()
    return await _view(db)


@router.delete("/machines")
async def reset_machines(
    db: AsyncSession = Depends(get_db), _admin: CurrentUser = Depends(require_admin)
) -> dict[str, Any]:
    """Back to the machines the node names point at."""
    await set_hub_setting(db, machines.SETTING_KEY, None)
    await db.commit()
    return await _view(db)
