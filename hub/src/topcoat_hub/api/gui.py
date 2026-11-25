"""REST + Server-Sent Events for the web GUI."""

import asyncio
import json
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Path, Query, Request
from fastapi.responses import Response, StreamingResponse
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from topcoat_hub import __version__, clock, node_rename
from topcoat_hub.api.node import create_pairing_code
from topcoat_hub.api.scheduling import standing_roles
from topcoat_hub.auth import CurrentUser, api_user, require_admin
from topcoat_hub.build_info import BUILD
from topcoat_hub.config import Settings
from topcoat_hub.db import get_db
from topcoat_hub.gateway import split_model
from topcoat_hub.live import ActionTimeout, EventBus, NodeOffline, NodeReadOnly
from topcoat_hub.models import Node, NodeInventory, User
from topcoat_hub.protocol import DEFAULT_RESIDENT_PRIORITY, ProtocolError
from topcoat_hub.service import NodeService
from topcoat_hub.views import node_summary

router = APIRouter(prefix="/api")


class ActionRequest(BaseModel):
    name: str = Field(pattern=r"^[a-z][a-z0-9_]*(\.[a-z][a-z0-9_]*)+$", max_length=100)
    args: dict[str, Any] = Field(default_factory=dict)


class ResidentRequest(BaseModel):
    """A ``config.residents`` entry without the model name (that is the path parameter)."""

    model_config = ConfigDict(extra="forbid")

    priority: int = Field(DEFAULT_RESIDENT_PRIORITY, ge=0, le=100)
    context_length: int | None = Field(None, ge=1)
    auto_reload: bool = True


ModelName = Annotated[str, Path(min_length=1, max_length=255)]


def _service(request: Request) -> NodeService:
    return request.app.state.nodes


def _error(status: int, code: str, message: str) -> HTTPException:
    return HTTPException(status, detail={"code": code, "message": message})


@router.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok", "version": __version__, "build": BUILD}


class LanguageRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # None: follow the browser language again.
    language: Literal["en", "de"] | None


class TimezoneRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # an IANA name like Europe/Berlin; None: follow the hub's time zone again.
    timezone: str | None = Field(default=None, max_length=64)


async def _me(
    db: AsyncSession, user: CurrentUser, hub_timezone: str = clock.DEFAULT_ZONE
) -> dict[str, str | None]:
    row = await db.get(User, uuid.UUID(user.id))
    own = row.timezone if row else None
    return {
        "id": user.id,
        "name": user.name,
        "role": user.role,
        "language": row.language if row else None,
        # the zone the person chose (None = none) and the one that applies to them
        "timezone": own,
        "effective_timezone": str(clock.zone(own, hub_timezone)),
        "hub_timezone": hub_timezone,
    }


@router.get("/me")
async def me(
    request: Request, db: AsyncSession = Depends(get_db), user: CurrentUser = Depends(api_user)
) -> dict[str, str | None]:
    return await _me(db, user, request.app.state.settings.hub_timezone)


@router.put("/me/language")
async def put_my_language(
    body: LanguageRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
    user: CurrentUser = Depends(api_user),
) -> dict[str, str | None]:
    """Store the GUI language of the calling user (per user, not per browser)."""
    await db.execute(
        update(User).where(User.id == uuid.UUID(user.id)).values(language=body.language)
    )
    await db.commit()
    return await _me(db, user, request.app.state.settings.hub_timezone)


@router.put("/me/timezone")
async def put_my_timezone(
    body: TimezoneRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
    user: CurrentUser = Depends(api_user),
) -> dict[str, str | None]:
    """Store the time zone of the calling user (D4-4): their \"today\", reminders and quiet hours
    follow it. Empty = the hub's zone."""
    if body.timezone is not None and not clock.valid_zone(body.timezone):
        raise HTTPException(
            422, {"code": "invalid", "message": "timezone must be an IANA name like Europe/Berlin"}
        )
    await db.execute(
        update(User).where(User.id == uuid.UUID(user.id)).values(timezone=body.timezone)
    )
    await db.commit()
    return await _me(db, user, request.app.state.settings.hub_timezone)


