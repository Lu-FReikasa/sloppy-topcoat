"""Database operations shared by the node WebSocket and the GUI API."""

import contextlib
import copy
import logging
import math
import uuid
from collections.abc import Callable
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from topcoat_hub import views
from topcoat_hub.live import EventBus, NodeSessions
from topcoat_hub.models import Node, NodeBenchmark, NodeEvent, NodeInventory
from topcoat_hub.protocol import config_hash, envelope
from topcoat_hub.updates import UpdateStore, node_channel

if TYPE_CHECKING:
    from topcoat_hub.model_transfers import TransferCoordinator

log = logging.getLogger(__name__)

MAX_THREADS = 1024  # node_config.schema.json runner.threads maximum


def utcnow() -> datetime:
    return datetime.now(UTC)


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _is_number(value: Any) -> bool:
    return (_is_int(value) or isinstance(value, float)) and math.isfinite(value)


def benchmark_row(node_id: uuid.UUID, data: dict[str, Any], ts: datetime) -> NodeBenchmark | None:
    """Build a ``node_benchmarks`` row from ``benchmark_result`` data, or None if malformed.

    Event ``data`` is free-form in the schema, so the fields are checked here
    (docs/PROTOCOL.md "Long-running actions").
    """
    try:
        benchmark_id = uuid.UUID(str(data["benchmark_id"]))
        model, backend = data["model"], data["backend"]
        threads, n_gpu_layers = data["threads"], data.get("n_gpu_layers")
        pp_tps, tg_tps = data["pp_tps"], data["tg_tps"]
    except (KeyError, ValueError):
        return None
    if not (isinstance(model, str) and model and isinstance(backend, str) and backend):
        return None
    if len(backend) > 20 or not _is_int(threads) or threads < 1:
        return None
    if n_gpu_layers is not None and not _is_int(n_gpu_layers):
        return None
    if not (_is_number(pp_tps) and _is_number(tg_tps)):
        return None
    return NodeBenchmark(
        node_id=node_id,
        benchmark_id=benchmark_id,
        model=model,
        backend=backend,
        threads=threads,
        n_gpu_layers=n_gpu_layers,
        pp_tps=float(pp_tps),
        tg_tps=float(tg_tps),
        ts=ts,
    )


