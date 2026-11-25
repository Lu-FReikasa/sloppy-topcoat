"""Direct model transfers between two nodes (E2-07, ARCHITECTURE D51, docs/PROTOCOL.md).

The hub only coordinates; the model file travels from node to node and never touches the hub:

1. ``create``: both nodes are checked, a row is stored, a one-time token is made (memory only)
   and ``models.send`` goes to the source.
2. The source answers with the ``transfer_ready`` event (port, addresses, hash, certificate
   fingerprint); the hub sends ``models.receive`` with the endpoints to the target.
3. Both nodes report ``transfer_progress``; the target's ``done`` ends the transfer. With
   ``move`` the hub then tells the source to delete its copy (``models.delete``, confirm).

The token is a secret of one transfer: it is never stored, never logged and never part of a view.
Every row carries a ``user_id`` and every query of the store is scoped by it (D20).
"""

import asyncio
import ipaddress
import logging
import secrets
import time
import uuid
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from topcoat_hub.gateway import spawn
from topcoat_hub.live import ActionTimeout, EventBus, NodeOffline, NodeReadOnly, NodeSessions
from topcoat_hub.models import ModelTransfer, Node, NodeInventory

log = logging.getLogger(__name__)

ACTIVE = ("pending", "preparing", "ready", "transferring")
FINISHED = ("done", "failed", "canceled")
# error codes a node reports (docs/PROTOCOL.md); the hub adds "interrupted" (restart)
NODE_ERROR_CODES = {
    "no_route",
    "hash_mismatch",
    "insufficient_disk",
    "timeout",
    "canceled",
    "busy",
    "internal",
}
# how a refused action of the source (or the hub's own failure) answers the REST call:
# node error code -> (HTTP status, REST error code)
REFUSALS = {
    "busy": (409, "busy"),
    "node_offline": (409, "node_offline"),
    "not_allowed": (403, "not_allowed"),
    "invalid_args": (409, "model_missing"),
    "timeout": (504, "timeout"),
}
STORED_CODES = NODE_ERROR_CODES | {"interrupted"}
ONE_GIB = 1024**3
MAX_ENDPOINTS = 16
# hint for the person when the hub itself gives up; the code stays machine-readable
ERROR_TEXT_MAX = 500


class TransferError(Exception):
    """A request the hub refuses; ``code`` is the error code of the REST answer."""

    def __init__(self, status: int, code: str, message: str) -> None:
        super().__init__(message)
        self.status, self.code, self.message = status, code, message


def utcnow() -> datetime:
    return datetime.now(UTC)


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


def view(row: ModelTransfer) -> dict[str, Any]:
    """The JSON shape of a transfer (the REST contract). It has no token, by construction."""
    return {
        "id": str(row.id),
        "user_id": str(row.user_id),
        "source_node_id": str(row.source_node_id),
        "target_node_id": str(row.target_node_id),
        "model": row.model,
        "target_name": row.target_name,
        "size": row.size,
        "sha256": row.sha256,
        "move": row.move,
        "status": row.status,
        "error_code": row.error_code,
        "error": row.error,
        "created_at": _iso(row.created_at),
        "updated_at": _iso(row.updated_at),
    }


class ModelTransferStore:
    """Rows of the model transfers of one person: every query is scoped by ``user_id``."""

    def __init__(self, db: AsyncSession, user_id: uuid.UUID) -> None:
        self.db = db
        self.user_id = user_id

    async def add(self, row: ModelTransfer) -> ModelTransfer:
        row.user_id = self.user_id
        self.db.add(row)
        await self.db.flush()
        return row

    async def get(self, transfer_id: uuid.UUID) -> ModelTransfer | None:
        return await self.db.scalar(
            select(ModelTransfer).where(
                ModelTransfer.id == transfer_id, ModelTransfer.user_id == self.user_id
            )
        )

    async def list(
        self, node_id: uuid.UUID | None = None, limit: int = 100
    ) -> Sequence[ModelTransfer]:
        stmt = select(ModelTransfer).where(ModelTransfer.user_id == self.user_id)
        if node_id is not None:
            stmt = stmt.where(
                (ModelTransfer.source_node_id == node_id)
                | (ModelTransfer.target_node_id == node_id)
            )
        rows = await self.db.scalars(
            stmt.order_by(ModelTransfer.created_at.desc(), ModelTransfer.id).limit(limit)
        )
        return rows.all()

    async def update(self, transfer_id: uuid.UUID, **values: Any) -> ModelTransfer | None:
        """Change a row of this person; None when it is not theirs (or does not exist)."""
        row = await self.get(transfer_id)
        if row is None:
            return None
        for key, value in values.items():
            setattr(row, key, value)
        row.updated_at = utcnow()
        await self.db.flush()
        return row