@router.get("/nodes")
async def list_nodes(
    request: Request,
    db: AsyncSession = Depends(get_db),
    _user: CurrentUser = Depends(require_admin),
) -> list[dict[str, Any]]:
    sessions = request.app.state.sessions
    rows = await db.execute(
        select(Node, NodeInventory)
        .outerjoin(NodeInventory, NodeInventory.node_id == Node.id)
        .order_by(Node.name)
    )
    service = _service(request)
    return [
        node_summary(n, inv, sessions.is_connected(n.id), service.latest_release(n))
        for n, inv in rows.all()
    ]


def _role(key: str, requested: str | None) -> dict[str, str | None]:
    """One standing task of the hub with its model and the node it is pinned to (if any)."""
    model, node = split_model(requested) if requested else (None, None)
    return {"key": key, "model": model, "node": node}


@router.get("/activity")
async def activity(
    request: Request,
    db: AsyncSession = Depends(get_db),
    _user: CurrentUser = Depends(require_admin),
) -> dict[str, Any]:
    """What the nodes do for the hub right now, which model each standing task uses, and the
    state of the chat digest: the central view of the nodes page."""
    state = request.app.state
    sessions = state.sessions
    rows = await db.execute(
        select(Node, NodeInventory)
        .outerjoin(NodeInventory, NodeInventory.node_id == Node.id)
        .order_by(Node.name)
    )
    busy = state.gateway.activity()
    nodes = []
    for node, inv in rows.all():
        summary = node_summary(node, inv, sessions.is_connected(node.id))
        nodes.append(
            {
                "name": node.name,
                "state": summary["state"],
                "work": [b for b in busy if b["node"] == node.name],
                "queued": state.gateway.waiting(node.id),
                "waiting": state.gateway.waiting_jobs(node.id),
            }
        )
    roles = await standing_roles(state, db)
    return {
        "nodes": nodes,
        "roles": [_role(key, m) for key, m in roles.items()],
        "background_nodes": state.gateway.background_nodes,
        "digest": state.digester.status(),
        # requests that wait for a node of the pool, in the order they will be served
        "pool_waiting": state.gateway.pool_waiting_jobs(),
    }


@router.get("/nodes/{node_id}")
async def get_node(
    node_id: uuid.UUID,
    request: Request,
    db: AsyncSession = Depends(get_db),
    _user: CurrentUser = Depends(require_admin),
) -> dict[str, Any]:
    service = _service(request)
    summary = await service.summary(db, node_id)
    if summary is None:
        raise _error(404, "not_found", "unknown node")
    node = await db.get(Node, node_id)
    assert node is not None  # noqa: S101 - summary() found it
    return {
        **summary,
        "config": node.config,
        "config_hash": node.config_hash,
        "events": await service.recent_events(db, node_id, 50),
        "benchmarks": await service.recent_benchmarks(db, node_id, 50),
    }


@router.get("/nodes/{node_id}/benchmarks")
async def node_benchmarks(
    node_id: uuid.UUID,
    request: Request,
    db: AsyncSession = Depends(get_db),
    _user: CurrentUser = Depends(require_admin),
) -> list[dict[str, Any]]:
    if await db.get(Node, node_id) is None:
        raise _error(404, "not_found", "unknown node")
    return await _service(request).recent_benchmarks(db, node_id, 200)


@router.put("/nodes/{node_id}/config")
async def put_node_config(
    node_id: uuid.UUID,
    request: Request,
    db: AsyncSession = Depends(get_db),
    user: CurrentUser = Depends(require_admin),
) -> dict[str, Any]:
    """Replace a node's desired config (backend, llama.cpp pin, watcher rules, ...).

    The body is the full config object; it is validated against node_config.schema.json
    and pushed to the node if it is connected.
    """
    if await db.get(Node, node_id) is None:
        raise _error(404, "not_found", "unknown node")
    await db.close()
    try:
        config = await request.json()
        request.app.state.schemas.validate("node_config", config)
    except ValueError as exc:  # also ProtocolError
        message = str(exc) if isinstance(exc, ProtocolError) else "body is not valid JSON"
        raise _error(400, "invalid_config", message) from exc

    service = _service(request)
    new_hash = await service.set_config(node_id, config)
    if new_hash is None:  # deleted concurrently
        raise _error(404, "not_found", "unknown node")
    await service.record_event(node_id, "config_changed", {"user_id": user.id})
    await service.publish_node(node_id)
    return {"config": config, "config_hash": new_hash}