class NodeService:
    """Writes node state and publishes the resulting GUI updates."""

    def __init__(
        self, sessionmaker: async_sessionmaker[AsyncSession], bus: EventBus, sessions: NodeSessions
    ) -> None:
        self.sessionmaker = sessionmaker
        self.bus = bus
        self.sessions = sessions
        # the release store of the update channel (set by the app; None in unit tests)
        self.updates: UpdateStore | None = None
        # the coordinator of direct model transfers (E2-07, D51; set by the app)
        self.transfers: TransferCoordinator | None = None

    async def summary(self, db: AsyncSession, node_id: uuid.UUID) -> dict[str, Any] | None:
        node = await db.get(Node, node_id)
        if node is None:
            return None
        inv = await db.get(NodeInventory, node_id)
        return views.node_summary(
            node, inv, self.sessions.is_connected(node_id), self.latest_release(node)
        )

    def latest_release(self, node: Node) -> str | None:
        """Version of the newest release the hub holds for this node (None: none or no store)."""
        if self.updates is None:
            return None
        rel = self.updates.latest(node_channel(node.config), node.os, node.arch)
        return rel.version if rel else None

    async def publish_node(self, node_id: uuid.UUID) -> None:
        async with self.sessionmaker() as db:
            summary = await self.summary(db, node_id)
        if summary is not None:
            self.bus.publish("node_updated", summary)

    async def record_event(
        self,
        node_id: uuid.UUID,
        kind: str,
        data: dict[str, Any],
        ts: datetime | None = None,
        message_id: str | None = None,
    ) -> bool:
        """Append an event; returns False for a replayed event (known message id).

        A new ``benchmark_result`` is stored in ``node_benchmarks`` in the same transaction,
        so replays after a lost ack never duplicate measurements.
        """
        ts = ts or utcnow()
        async with self.sessionmaker() as db:
            stmt = (
                insert(NodeEvent)
                .values(
                    node_id=node_id,
                    kind=kind,
                    data=data,
                    ts=ts,
                    message_id=uuid.UUID(message_id) if message_id else None,
                )
                .on_conflict_do_nothing(index_elements=["message_id"])
                .returning(NodeEvent)
            )
            event = (await db.execute(stmt)).scalar_one_or_none()
            if event is not None and kind == "benchmark_result":
                row = benchmark_row(node_id, data, ts)
                if row is None:
                    log.warning("ignoring malformed benchmark_result from node %s", node_id)
                else:
                    db.add(row)
            await db.commit()
        if event is None:
            return False
        self.bus.publish("node_event", {"node_id": str(node_id), "event": views.node_event(event)})
        return True

    async def set_config(self, node_id: uuid.UUID, config: dict[str, Any]) -> str | None:
        """Store a node's desired config and push it if the node is connected.

        The caller validates ``config``. Returns the new hash, or None for an unknown node.
        """
        new_hash = config_hash(config)
        async with self.sessionmaker() as db:
            result = await db.execute(
                update(Node)
                .where(Node.id == node_id)
                .values(config=config, config_hash=new_hash)
                .returning(Node.id)
            )
            found = result.scalar_one_or_none() is not None
            await db.commit()
        if not found:
            return None
        await self.push_config(node_id, config, new_hash)
        return new_hash

    async def change_config(
        self, node_id: uuid.UUID, mutate: Callable[[dict[str, Any]], bool]
    ) -> tuple[dict[str, Any], str, bool]:
        """Change a node's config under a row lock and push it.

        ``mutate`` edits a copy in place and returns False if it changed nothing (it may raise
        to abort, e.g. when the result fails schema validation). Returns the resulting config,
        its hash and whether it changed. Raises LookupError for an unknown node.
        """
        async with self.sessionmaker() as db:
            node = await db.scalar(select(Node).where(Node.id == node_id).with_for_update())
            if node is None:
                raise LookupError(node_id)
            config = copy.deepcopy(node.config)
            if not mutate(config):
                return node.config, node.config_hash, False
            new_hash = config_hash(config)
            node.config = config
            node.config_hash = new_hash
            await db.commit()
        await self.push_config(node_id, config, new_hash)
        return config, new_hash, True

    async def push_config(self, node_id: uuid.UUID, config: dict[str, Any], hash_: str) -> None:
        session = self.sessions.get(node_id)
        if session is None or session.read_only:
            return  # the node gets the stored config with its next hello
        # A socket closing concurrently is fine: the next hello delivers the config.
        with contextlib.suppress(RuntimeError, OSError):
            await session.send(envelope("config", {"config_hash": hash_, "config": config}))

    async def apply_recommended_threads(self, node_id: uuid.UUID, threads: Any) -> bool:
        """Store a benchmark's ``recommended_threads`` as ``config.runner.threads``.

        Returns True if the config changed (and was pushed to the node).
        """
        if not _is_int(threads) or not 1 <= threads <= MAX_THREADS:
            return False
        async with self.sessionmaker() as db:
            node = await db.scalar(select(Node).where(Node.id == node_id).with_for_update())
            if node is None or node.config.get("runner", {}).get("threads") == threads:
                return False
            config = copy.deepcopy(node.config)
            config.setdefault("runner", {})["threads"] = threads
            new_hash = config_hash(config)
            node.config = config
            node.config_hash = new_hash
            await db.commit()
        await self.push_config(node_id, config, new_hash)
        return True

    async def update_node(self, node_id: uuid.UUID, **values: Any) -> None:
        async with self.sessionmaker() as db:
            await db.execute(update(Node).where(Node.id == node_id).values(**values))
            await db.commit()

    async def store_inventory(
        self,
        node_id: uuid.UUID,
        *,
        inventory: dict[str, Any] | None = None,
        status: dict[str, Any] | None = None,
    ) -> None:
        values: dict[str, Any] = {"updated_at": utcnow()}
        if inventory is not None:
            values["inventory"] = inventory
        if status is not None:
            values["status"] = status
        async with self.sessionmaker() as db:
            stmt = insert(NodeInventory).values(node_id=node_id, **values)
            stmt = stmt.on_conflict_do_update(index_elements=["node_id"], set_=values)
            await db.execute(stmt)
            await db.commit()

    async def mark_all_offline(self) -> None:
        """At startup no node is connected yet."""
        async with self.sessionmaker() as db:
            await db.execute(update(Node).where(Node.state != "offline").values(state="offline"))
            await db.commit()

    async def recent_events(
        self, db: AsyncSession, node_id: uuid.UUID, limit: int
    ) -> list[dict[str, Any]]:
        rows = await db.execute(
            select(NodeEvent)
            .where(NodeEvent.node_id == node_id)
            .order_by(NodeEvent.id.desc())
            .limit(limit)
        )
        return [views.node_event(e) for e in rows.scalars()]

    async def recent_benchmarks(
        self, db: AsyncSession, node_id: uuid.UUID, limit: int
    ) -> list[dict[str, Any]]:
        rows = await db.execute(
            select(NodeBenchmark)
            .where(NodeBenchmark.node_id == node_id)
            .order_by(NodeBenchmark.ts.desc(), NodeBenchmark.id.desc())
            .limit(limit)
        )
        return [views.node_benchmark(b) for b in rows.scalars()]
