"""Old rows leave the tables that only ever grow (consolidation step 7).

Two tables grow with every heartbeat and every call and nothing read them after a while:

* ``node_events`` -- what the nodes reported (a model loaded, a game seen):
  ``retention_node_events_days`` (90) after the hub received it;
* ``tool_calls`` -- the audit log of every capability call (D15, D29):
  ``retention_tool_calls_days`` (365) after the call. This is the record of what the assistant
  looked at, so it is kept for a year; an admin who needs it longer sets the number higher
  (0 = keep for ever).

Rows are deleted in batches so that one run never holds a lock for long. The chats, notes, tasks and
their steps are the person's data and are not touched here."""

import asyncio
import logging
from datetime import UTC, datetime, timedelta

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from topcoat_hub.config import Settings
from topcoat_hub.models import NodeEvent, ToolCall

log = logging.getLogger(__name__)

BATCH = 5000


class Retention:
    def __init__(self, sessionmaker: async_sessionmaker[AsyncSession], settings: Settings) -> None:
        self.sessionmaker = sessionmaker
        self.settings = settings

    async def prune(self, now: datetime | None = None) -> dict[str, int]:
        """Delete what is older than the retention; returns how many rows went, per table."""
        now = now or datetime.now(UTC)
        gone = {"node_events": 0, "tool_calls": 0}
        days = self.settings.retention_node_events_days
        if days > 0:
            gone["node_events"] = await self._delete(
                NodeEvent, NodeEvent.id, NodeEvent.received_at, now - timedelta(days=days)
            )
        days = self.settings.retention_tool_calls_days
        if days > 0:
            gone["tool_calls"] = await self._delete(
                ToolCall, ToolCall.id, ToolCall.created_at, now - timedelta(days=days)
            )
        if any(gone.values()):
            log.info("retention: removed %s", ", ".join(f"{n} {t}" for t, n in gone.items() if n))
        return gone

    async def _delete(self, model: type, key: object, when: object, before: datetime) -> int:
        total = 0
        while True:
            async with self.sessionmaker() as db:
                ids = (
                    await db.scalars(
                        select(key).where(when < before).order_by(key).limit(BATCH)  # type: ignore[arg-type]
                    )
                ).all()
                if not ids:
                    return total
                await db.execute(delete(model).where(key.in_(ids)))  # type: ignore[attr-defined]
                await db.commit()
            total += len(ids)
            if len(ids) < BATCH:
                return total

    async def scheduler(self) -> None:
        """Once at start (a little later, the hub has other work then) and then every tick."""
        await asyncio.sleep(min(300.0, self.settings.retention_tick_seconds))
        while True:
            try:
                await self.prune()
            except Exception:
                log.exception("retention: pruning failed")
            await asyncio.sleep(self.settings.retention_tick_seconds)