async def _change_config(
    request: Request, node_id: uuid.UUID, mutate: Callable[[dict[str, Any]], bool]
) -> tuple[dict[str, Any], str, bool]:
    """Apply ``mutate`` to the stored config; the result must still match the schema."""
    schemas = request.app.state.schemas

    def checked(config: dict[str, Any]) -> bool:
        if not mutate(config):
            return False
        schemas.validate("node_config", config)
        return True

    try:
        return await _service(request).change_config(node_id, checked)
    except LookupError as exc:
        raise _error(404, "not_found", "unknown node") from exc
    except ProtocolError as exc:
        raise _error(400, "invalid_config", str(exc)) from exc


@router.put("/nodes/{node_id}/residents/{model}")
async def put_resident(
    node_id: uuid.UUID,
    model: ModelName,
    body: ResidentRequest,
    request: Request,
    user: CurrentUser = Depends(require_admin),
) -> dict[str, Any]:
    """Keep ``model`` loaded on the node ("Automatisch bereithalten", E1-06).

    Creates or replaces the model's ``config.residents`` entry; the body carries the whole
    entry (missing fields get their defaults). The model does not have to be on disk yet.
    """
    entry: dict[str, Any] = {
        "model": model,
        "priority": body.priority,
        "auto_reload": body.auto_reload,
    }
    if body.context_length is not None:
        entry["context_length"] = body.context_length

    def mutate(config: dict[str, Any]) -> bool:
        residents = config.setdefault("residents", [])
        for i, r in enumerate(residents):
            if r.get("model") == model:
                if r == entry:
                    return False
                residents[i] = entry
                return True
        residents.append(entry)
        return True

    config, new_hash, changed = await _change_config(request, node_id, mutate)
    if changed:
        service = _service(request)
        data = {"source": "residents", "model": model, "resident": entry, "user_id": user.id}
        await service.record_event(node_id, "config_changed", data)
        await service.publish_node(node_id)
    return {"config": config, "config_hash": new_hash, "changed": changed}


@router.delete("/nodes/{node_id}/residents/{model}")
async def delete_resident(
    node_id: uuid.UUID,
    model: ModelName,
    request: Request,
    user: CurrentUser = Depends(require_admin),
) -> dict[str, Any]:
    """Stop keeping ``model`` loaded. The node does not unload it; idle unload applies again."""

    def mutate(config: dict[str, Any]) -> bool:
        residents = config.get("residents", [])
        kept = [r for r in residents if r.get("model") != model]
        config["residents"] = kept
        return len(kept) != len(residents)

    config, new_hash, changed = await _change_config(request, node_id, mutate)
    if changed:
        service = _service(request)
        data = {"source": "residents", "model": model, "resident": None, "user_id": user.id}
        await service.record_event(node_id, "config_changed", data)
        await service.publish_node(node_id)
    return {"config": config, "config_hash": new_hash, "changed": changed}


def _acted_on(action: ActionRequest, result: dict[str, Any]) -> list[str]:
    """Models a successful ``models.load`` / ``models.unload`` acted on."""
    if action.name == "models.unload":
        # Nodes list what they unloaded; that also covers {"all": true}.
        unloaded = (result.get("result") or {}).get("unloaded")
        if isinstance(unloaded, list):
            return [m for m in unloaded if isinstance(m, str)]
    model = action.args.get("model")
    return [model] if isinstance(model, str) else []


