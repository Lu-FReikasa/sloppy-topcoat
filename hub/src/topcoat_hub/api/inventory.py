"""The inventory over REST (E3-10): what runs on which node, how old that knowledge is, a refresh.

Who may see a node's inventory is decided in :mod:`topcoat_hub.inventory` (the right to run
``inventory.snapshot`` there, D20); a node a user may not see does not exist for them (404)."""

import uuid
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request

from topcoat_hub.auth import CurrentUser, api_user
from topcoat_hub.inventory import InventoryService
from topcoat_hub.proposals import MANUAL_CAPABILITIES
from topcoat_hub.tools import Caller

router = APIRouter(prefix="/api/inventory")


def _inventory(request: Request) -> InventoryService:
    return request.app.state.inventory


def _error(status: int, code: str, message: str) -> HTTPException:
    return HTTPException(status, detail={"code": code, "message": message})


# what the inventory page offers next to a container or a stack, if the caller may run it
LOG_CAPABILITIES = ("docker.logs", "compose.logs")


async def _with_actions(
    request: Request, caller: Caller, views: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """Add to every node what *this caller* may do there right now: ``can.read`` (logs, only
    capabilities they may run on a connected node) and ``can.change`` (restart buttons, only where
    proposals are on and the node offers the change). Nothing is revealed that a click would not
    allow anyway; the click is checked again by the services."""
    tools = request.app.state.tools
    readable: dict[str, list[str]] = {}
    for t in await tools.usable_tools(caller):
        if t["capability"] in LOG_CAPABILITIES:
            readable.setdefault(t["node_id"], []).append(t["capability"])
    changeable: dict[str, list[str]] = {}
    if request.app.state.proposals.enabled and caller.via == "gui":
        for t in await tools.proposable_tools(caller):
            if t["capability"] in MANUAL_CAPABILITIES:
                changeable.setdefault(t["node_id"], []).append(t["capability"])
    for v in views:
        nid = v["node"]["id"]
        v["can"] = {
            "read": sorted(readable.get(nid, [])),
            "change": sorted(changeable.get(nid, [])),
        }
    return views


@router.get("")
async def list_inventory(request: Request, user: CurrentUser = Depends(api_user)) -> dict[str, Any]:
    """Every node this user may see, with its containers, compose projects and services and the
    age of each look. ``stale_after_seconds`` tells when a look stops being current."""
    svc = _inventory(request)
    caller = Caller.of(user)
    return {
        "nodes": await _with_actions(request, caller, await svc.view(caller)),
        "stale_after_seconds": svc.stale_after(),
    }


@router.get("/nodes/{node_id}")
async def node_inventory(
    node_id: uuid.UUID, request: Request, user: CurrentUser = Depends(api_user)
) -> dict[str, Any]:
    caller = Caller.of(user)
    views = await _inventory(request).view(caller, node_id)
    if not views:
        raise _error(404, "not_found", "unknown node")
    return (await _with_actions(request, caller, views))[0]


@router.post("/nodes/{node_id}/refresh")
async def refresh_node(
    node_id: uuid.UUID, request: Request, user: CurrentUser = Depends(api_user)
) -> dict[str, Any]:
    """Read the node now (read-only; at most one read per few seconds, a second request gets the
    result of the first). A node that is not connected answers 409 and keeps what it had."""
    svc, caller = _inventory(request), Caller.of(user)
    if not await svc.view(caller, node_id):
        raise _error(404, "not_found", "unknown node")
    if request.app.state.sessions.get(node_id) is None:
        raise _error(409, "node_offline", "the node is not connected")
    await svc.collect(node_id)
    await request.app.state.nodes.record_event(
        node_id, "inventory_refresh", {"by": user.id, "via": caller.via}
    )
    views = await _with_actions(request, caller, await svc.view(caller, node_id))
    return views[0]
