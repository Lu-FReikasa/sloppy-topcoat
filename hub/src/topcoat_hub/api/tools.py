"""Tool nodes over REST (E2-04): which capabilities a node offers, running one, grants, audit.

Whoever may run what is decided in :mod:`topcoat_hub.tools` (D20); this module only maps it to
HTTP. A user who may use nothing on a node gets 404 for it, so it does not even reveal that the
node exists.
"""

import uuid
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from topcoat_hub.auth import CurrentUser, api_user, require_admin
from topcoat_hub.db import get_db
from topcoat_hub.models import Node, NodeInventory, User
from topcoat_hub.tools import ALL, Caller, ToolError, ToolService, enabled, offered, usable

router = APIRouter(prefix="/api")


def _tools(request: Request) -> ToolService:
    return request.app.state.tools


def _error(status: int, code: str, message: str) -> HTTPException:
    return HTTPException(status, detail={"code": code, "message": message})


class InvokeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    input: dict[str, Any] = Field(default_factory=dict)


class GrantRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    capabilities: list[str] = Field(max_length=64)


@router.get("/nodes/{node_id}/capabilities")
async def node_capabilities(
    node_id: uuid.UUID,
    request: Request,
    db: AsyncSession = Depends(get_db),
    user: CurrentUser = Depends(api_user),
) -> dict[str, Any]:
    """What a node offers, what its config switched on, and what *this user* may run."""
    caller = Caller.of(user)
    svc = _tools(request)
    node = await db.get(Node, node_id)
    grants = await svc.grants_of(db, caller.user_id, node_id)
    if node is None or not (caller.admin or grants):
        raise _error(404, "not_found", "unknown node")
    inv = await db.get(NodeInventory, node_id)
    inventory = inv.inventory if inv else None
    off = offered(inventory)
    return {
        "node": {"id": str(node.id), "name": node.name, "roles": node.config.get("roles", [])},
        "offered": list(off.values()),
        "enabled": enabled(node.config),
        "usable": usable(caller, grants, node.config, inventory),
        "grants": sorted(grants),
    }


@router.post("/nodes/{node_id}/capabilities/{capability}")
async def run_capability(
    node_id: uuid.UUID,
    capability: str,
    body: InvokeRequest,
    request: Request,
    user: CurrentUser = Depends(api_user),
) -> dict[str, Any]:
    """Run one read-only capability with the rights of the caller. The answer is
    ``{"ok": true, "output": …}``; a capability that ran and failed is ``{"ok": false, "error":
    {"code", "message"}}`` with status 200 (the call itself worked); a refused call is an HTTP
    error."""
    try:
        return await _tools(request).invoke(Caller.of(user), node_id, capability, body.input)
    except ToolError as exc:
        raise _error(exc.status, exc.code, exc.message) from exc


@router.get("/nodes/{node_id}/grants")
async def list_grants(
    node_id: uuid.UUID,
    request: Request,
    db: AsyncSession = Depends(get_db),
    _admin: CurrentUser = Depends(require_admin),
) -> list[dict[str, Any]]:
    if await db.get(Node, node_id) is None:
        raise _error(404, "not_found", "unknown node")
    return await _tools(request).grants_on(db, node_id)


@router.put("/nodes/{node_id}/grants/{user_id}")
async def put_grants(
    node_id: uuid.UUID,
    user_id: uuid.UUID,
    body: GrantRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
    admin: CurrentUser = Depends(require_admin),
) -> dict[str, Any]:
    """Replace what one user may run on one node: capability names, or ``*`` for all read-only
    ones the node enables. An empty list takes every grant away."""
    node = await db.get(Node, node_id)
    if node is None or await db.get(User, user_id) is None:
        raise _error(404, "not_found", "unknown node or user")
    bad = [c for c in body.capabilities if c != ALL and not _valid_name(c)]
    if bad:
        raise _error(422, "invalid_capability", f"not a capability name: {bad[0]!r}")
    await _tools(request).set_grants(db, node_id, user_id, body.capabilities, admin.uuid)
    await request.app.state.nodes.record_event(
        node_id,
        "grants_changed",
        {"user_id": str(user_id), "capabilities": sorted(set(body.capabilities)), "by": admin.id},
    )
    return {"user_id": str(user_id), "capabilities": sorted(set(body.capabilities))}


def _valid_name(name: str) -> bool:
    import re

    return bool(re.fullmatch(r"[a-z][a-z0-9_]*(\.[a-z][a-z0-9_]*)+", name)) and len(name) <= 100


@router.get("/users")
async def list_users(
    db: AsyncSession = Depends(get_db),
    _admin: CurrentUser = Depends(require_admin),
) -> list[dict[str, str]]:
    rows = await db.scalars(select(User).order_by(User.name))
    return [{"id": str(u.id), "name": u.name, "role": u.role} for u in rows]


@router.get("/tool-calls")
async def tool_calls(
    request: Request,
    node_id: uuid.UUID | None = None,
    limit: int = Query(50, ge=1, le=500),
    db: AsyncSession = Depends(get_db),
    user: CurrentUser = Depends(api_user),
) -> list[dict[str, Any]]:
    """The audit log: a user's own calls, all of them for an admin (D20)."""
    return await _tools(request).recent_calls(db, Caller.of(user), node_id, limit)