async def _follow_manual_action(
    request: Request,
    node_id: uuid.UUID,
    action: ActionRequest,
    result: dict[str, Any],
    user_id: str,
) -> None:
    """A manual unload clears ``auto_reload`` of resident models (the user meant it); a
    manual load of a resident model sets it again (E1-06)."""
    if not result.get("ok") or action.name not in ("models.load", "models.unload"):
        return
    models = set(_acted_on(action, result))
    if not models:
        return
    value = action.name == "models.load"
    changed: list[str] = []

    def mutate(config: dict[str, Any]) -> bool:
        for r in config.get("residents", []):
            if r.get("model") in models and r.get("auto_reload", True) != value:
                r["auto_reload"] = value
                changed.append(r["model"])
        return bool(changed)

    try:
        _, _, did_change = await _change_config(request, node_id, mutate)
    except HTTPException:  # node deleted or config broken meanwhile; the action itself is done
        return
    if did_change:
        service = _service(request)
        source = "manual_load" if value else "manual_unload"
        data = {"source": source, "models": changed, "auto_reload": value, "user_id": user_id}
        await service.record_event(node_id, "config_changed", data)
        await service.publish_node(node_id)


@router.get("/nodes/{node_id}/events")
async def node_events(
    node_id: uuid.UUID,
    request: Request,
    limit: int = Query(100, ge=1, le=1000),
    db: AsyncSession = Depends(get_db),
    _user: CurrentUser = Depends(require_admin),
) -> list[dict[str, Any]]:
    if await db.get(Node, node_id) is None:
        raise _error(404, "not_found", "unknown node")
    return await _service(request).recent_events(db, node_id, limit)


def _action_timeout(settings: Settings, name: str) -> float:
    """Loading a big model takes minutes (30 s to 2.5 min measured for a 30B on a laptop CPU),
    so ``models.load`` waits as long as the gateway does; other actions answer at once."""
    if name == "models.load":
        return max(settings.action_timeout_seconds, settings.gateway_load_timeout_seconds)
    return settings.action_timeout_seconds


@router.post("/nodes/{node_id}/actions")
async def trigger_action(
    node_id: uuid.UUID,
    action: ActionRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
    user: CurrentUser = Depends(require_admin),
) -> dict[str, Any]:
    node = await db.get(Node, node_id)
    if node is None:
        raise _error(404, "not_found", "unknown node")
    await db.close()  # don't hold a connection while waiting for the node
    # Defence in depth: the node enforces its whitelist too.
    if action.name not in node.config.get("allowed_actions", []):
        raise _error(403, "not_allowed", f"action {action.name!r} is not allowed on this node")

    state = request.app.state
    try:
        result = await state.sessions.dispatch(
            node_id, action.name, action.args, _action_timeout(state.settings, action.name)
        )
    except NodeOffline as exc:
        raise _error(409, "node_offline", "node is not connected") from exc
    except NodeReadOnly as exc:
        raise _error(409, "upgrade_required", "node speaks an unsupported protocol") from exc
    except ActionTimeout as exc:
        raise _error(504, "timeout", "node did not answer in time") from exc

    await _service(request).record_event(
        node_id,
        "action",
        {
            "name": action.name,
            "args": action.args,
            "user_id": user.id,
            "ok": result["ok"],
            "error": result.get("error"),
        },
    )
    await _follow_manual_action(request, node_id, action, result, user.id)
    return result


class RenameRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=63)


@router.post("/nodes/{node_id}/rename")
async def rename_node(
    node_id: uuid.UUID,
    body: RenameRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
    user: CurrentUser = Depends(require_admin),
) -> dict[str, Any]:
    """Rename a node (an admin's click): the living settings follow, the history keeps the old
    name, and the old name still leads to the node (node_rename.py). The node stays connected."""
    try:
        result = await node_rename.rename(db, node_id, body.name, user.uuid)
    except node_rename.RenameError as exc:
        raise _error(exc.status, exc.code, exc.message) from exc
    await node_rename.load_aliases(request.app.state.sessionmaker)
    return result