@dataclass
class _Active:
    """What the hub remembers about a running transfer, in memory only."""

    transfer_id: uuid.UUID
    user_id: uuid.UUID
    source_id: uuid.UUID
    target_id: uuid.UUID
    token: str = field(repr=False)  # a secret: not in repr, logs, rows or views
    target_name: str
    move: bool
    target_candidates: list[str]  # IPs the source accepts the single peer from
    last_progress: float = field(default_factory=time.monotonic)
    watcher: "asyncio.Future[None] | None" = field(default=None, repr=False)


def _ip(value: Any) -> str | None:
    """A normalised IP address, or None for anything that is not one."""
    try:
        return str(ipaddress.ip_address(str(value).strip()))
    except ValueError:
        return None


def _unique_ips(values: Any, limit: int) -> list[str]:
    out: list[str] = []
    for value in values if isinstance(values, list) else []:
        ip = _ip(value)
        if ip is not None and ip not in out:
            out.append(ip)
    return out[:limit]


def _endpoint(ip: str, port: int) -> str:
    return f"[{ip}]:{port}" if ":" in ip else f"{ip}:{port}"


def _error_text(message: Any) -> str | None:
    return str(message)[:ERROR_TEXT_MAX] if message else None


class TransferCoordinator:
    """The state machine of the direct model transfers."""

    def __init__(
        self,
        sessionmaker: async_sessionmaker[AsyncSession],
        sessions: NodeSessions,
        bus: EventBus,
        action_timeout_seconds: float = 30.0,
        timeout_seconds: float = 15 * 60.0,
    ) -> None:
        self.sessionmaker = sessionmaker
        self.sessions = sessions
        self.bus = bus
        self.action_timeout_seconds = action_timeout_seconds
        # a transfer without progress for this long fails with ``timeout``
        self.timeout_seconds = timeout_seconds
        self._active: dict[uuid.UUID, _Active] = {}
        self._tasks: set[asyncio.Future[None]] = set()
        # per transfer: the state changes are applied one after the other
        self._locks: dict[uuid.UUID, asyncio.Lock] = {}

    # --- startup / shutdown -------------------------------------------------------------------

    async def sweep_interrupted(self) -> int:
        """At startup: a transfer the previous hub process was coordinating cannot continue (its
        token is gone). The nodes close their sockets on their own; the rows are marked failed."""
        async with self.sessionmaker() as db:
            result = await db.execute(
                update(ModelTransfer)
                .where(ModelTransfer.status.in_(ACTIVE))
                .values(
                    status="failed",
                    error_code="interrupted",
                    error="the hub restarted during the transfer",
                    updated_at=utcnow(),
                )
            )
            await db.commit()
        return int(result.rowcount or 0)  # type: ignore[attr-defined]

    async def shutdown(self) -> None:
        for task in list(self._tasks):
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks.clear()
        self._active.clear()

    # --- create / read / cancel ---------------------------------------------------------------

    async def create(
        self,
        user_id: uuid.UUID,
        source_node_id: uuid.UUID,
        target_node_id: uuid.UUID,
        model: str,
        target_name: str | None = None,
        move: bool = False,
    ) -> ModelTransfer:
        """Check everything, store the row and ask the source to send. Raises TransferError."""
        target_name = target_name or model
        if source_node_id == target_node_id:
            raise TransferError(400, "invalid_request", "source and target must be different nodes")
        async with self.sessionmaker() as db:
            source = await db.get(Node, source_node_id)
            target = await db.get(Node, target_node_id)
            if source is None or target is None:
                raise TransferError(404, "not_found", "unknown node")
            src_inv = await db.get(NodeInventory, source_node_id)
            dst_inv = await db.get(NodeInventory, target_node_id)
        for node in (source, target):
            if not self.sessions.is_connected(node.id):
                raise TransferError(409, "node_offline", f"node {node.name!r} is not connected")
        if "models.send" not in (source.config or {}).get("allowed_actions", []):
            raise TransferError(403, "not_allowed", "models.send is not allowed on the source")
        if "models.receive" not in (target.config or {}).get("allowed_actions", []):
            raise TransferError(403, "not_allowed", "models.receive is not allowed on the target")

        src_models = ((src_inv.inventory if src_inv else None) or {}).get("models") or []
        found = next((m for m in src_models if m.get("name") == model), None)
        if found is None:
            raise TransferError(409, "model_missing", f"the source node has no model {model!r}")
        dst_inventory = (dst_inv.inventory if dst_inv else None) or {}
        if any(m.get("name") == target_name for m in dst_inventory.get("models") or []):
            raise TransferError(409, "model_exists", f"the target node already has {target_name!r}")
        size = found.get("size_bytes")
        size = size if isinstance(size, int) and not isinstance(size, bool) and size > 0 else None
        free_mb = (dst_inventory.get("hardware") or {}).get("disk_free_mb")
        if (
            size is not None
            and isinstance(free_mb, int | float)
            and free_mb * 1024 * 1024 < size + ONE_GIB
        ):
            raise TransferError(
                409, "insufficient_disk", "the target node has not enough free disk space"
            )

        # No await between the check and the registration: two requests cannot both pass.
        for other in self._active.values():
            if other.source_id in (source.id, target.id) or other.target_id in (
                source.id,
                target.id,
            ):
                raise TransferError(
                    409, "busy", "a model transfer is already running on one of these nodes"
                )
        transfer_id = uuid.uuid4()
        target_session = self.sessions.get(target.id)
        candidates = _unique_ips(
            ((dst_inventory.get("hardware") or {}).get("addresses") or [])
            + ([target_session.peer_ip] if target_session and target_session.peer_ip else []),
            MAX_ENDPOINTS,
        )
        active = _Active(
            transfer_id=transfer_id,
            user_id=user_id,
            source_id=source.id,
            target_id=target.id,
            token=secrets.token_urlsafe(32),
            target_name=target_name,
            move=move,
            target_candidates=candidates,
        )
        self._active[transfer_id] = active
        try:
            async with self.sessionmaker() as db:
                row = await ModelTransferStore(db, user_id).add(
                    ModelTransfer(
                        id=transfer_id,
                        source_node_id=source.id,
                        target_node_id=target.id,
                        model=model,
                        target_name=target_name,
                        size=size,
                        move=move,
                        status="pending",
                    )
                )
                await db.commit()
                await db.refresh(row)
        except Exception:
            self._active.pop(transfer_id, None)
            raise
        self._publish(row, user_id)
        active.watcher = self._start_watch(active)

        ok, error = await self._dispatch(
            source.id,
            "models.send",
            {
                "transfer_id": str(transfer_id),
                "model": model,
                "token": active.token,
                "peer_addresses": candidates,
            },
        )
        if ok:
            updated = await self._set(active, status="preparing", only_from=("pending",))
            return updated or row
        code, message = error
        await self._fail(active, code, message)
        status, rest_code = REFUSALS.get(code, (502, "internal"))
        raise TransferError(status, rest_code, message or "the source node refused the transfer")

    async def get(self, user_id: uuid.UUID, transfer_id: uuid.UUID) -> ModelTransfer | None:
        async with self.sessionmaker() as db:
            return await ModelTransferStore(db, user_id).get(transfer_id)

    async def list(self, user_id: uuid.UUID, node_id: uuid.UUID | None) -> list[ModelTransfer]:
        async with self.sessionmaker() as db:
            return list(await ModelTransferStore(db, user_id).list(node_id))

    async def cancel(self, user_id: uuid.UUID, transfer_id: uuid.UUID) -> ModelTransfer:
        """Best effort: the hub forgets the transfer (token included) and marks it canceled; the
        nodes close their sockets on their own (``ttl_seconds``)."""
        row = await self.get(user_id, transfer_id)
        if row is None:
            raise TransferError(404, "not_found", "unknown transfer")
        active = self._active.get(transfer_id)
        if active is None or active.user_id != user_id:
            raise TransferError(409, "not_active", "the transfer has already finished")
        updated = await self._set(
            active, final=True, status="canceled", error_code="canceled", error=None
        )
        return updated or row

    # --- events from the nodes ----------------------------------------------------------------

    async def on_event(self, node_id: uuid.UUID, kind: str, data: dict[str, Any]) -> None:
        """A ``transfer_ready`` / ``transfer_progress`` event of a node (first delivery only)."""
        try:
            transfer_id = uuid.UUID(str(data.get("transfer_id")))
        except ValueError:
            return
        active = self._active.get(transfer_id)
        if active is None or node_id not in (active.source_id, active.target_id):
            return  # not ours (finished, canceled, unknown): nothing to do, nothing to leak
        active.last_progress = time.monotonic()
        if kind == "transfer_ready":
            if node_id == active.source_id:
                spawn(self._on_ready(active, data))
        elif kind == "transfer_progress":
            await self._on_progress(active, node_id, data)

    async def _on_ready(self, active: _Active, data: dict[str, Any]) -> None:
        try:
            size, port = data.get("size"), data.get("port")
            sha, cert = data.get("sha256"), data.get("cert_sha256")
            if not (
                isinstance(size, int)
                and not isinstance(size, bool)
                and size >= 0
                and isinstance(port, int)
                and not isinstance(port, bool)
                and 0 < port < 65536
                and isinstance(sha, str)
                and isinstance(cert, str)
            ):
                await self._fail(active, "internal", "the source sent a malformed transfer_ready")
                return
            updated = await self._set(
                active, status="ready", size=size, sha256=sha, only_from=("pending", "preparing")
            )
            if updated is None:
                return
            session = self.sessions.get(active.source_id)
            addresses = _unique_ips(data.get("addresses"), MAX_ENDPOINTS)
            for ip in _unique_ips([session.peer_ip] if session and session.peer_ip else [], 1):
                if ip not in addresses:
                    addresses.append(ip)
            endpoints = [_endpoint(ip, port) for ip in addresses[:MAX_ENDPOINTS]]
            if not endpoints:
                await self._fail(active, "no_route", "the source node reported no address")
                return
            ok, error = await self._dispatch(
                active.target_id,
                "models.receive",
                {
                    "transfer_id": str(active.transfer_id),
                    "model": active.target_name,
                    "size": size,
                    "sha256": sha,
                    "token": active.token,
                    "cert_sha256": cert,
                    "endpoints": endpoints,
                },
            )
            if not ok:
                await self._fail(active, error[0], error[1])
        except Exception:
            log.exception("model transfer %s: handling transfer_ready failed", active.transfer_id)
            await self._fail(active, "internal", "the hub could not start the receiving node")

    async def _on_progress(self, active: _Active, node_id: uuid.UUID, data: dict[str, Any]) -> None:
        role, status = data.get("role"), data.get("status")
        expected = "send" if node_id == active.source_id else "receive"
        if role != expected:
            return
        progress = {
            "transferred_bytes": data.get("transferred_bytes"),
            "total_bytes": data.get("total_bytes"),
        }
        if status == "running":
            await self._set(
                active, status="transferring", only_from=("ready", "transferring"), extra=progress
            )
        elif status == "failed":
            code = data.get("error_code")
            await self._fail(
                active, code if code in NODE_ERROR_CODES else "internal", data.get("error")
            )
        elif status == "done" and role == "receive":
            row = await self._set(active, final=True, status="done", error=None, error_code=None)
            if row is not None and active.move:
                spawn(self._delete_source(active, row))

    async def _delete_source(self, active: _Active, row: ModelTransfer) -> None:
        """Move: the target has the verified copy, now the source may drop its own."""
        try:
            ok, error = await self._dispatch(
                active.source_id, "models.delete", {"model": row.model, "confirm": True}
            )
            warning = None if ok else f"copied, but the source copy was not deleted: {error[1]}"
        except Exception:
            log.exception("model transfer %s: deleting the source failed", active.transfer_id)
            warning = "copied, but the source copy was not deleted"
        if warning is None:
            return
        async with self.sessionmaker() as db:
            updated = await ModelTransferStore(db, active.user_id).update(
                active.transfer_id, error=warning
            )
            await db.commit()
        if updated is not None:
            self._publish(updated, active.user_id)

    # --- state changes ------------------------------------------------------------------------

    async def _dispatch(
        self, node_id: uuid.UUID, name: str, args: dict[str, Any]
    ) -> tuple[bool, tuple[str, str | None]]:
        """Run an action on a node; (True, ...) on success, else (False, (error_code, message)).
        The arguments (they hold the token) are never logged."""
        try:
            result = await self.sessions.dispatch(node_id, name, args, self.action_timeout_seconds)
        except (NodeOffline, NodeReadOnly):
            return False, ("node_offline", "the node is not connected")
        except ActionTimeout:
            return False, ("timeout", f"the node did not answer {name} in time")
        if result.get("ok"):
            return True, ("", None)
        err = result.get("error") or {}
        code = err.get("code")
        return False, (str(code or "internal"), _error_text(err.get("message")))

    def _lock(self, transfer_id: uuid.UUID) -> asyncio.Lock:
        return self._locks.setdefault(transfer_id, asyncio.Lock())

    async def _set(
        self,
        active: _Active,
        final: bool = False,
        only_from: tuple[str, ...] | None = None,
        extra: dict[str, Any] | None = None,
        **values: Any,
    ) -> ModelTransfer | None:
        """Apply a state change of a running transfer, store it and tell the GUI. ``only_from``:
        the change only applies from these statuses. A final change ends the transfer here (the
        token is forgotten). Returns the row, or None when nothing was changed."""
        async with self._lock(active.transfer_id):
            if self._active.get(active.transfer_id) is not active:
                return None  # already finished
            async with self.sessionmaker() as db:
                store = ModelTransferStore(db, active.user_id)
                current = await store.get(active.transfer_id)
                if current is None or (only_from is not None and current.status not in only_from):
                    return None
                changed = {k: v for k, v in values.items() if getattr(current, k) != v}
                row = current
                if changed:
                    row = await store.update(active.transfer_id, **changed) or current
                await db.commit()
                await db.refresh(row)
            if final:
                self._finish(active)
        if changed or extra:
            self._publish(row, active.user_id, extra)
        return row

    async def _fail(self, active: _Active, code: str, message: Any = None) -> ModelTransfer | None:
        return await self._set(
            active,
            final=True,
            status="failed",
            error_code=code if code in STORED_CODES else "internal",
            error=_error_text(message),
        )

    def _finish(self, active: _Active) -> None:
        self._active.pop(active.transfer_id, None)
        self._locks.pop(active.transfer_id, None)
        watcher, active.watcher = active.watcher, None
        if watcher is not None and watcher is not asyncio.current_task():
            watcher.cancel()

    def _publish(
        self, row: ModelTransfer, user_id: uuid.UUID, extra: dict[str, Any] | None = None
    ) -> None:
        # ``_user``: only this person's GUI streams get it (sse_stream)
        payload = {"transfer": view(row), **(extra or {}), "_user": str(user_id)}
        self.bus.publish("model_transfer", payload)

    # --- timeout ------------------------------------------------------------------------------

    def _start_watch(self, active: _Active) -> "asyncio.Future[None]":
        task = asyncio.ensure_future(self._watch(active))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return task

    async def _watch(self, active: _Active) -> None:
        """Fail the transfer when neither node has reported anything for ``timeout_seconds``."""
        while self._active.get(active.transfer_id) is active:
            idle = time.monotonic() - active.last_progress
            if idle >= self.timeout_seconds:
                await self._fail(active, "timeout", "no progress for too long")
                return
            await asyncio.sleep(self.timeout_seconds - idle)
