"""Node-facing endpoints: pairing and the node WebSocket (docs/PROTOCOL.md)."""

import asyncio
import contextlib
import json
import logging
import secrets
import uuid
from collections.abc import Mapping
from datetime import timedelta
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request, WebSocket, WebSocketDisconnect
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from topcoat_hub.auth import hash_secret
from topcoat_hub.db import get_db
from topcoat_hub.gateway import spawn
from topcoat_hub.house_residents import keep_house_resident
from topcoat_hub.live import NodeSession
from topcoat_hub.models import Node, NodeAlias, PairingCode
from topcoat_hub.nodeauth import parse_authorization, signed_message, verify_signature
from topcoat_hub.protocol import (
    PROTOCOL_VERSION,
    SUPPORTED_VERSIONS,
    ProtocolError,
    config_hash,
    default_build_config,
    default_dev_config,
    default_node_config,
    default_tools_config,
    envelope,
    parse_ts,
)
from topcoat_hub.service import NodeService, utcnow

log = logging.getLogger(__name__)
router = APIRouter()

CODE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"  # no 0/O, 1/I lookalikes

# WebSocket close codes (4000-4999 are application-defined).
CLOSE_UNAUTHORIZED = 4401
CLOSE_PROTOCOL = 4400
CLOSE_REPLACED = 4409
CLOSE_HELLO_TIMEOUT = 4408

# Messages a node may send after hello.
NODE_MESSAGE_TYPES = {
    "inventory",
    "heartbeat",
    "action_result",
    "event",
    "queued_events",
    "proxy_chunk",
    "proxy_end",
    "proxy_status",
}


def new_pairing_code() -> str:
    raw = "".join(secrets.choice(CODE_ALPHABET) for _ in range(8))
    return f"{raw[:4]}-{raw[4:]}"


def _http_error(status: int, code: str, message: str) -> HTTPException:
    return HTTPException(status, detail={"code": code, "message": message})


@router.post("/api/node/pair")
async def pair(request: Request, db: AsyncSession = Depends(get_db)) -> dict[str, str]:
    """Exchange a one-time pairing code for a node identity (the node keeps its private key)."""
    schemas = request.app.state.schemas
    try:
        body = await request.json()
        schemas.validate("pair_request", body)
    except (ValueError, ProtocolError) as exc:
        raise _http_error(400, "invalid_request", str(exc)) from exc

    code = await db.scalar(
        select(PairingCode)
        .where(PairingCode.code_hash == hash_secret(body["code"]))
        .with_for_update()
    )
    if code is None:
        raise _http_error(404, "invalid_code", "unknown pairing code")
    if code.used_at is not None or code.expires_at <= utcnow():
        raise _http_error(410, "code_expired", "pairing code expired or already used")
    if await db.scalar(select(Node.id).where(Node.name == body["name"])) is not None:
        raise _http_error(409, "name_taken", f"a node named {body['name']!r} already exists")
    if await db.get(NodeAlias, body["name"]) is not None:  # an earlier name still leads to a node
        raise _http_error(
            409, "name_taken", f"{body['name']!r} was the name of a node and still leads to it"
        )

    settings = request.app.state.settings
    if body.get("role") == "tools":
        config = default_tools_config(settings.heartbeat_interval_seconds)
    elif body.get("role") == "dev":
        config = default_dev_config(settings.heartbeat_interval_seconds)
    elif body.get("role") == "build":
        config = default_build_config(settings.heartbeat_interval_seconds)
    else:
        config = default_node_config(body["os"], settings.heartbeat_interval_seconds)
    node = Node(
        id=uuid.uuid4(),
        name=body["name"],
        os=body["os"],
        arch=body["arch"],
        public_key=body["public_key"],
        state="offline",
        agent_version=body.get("agent_version"),
        config=config,
        config_hash=config_hash(config),
    )
    db.add(node)
    await db.flush()
    code.used_at = utcnow()
    code.node_id = node.id
    await db.commit()

    service: NodeService = request.app.state.nodes
    await service.record_event(node.id, "paired", {"name": node.name, "user_id": str(code.user_id)})
    await service.publish_node(node.id)
    response = {"node_id": str(node.id)}
    schemas.validate("pair_response", response)
    return response


@router.post("/api/node/challenge")
async def challenge(request: Request) -> dict[str, Any]:
    """Hand out a single-use nonce for the WebSocket authentication (nodeauth.py)."""
    schemas = request.app.state.schemas
    try:
        body = await request.json()
        schemas.validate("challenge_request", body)
    except (ValueError, ProtocolError) as exc:
        raise _http_error(400, "invalid_request", str(exc)) from exc
    store = request.app.state.challenges
    nonce = store.issue(uuid.UUID(body["node_id"]))
    response = {"nonce": nonce, "expires_in": int(store.ttl_seconds)}
    schemas.validate("challenge_response", response)
    return response


