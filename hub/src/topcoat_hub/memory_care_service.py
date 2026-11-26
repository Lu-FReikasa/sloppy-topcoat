"""The care run of the assistant's memory (E3-05): once a night and when the hub is idle it looks
after the notes of every user, one user at a time (D20), and writes down what it did.

* **Expiry.** A volatile note past its ``expires_at`` is soft-deleted, with the reason.
* **Archive.** A note nobody needs (usefulness low *and* reliability low *and* unused for long) is
  archived — soft, searchable, restorable. Age alone never archives.
* **No live checks (D124).** The care run checks no note against a live system and lowers no
  trust: a chat note keeps the reliability its statement, the settling of contradictions and its
  age give it.
* **Mirrored notes.** A note mirrored from a note file (``steckbriefe/``, E3-06) is never
  archived or rewritten by the care: the file wins.
* **The core.** The entries that stand first in every prompt are looked at for contradictions,
  facts that cost room and are never used, facts that deserve a place — as *proposals* only
  (:mod:`topcoat_hub.core_review`). The *deep* model is preferred (the model of the ``thorough``
  level).
* **Across the memories.** What two program memories say differently goes to the program
  responsible for its topic (D74, :mod:`topcoat_hub.memory_check`).

Every change is a version (``actor = "care"``) with its reason, and the run's report lists each
with what undoes it."""

import asyncio
import logging
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from topcoat_hub import memory_care
from topcoat_hub.background import current
from topcoat_hub.config import Settings
from topcoat_hub.core_review import CoreReview
from topcoat_hub.gateway import Gateway, GatewayError, background_work
from topcoat_hub.memory import MemoryService
from topcoat_hub.memory_check import CrossCheck
from topcoat_hub.memory_store import MemoryStore, users_with_memory
from topcoat_hub.models import MemoryItem

log = logging.getLogger(__name__)

RUN_KIND = "care"


def night_boundary(now: datetime, hour: int, tz: Any = None) -> datetime | None:
    """The latest ``hour``:00 (server time, or ``tz``) that is not after ``now``, in UTC; ``None``
    when no fixed hour is set (-1)."""
    if not 0 <= hour <= 23:
        return None
    local = now.astimezone(tz)
    boundary = local.replace(hour=hour, minute=0, second=0, microsecond=0)
    if boundary > local:
        boundary -= timedelta(days=1)
    return boundary.astimezone(UTC)


@dataclass
class Plan:
    """What a run would do for one user right now."""

    expired: list[MemoryItem] = field(default_factory=list)
    archive: list[MemoryItem] = field(default_factory=list)

    @property
    def has_work(self) -> bool:
        return bool(self.expired or self.archive)


def plan_for(
    items: list[MemoryItem],
    expired: list[MemoryItem],
    now: datetime,
    cfg: memory_care.CareConfig,
) -> Plan:
    """Sort a user's live notes into what the care does with them (pure, tested with a fake
    clock). ``items`` are the active notes, ``expired`` the live ones past their expiry. A note
    mirrored from a file is never archived: the file wins."""
    plan = Plan(expired=list(expired))
    gone = {i.id for i in expired}
    for item in items:
        if item.id in gone or getattr(item, "source_type", None) == "kb":
            continue
        if memory_care.archive_due(item, now, cfg):
            plan.archive.append(item)
    return plan