@router.post("/nodes/{node_id}/wake")
async def wake_node(
    node_id: uuid.UUID,
    request: Request,
    db: AsyncSession = Depends(get_db),
    user: CurrentUser = Depends(require_admin),
) -> dict[str, Any]:
    """Wake a node whose machine sleeps or is off (Wake-on-LAN, E2-05 E): a magic packet to
    every wired address of its last inventory (wireless ones only if there is no other)."""
    node = await db.get(Node, node_id)
    if node is None:
        raise _error(404, "not_found", "unknown node")
    state = request.app.state
    if state.sessions.is_connected(node_id):
        raise _error(409, "already_online", "the node is connected; it does not need waking")
    inventory = await db.get(NodeInventory, node_id)
    interfaces = ((inventory.inventory if inventory else None) or {}).get("hardware", {}).get(
        "network"
    ) or []
    wired = [i for i in interfaces if i.get("kind") != "wifi"] or interfaces
    macs = [i["mac"] for i in wired if isinstance(i.get("mac"), str)]
    if not macs:
        raise _error(
            409,
            "no_mac",
            "the hub does not know a network address of this node yet (it reports them "
            "while it is online)",
        )
    await db.close()
    settings = state.settings
    try:
        for mac in macs:
            await state.wake.send(mac, settings.wol_broadcast, settings.wol_port)
    except OSError as exc:
        raise _error(502, "wake_failed", f"the magic packet could not be sent: {exc}") from exc
    await _service(request).record_event(
        node_id, "wake_sent", {"user_id": user.id, "addresses": len(macs)}
    )
    return {"sent": len(macs), "broadcast": settings.wol_broadcast, "port": settings.wol_port}


@router.post("/pairing-codes", status_code=201)
async def new_pairing_code(
    request: Request,
    db: AsyncSession = Depends(get_db),
    user: CurrentUser = Depends(require_admin),
) -> dict[str, str | None]:
    state = request.app.state
    code: dict[str, str | None] = dict(
        await create_pairing_code(db, user.id, state.settings.pairing_code_ttl_seconds)
    )
    # What the GUI needs to print the pair command: the URL nodes should use (None: the
    # address the GUI was opened with) and the CA fingerprint to pin (None: system trust).
    code["hub_url"] = state.settings.public_url.rstrip("/") or None
    code["ca_fingerprint"] = state.tls.ca_fingerprint
    return code


@router.get("/tls")
async def tls_info(request: Request) -> dict[str, str | bool | None]:
    """TLS mode and CA fingerprint. No auth: both are public information."""
    material = request.app.state.tls
    return {
        "mode": material.mode,
        "ca_fingerprint": material.ca_fingerprint,
        "ca_available": material.ca_pem is not None,
    }


@router.get("/tls/ca.pem")
async def tls_ca(request: Request) -> Response:
    """The hub CA certificate, for installing it in a browser or OS trust store."""
    material = request.app.state.tls
    if material.ca_pem is None:
        raise HTTPException(404, detail={"code": "not_found", "message": "no CA certificate"})
    return Response(
        material.ca_pem,
        media_type="application/x-pem-file",
        headers={"Content-Disposition": 'attachment; filename="topcoat-hub-ca.pem"'},
    )


def _sse(event: str, data: dict[str, Any]) -> str:
    return f"event: {event}\ndata: {json.dumps(data, separators=(',', ':'))}\n\n"


async def sse_stream(
    bus: EventBus,
    is_disconnected: Callable[[], Awaitable[bool]],
    keepalive_seconds: float,
    user_id: str | None = None,
) -> AsyncIterator[str]:
    """Yield SSE frames from the bus until the client goes away. An event that belongs to one
    user (``_user`` in its data: the agent's task events, D20) goes to that user's streams only,
    and ``_user`` is not sent."""
    queue = bus.subscribe()
    try:
        yield "retry: 3000\n: connected\n\n"
        while not await is_disconnected():
            try:
                event, data = await asyncio.wait_for(queue.get(), keepalive_seconds)
            except TimeoutError:
                yield ": ping\n\n"
                continue
            owner = data.get("_user")
            if owner is not None:
                if owner != user_id:
                    continue
                data = {k: v for k, v in data.items() if k != "_user"}
            yield _sse(event, data)
    finally:
        bus.unsubscribe(queue)


@router.get("/stream")
async def stream(request: Request, user: CurrentUser = Depends(require_admin)) -> StreamingResponse:
    state = request.app.state
    return StreamingResponse(
        sse_stream(
            state.bus, request.is_disconnected, state.settings.sse_keepalive_seconds, user.id
        ),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