async def authenticate_node(state: Any, headers: Mapping[str, str]) -> Node | None:
    """Check the signed nonce of a request (WebSocket upgrade or node download); None refuses."""
    auth = parse_authorization(headers)
    if auth is None:
        return None
    if not state.challenges.consume(auth.node_id, auth.nonce):
        return None
    async with state.sessionmaker() as db:
        node = await db.get(Node, auth.node_id)
    if node is None:
        return None
    if not verify_signature(
        node.public_key, signed_message(str(auth.node_id), auth.nonce), auth.signature
    ):
        log.warning("node %s: invalid signature", auth.node_id)
        return None
    return node


async def _authenticate(ws: WebSocket) -> Node | None:
    return await authenticate_node(ws.app.state, ws.headers)


async def authenticated_node(request: Request) -> Node:
    """Dependency of the node-facing HTTP endpoints."""
    node = await authenticate_node(request.app.state, request.headers)
    if node is None:
        raise HTTPException(
            401, detail={"code": "unauthorized", "message": "missing or invalid node signature"}
        )
    return node


async def create_pairing_code(db: AsyncSession, user_id: str, ttl_seconds: int) -> dict[str, str]:
    code = new_pairing_code()
    expires_at = utcnow() + timedelta(seconds=ttl_seconds)
    db.add(
        PairingCode(code_hash=hash_secret(code), user_id=uuid.UUID(user_id), expires_at=expires_at)
    )
    await db.commit()
    return {"code": code, "expires_at": expires_at.isoformat()}


async def _send_error(
    session_ws: WebSocket, code: str, message: str, re: str | None, v: int = PROTOCOL_VERSION
) -> None:
    await session_ws.send_text(
        json.dumps(envelope("error", {"code": code, "message": message}, re, v))
    )


@router.websocket("/api/node/ws")
async def node_ws(ws: WebSocket) -> None:
    state = ws.app.state
    service: NodeService = state.nodes
    schemas = state.schemas

    node = await _authenticate(ws)
    if node is None:
        await ws.close(code=CLOSE_UNAUTHORIZED)  # before accept(): handshake gets HTTP 403
        return
    await ws.accept()

    try:
        hello = json.loads(
            await asyncio.wait_for(ws.receive_text(), state.settings.hello_timeout_seconds)
        )
    except TimeoutError:
        await ws.close(code=CLOSE_HELLO_TIMEOUT)
        return
    except (WebSocketDisconnect, ValueError):
        await ws.close(code=CLOSE_PROTOCOL)
        return

    session = NodeSession(
        node_id=node.id, websocket=ws, peer_ip=ws.client.host if ws.client else None
    )
    version = hello.get("v") if isinstance(hello, dict) else None
    reject: str | None = None
    if version not in SUPPORTED_VERSIONS and isinstance(version, int):
        # Stay connected read-only: no config, no actions (docs/PROTOCOL.md "Versioning"). The
        # refusal is sent after the state is stored (below), so whoever acts on it reads "degraded".
        session.read_only = True
        reject = f"protocol version {version} not supported"
    else:
        try:
            if isinstance(version, int):
                session.version = version
            state.schema_sets.get(version, schemas).validate_message(hello)
            if hello["type"] != "hello":
                raise ProtocolError("first message must be hello")
            if hello["body"]["node_id"] != str(node.id):
                raise ProtocolError("node_id does not match the authenticated node")
        except ProtocolError as exc:
            await _send_error(ws, "invalid_message", str(exc), None, session.version)
            await ws.close(code=CLOSE_PROTOCOL)
            return

    config, config_hash_ = node.config, node.config_hash
    if not session.read_only:
        # before the session is registered, so the change is not pushed twice
        config, config_hash_ = await _keep_house_model(state, node)

    previous = state.sessions.add(session)
    if previous is not None:
        previous.fail_all()
        await _close_quietly(previous.websocket, CLOSE_REPLACED)

    values: dict[str, Any] = {"last_seen": utcnow(), "state": "degraded"}
    if not session.read_only:
        values.update(state="available", agent_version=hello["body"]["agent_version"])
    await service.update_node(node.id, **values)
    await service.record_event(
        node.id, "connected", {"read_only": session.read_only, "protocol_version": session.version}
    )
    if reject is not None:
        await _send_error(ws, "upgrade_required", reject, None)
    if session.version != PROTOCOL_VERSION and not session.read_only:
        log.warning(
            "node %s speaks protocol v%d; the hub speaks v%d and accepts v%d for one more "
            "release only: update the node",
            node.name,
            session.version,
            PROTOCOL_VERSION,
            session.version,
        )
    log.info("node %s (%s) connected", node.name, node.id)

    try:
        if not session.read_only:
            await session.send(
                envelope("config", {"config_hash": config_hash_, "config": config})
            )
        await service.publish_node(node.id)
        while True:
            try:
                raw = await ws.receive_text()
            except RuntimeError:
                break  # we closed the socket ourselves (session replaced)
            await _handle_message(session, raw)
    except WebSocketDisconnect:
        pass
    finally:
        # Shielded: the cleanup must finish even if the handler task is being cancelled. It is a
        # task of the hub (``spawn``), so the shutdown ends it before the engine goes: a cleanup
        # dropped in the middle of its query kept its connection "idle in transaction" and hung
        # the next test's TRUNCATE (the last leak of the suite hang).
        cleanup = spawn(_on_disconnect(session))
        await asyncio.shield(cleanup)