class MemoryCare:
    def __init__(
        self,
        sessionmaker: async_sessionmaker[AsyncSession],
        gateway: Gateway,
        memory: MemoryService,
        settings: Settings,
        digester: Any = None,
    ) -> None:
        self.digester = digester  # the chat digest: the care run hands it chats nobody summed up
        self.sessionmaker = sessionmaker
        self.gateway = gateway
        self.memory = memory
        self.settings = settings
        self.cfg = memory.care_config
        self.review = CoreReview(gateway, self.cfg)
        # D74: what two program memories say differently goes to the program responsible for it
        self.crosscheck = CrossCheck(sessionmaker, gateway, memory, settings)
        self._running: set[uuid.UUID] = set()

    # --- scheduling ---------------------------------------------------------------------------

    async def _backfill_digests(self, user_id: uuid.UUID) -> None:
        """Hand the chat digest the chats of any age that were never summed up (a node was away
        when they ended). It arms timers only, the digest does the work when the hub is idle."""
        if self.digester is None:
            return
        try:
            armed = await self.digester.catch_up(user_id, hours=24 * 365)
            if armed:
                log.info("memory care: %d chat(s) handed to the chat digest", armed)
        except Exception:
            log.exception("memory care: handing chats to the digest failed")

    def is_running(self, user_id: uuid.UUID) -> bool:
        return user_id in self._running

    async def scheduler(self) -> None:
        interval = self.settings.memory_care_interval_seconds
        while True:
            await asyncio.sleep(interval)
            try:
                await self.tick()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("memory care tick failed")

    async def tick(self) -> int:
        """One look at all users: a run for each whose night run is due (the first idle moment
        after the care hour) or who has not been looked after for a while. Returns the runs."""
        if not current(self, "care").on or not await self.memory.idle_ok():
            return 0
        async with self.sessionmaker() as db:
            users = await users_with_memory(db)
        runs = 0
        for user_id in users:
            if not await self.memory.idle_ok():
                break  # somebody started chatting or a game started
            trigger = await self._due(user_id)
            if trigger is None:
                continue
            if await self.run(user_id, trigger, only_with_work=True) is not None:
                runs += 1
        return runs

    async def _due(self, user_id: uuid.UUID) -> str | None:
        now = memory_care.utcnow()
        async with self.sessionmaker() as db:
            runs = await MemoryStore(db, user_id).runs(limit=5, kind=RUN_KIND)
        # a paused run did not look after anything: it does not count
        last = next((r for r in runs if r.status not in ("running", "paused")), None)
        if any(r.status == "running" for r in runs):
            return None
        started = last.started_at if last else None
        bg = current(self, "care")
        boundary = night_boundary(now, bg.hour) if bg.hour >= 0 else None
        if boundary is not None and (started is None or started < boundary):
            return "nightly"
        every = timedelta(hours=bg.every_hours or self.settings.memory_care_idle_every_hours)
        if started is None or now - started >= every:
            return "idle"
        return None

    # --- one run ------------------------------------------------------------------------------

    @background_work
    async def run(
        self, user_id: uuid.UUID, trigger: str, only_with_work: bool = False
    ) -> uuid.UUID | None:
        """One care run for one user; the run's id, or ``None`` if one is already running for
        this user (or, with ``only_with_work``, there was nothing to do)."""
        if user_id in self._running:
            return None
        self._running.add(user_id)
        try:
            await self._backfill_digests(user_id)
            if (
                only_with_work
                and not (await self._plan(user_id)).has_work
                and not await self._core_due(user_id)
                and not await self._crosscheck_due(user_id)
            ):
                return None
            async with self.sessionmaker() as db:
                store = MemoryStore(db, user_id)
                run = await store.start_run(trigger, RUN_KIND)
                await db.commit()
            stats: dict[str, Any] = {"expired": 0, "archived": 0, "proposed": 0, "changes": []}
            status = "ok"
            try:
                status = await self._work(user_id, run.id, stats)
            except GatewayError as exc:
                status = "paused" if exc.status == 503 else "no_model"
                stats["error"] = exc.message[:200]
            except Exception as exc:
                log.exception("memory care failed")
                status = "error"
                stats["error"] = type(exc).__name__
            async with self.sessionmaker() as db:
                await MemoryStore(db, user_id).finish_run(run.id, status, stats)
                await db.commit()
            return run.id
        finally:
            self._running.discard(user_id)

    async def _crosscheck_due(self, user_id: uuid.UUID) -> bool:
        async with self.sessionmaker() as db:
            return bool(await self.crosscheck.pairs(db, user_id, limit=1))

    async def _plan(self, user_id: uuid.UUID) -> Plan:
        now = memory_care.utcnow()
        async with self.sessionmaker() as db:
            store = MemoryStore(db, user_id)
            return plan_for(
                await store.active(mirror=True), await store.expired(now), now, self.cfg
            )

    async def _work(self, user_id: uuid.UUID, run_id: uuid.UUID, stats: dict[str, Any]) -> str:
        plan = await self._plan(user_id)
        bg = current(self, "care")  # what the flow of the care run says (D75)
        # 1. what needs no model: expiry and archive
        if bg.step("expire").on:
            await self._expire(user_id, plan.expired, stats)
        if bg.step("archive").on:
            await self._archive(user_id, plan.archive, stats)
        # 2. the core: contradictions and room-takers become proposals, never changes
        if bg.step("core_review").on:
            await self._review_core(user_id, stats)
        # 3. across the memories: a discrepancy goes to the program responsible for its topic
        if bg.step("crosscheck").on:
            await self.crosscheck.run(user_id, stats)
        return "ok"

    # --- the core -----------------------------------------------------------------------------

    async def _last_review(self, user_id: uuid.UUID) -> str | None:
        async with self.sessionmaker() as db:
            return next(
                (
                    (r.stats or {}).get("core_review")
                    for r in await MemoryStore(db, user_id).runs(limit=5, kind=RUN_KIND)
                    if (r.stats or {}).get("core_review")
                ),
                None,
            )

    async def _core_due(self, user_id: uuid.UUID) -> bool:
        last = await self._last_review(user_id)
        async with self.sessionmaker() as db:
            return await self.review.due(MemoryStore(db, user_id), last)

    async def _review_core(self, user_id: uuid.UUID, stats: dict[str, Any]) -> None:
        now = memory_care.utcnow()
        last = await self._last_review(user_id)
        model: str | None = None
        try:
            model = await self._deep_model()
            await self.gateway.route(model)
        except GatewayError:
            model = None  # no model now: the counters are still looked at
        try:
            async with self.sessionmaker() as db:
                lines, fp = await self.review.review(MemoryStore(db, user_id), model, last, now)
                await db.commit()
        except GatewayError:
            return
        stats["core_review"] = fp if model is not None else last
        stats["proposed"] += len(lines)
        stats["changes"].extend(lines)

    # --- expiry and archive ------------------------------------------------------------------

    async def _expire(
        self, user_id: uuid.UUID, items: list[MemoryItem], stats: dict[str, Any]
    ) -> None:
        for item in items:
            async with self.sessionmaker() as db:
                store = MemoryStore(db, user_id)
                reason = (
                    f"care: expired on {item.expires_at:%Y-%m-%d %H:%M} UTC "
                    "(a volatile state is kept for a day at most)"
                )
                new = await store.change(
                    item.id,
                    reason=reason,
                    actor="care",
                    status="deleted",
                    disputed_with=None,
                    superseded_by=None,
                )
                if new is None:
                    continue
                await store.close_questions_about([item.id], "the note expired")
                await db.commit()
            stats["expired"] += 1
            stats["changes"].append(_change("expired", item, new.version, reason, undo="restore"))

    async def _archive(
        self, user_id: uuid.UUID, items: list[MemoryItem], stats: dict[str, Any]
    ) -> None:
        now = memory_care.utcnow()
        for item in items:
            idle = (now - memory_care.last_use_or_creation(item)).days
            useful = memory_care.usefulness(item, now)
            sure = memory_care.reliability(item, now, self.cfg)
            reason = (
                f"care: archived — rarely useful ({useful:.2f}), not reliable any more "
                f"({sure:.2f}) and not used for {idle} days"
            )
            async with self.sessionmaker() as db:
                store = MemoryStore(db, user_id)
                new = await store.change(item.id, reason=reason, actor="care", status="archived")
                if new is None:
                    continue
                await db.commit()
            stats["archived"] += 1
            stats["changes"].append(_change("archived", item, new.version, reason, undo="restore"))

    async def _deep_model(self) -> str:
        """The deep role: the model the memory uses (the pick of the row ``background``)."""
        return await self.memory._model()


def _change(
    action: str, item: MemoryItem, version: int | None, reason: str, undo: str | None = None
) -> dict[str, Any]:
    """One line of a run's report. ``undo``: ``restore`` (bring a struck or archived note back)
    or ``revert`` (to ``from_version``); none for what changed no content."""
    return {
        "action": action,
        "item_id": str(item.id),
        "text": item.text,
        "kind": item.kind,
        "version": version,
        "reason": reason,
        "undo": undo,
    }
