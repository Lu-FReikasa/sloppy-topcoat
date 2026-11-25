"""The regular re-check of pinned answers (docs/PLATFORM.md, 3.7).

An answer that came back from another application and was pinned to the board is not left to age:
every ``recheck_interval_hours`` the same dossier is sent again as the person who pinned it (their
rights, the application's own tools, read-only like any check). The new answer is compared with the
old one and leaves a mark on the pinned item: a "still correct" sticker with the date, or a Post-It
"outdated" with what differs, and then a new item in the inbox. The pinned item's text is never
rewritten, and nothing is re-checked that does not carry the dossier it came from."""

import asyncio
import logging
import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from topcoat_hub.auth import CurrentUser
from topcoat_hub.background import current
from topcoat_hub.config import Settings
from topcoat_hub.handover import HandoverError, HandoverService
from topcoat_hub.models import Handover, InboxItem, User

log = logging.getLogger(__name__)

FIRST_ROUND_DELAY = 600.0  # seconds after the start: the hub is not busy with this at boot


class Rechecker:
    def __init__(
        self,
        sessionmaker: async_sessionmaker[AsyncSession],
        handovers: HandoverService,
        settings: Settings,
    ) -> None:
        self.sessionmaker, self.handovers, self.settings = sessionmaker, handovers, settings

    async def due(self, now: datetime | None = None) -> list[tuple[uuid.UUID, uuid.UUID]]:
        """``(user, item)`` of the pinned answers that have not been checked for a whole interval
        and have no re-check going on, oldest first, at most ``recheck_max``."""
        now = now or datetime.now(UTC)
        bg = current(self, "recheck")
        if not bg.on or not bg.step("recheck").on:
            return []
        every = timedelta(hours=bg.every_hours or self.settings.recheck_interval_hours)
        most = bg.limit("recheck", self.settings.recheck_max)
        out: list[tuple[uuid.UUID, uuid.UUID]] = []
        async with self.sessionmaker() as db:
            pinned = await db.scalars(
                select(InboxItem)
                .where(InboxItem.pinned.is_(True), InboxItem.kind == "handover_result")
                .order_by(InboxItem.created_at)
            )
            for item in pinned:
                if not (item.ref or {}).get("recheck", {}).get("handover"):
                    continue
                last = list(
                    await db.scalars(
                        select(Handover)
                        .where(Handover.recheck_item_id == item.id)
                        .order_by(Handover.created_at.desc())
                        .limit(1)
                    )
                )
                if last:
                    h = last[0]
                    if h.status in ("proposed", "sent"):
                        continue  # one is going on
                    if (h.finished_at or h.created_at) > now - every:
                        continue  # checked lately
                elif item.created_at > now - every:
                    continue  # the answer itself is younger than an interval
                out.append((item.user_id, item.id))
                if len(out) >= most:
                    break
        return out

    async def run_once(self) -> int:
        """One round. Returns how many re-checks were started."""
        started = 0
        for user_id, item_id in await self.due():
            async with self.sessionmaker() as db:
                account = await db.get(User, user_id)
            if account is None:
                continue
            user = CurrentUser(id=str(user_id), name=account.name, role=account.role)
            try:
                await self.handovers.recheck(user, item_id)
                started += 1
            except HandoverError as exc:  # the application is gone, the agent is busy: next round
                log.info("re-check of %s not started: %s", item_id, exc.code)
            except Exception:
                log.exception("re-check of %s failed to start", item_id)
        return started

    async def scheduler(self) -> None:
        await asyncio.sleep(FIRST_ROUND_DELAY)
        while True:
            try:
                n = await self.run_once()
                if n:
                    log.info("re-check: %d pinned answer(s) asked for again", n)
            except Exception:
                log.exception("re-check round failed")
            await asyncio.sleep(max(600.0, self.settings.recheck_interval_hours * 3600 / 4))