async def _keep_house_model(state: Any, node: Node) -> tuple[dict[str, Any], str]:
    """Make the node's house model an auto-reloading resident again (plan step 1.6) and return
    the config to send. A failure leaves the stored config as it was: the node still connects."""
    gateway = getattr(state, "gateway", None)
    model = gateway.policy.house.get(node.name) if gateway is not None else None
    if not model:
        return node.config, node.config_hash
    try:
        config, hash_, _ = await keep_house_resident(
            state.nodes, state.schemas, node.id, model, source="house_reconnect"
        )
    except (LookupError, ProtocolError) as exc:
        log.warning("node %s: could not keep house model %s: %s", node.name, model, exc)
        return node.config, node.config_hash
    return config, hash_


async def _on_disconnect(session: NodeSession) -> None:
    state = session.websocket.app.state
    if state.sessions.remove(session):
        state.metrics.forget(session.node_id)
        service: NodeService = state.nodes
        await service.update_node(session.node_id, state="offline")
        await service.record_event(session.node_id, "disconnected", {})
        await service.publish_node(session.node_id)
        log.info("node %s disconnected", session.node_id)


async def _close_quietly(ws: WebSocket, code: int) -> None:
    with contextlib.suppress(RuntimeError, OSError):
        await ws.close(code=code)


async def _handle_message(session: NodeSession, raw: str) -> None:
    state = session.websocket.app.state
    service: NodeService = state.nodes
    node_id = session.node_id

    if session.read_only:
        await service.update_node(node_id, last_seen=utcnow())
        return

    msg_id: str | None = None
    try:
        msg = json.loads(raw)
        msg_id = msg.get("id") if isinstance(msg, dict) else None
        state.schema_sets[session.version].validate_message(msg)
        if msg["type"] not in NODE_MESSAGE_TYPES:
            raise ProtocolError(f"nodes may not send {msg['type']!r}")
    except (ValueError, ProtocolError) as exc:
        log.warning("invalid message from node %s: %s", node_id, exc)
        await session.send(
            envelope("error", {"code": "invalid_message", "message": str(exc)}, msg_id)
        )
        return

    if msg["type"] in ("proxy_chunk", "proxy_end", "proxy_status"):
        # Gateway traffic is hot: no DB write per chunk (last_seen comes with heartbeats).
        session.deliver(msg)
        return

    body = msg["body"]
    changed = False
    values: dict[str, Any] = {"last_seen": utcnow()}
    match msg["type"]:
        case "inventory":
            await service.store_inventory(node_id, inventory=body)
            changed = True
        case "heartbeat":
            await service.store_inventory(node_id, status=body)
            state.metrics.record(node_id, body)
            values["state"] = body["state"]
            changed = True
        case "action_result":
            if not session.resolve(msg):
                # The hub gave up waiting (a big model takes minutes to load); keep the outcome.
                name = session.pop_late(msg.get("re", ""))
                if name is not None:
                    await service.record_event(
                        node_id,
                        "action_late_result",
                        {"name": name, "ok": body["ok"], "error": body.get("error")},
                    )
        case "event":
            await _store_event(service, node_id, msg)
        case "queued_events":
            for event in body["events"]:
                await _store_event(service, node_id, event)
    await service.update_node(node_id, **values)
    if changed:
        await service.publish_node(node_id)


async def _store_event(service: NodeService, node_id: uuid.UUID, msg: dict[str, Any]) -> None:
    body = msg["body"]
    kind, data = body["kind"], body["data"]
    is_new = await service.record_event(
        node_id, kind, data, ts=parse_ts(msg["ts"]), message_id=msg["id"]
    )
    # Side effects only for the first delivery; replays of queued events are no-ops.
    if is_new and kind in ("transfer_ready", "transfer_progress") and service.transfers:
        await service.transfers.on_event(node_id, kind, data)  # model transfers (E2-07, D51)
        return
    if not (is_new and kind == "benchmark_finished" and "recommended_threads" in data):
        return
    threads = data["recommended_threads"]
    if await service.apply_recommended_threads(node_id, threads):
        await service.record_event(
            node_id, "config_changed", {"source": "benchmark", "threads": threads}
        )
        await service.publish_node(node_id)
