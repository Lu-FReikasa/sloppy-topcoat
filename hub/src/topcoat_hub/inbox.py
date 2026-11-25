"""The inbox (docs/PLATFORM.md, 3.7): what happened and wants to be read.

An item belongs to a person (D20) and to an application. Reading an item opens it and files it in
the corner of its application, unless it is pinned (the board) or **urgent** and not yet resolved:
an urgent item needs an action or attention and stays in the inbox until the matter is settled.

Most items are not pushed by the code that causes them. :func:`sync` looks at the places that
already hold such things (open questions of the memory, proposals waiting for a click, findings of
the second look, background tasks that ended) and makes one item per thing; an item whose thing is
settled is resolved. So nothing in those services had to learn about the inbox, and a missed event
is made good by the next look. Producers that have no place of their own (a hand-over, a trigger)
call :meth:`InboxStore.add`."""

import uuid
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import and_, delete, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from topcoat_hub.agent_store import AgentStore
from topcoat_hub.memory_store import MemoryStore
from topcoat_hub.models import Conversation, InboxItem
from topcoat_hub.review_store import ReviewStore

DEFAULT_APPLICATION = "chat"
VIEWS = ("inbox", "board", "filed")
RECENT_TASKS = timedelta(days=14)
# a mistake the second look found is worth an item while it is fresh (2026-11-13: the inbox held
# 150 notes of the second look, most about answers of days ago)
RECENT_REVIEWS = timedelta(days=7)
# kinds that no longer make an item: their open items are filed on the next look. A note of the
# second look is shown at the answer; it asks nothing of the person.
RETIRED_KINDS = ("review_hint",)
# kinds whose item is resolved when the thing it is about is gone (most are urgent while it is
# open; a mistake of the second look is not urgent, but settles itself once it was seen)
AUTO_RESOLVED = (
    "memory_question",
    "proposal",
    "instruction_proposal",
    "review_error",
    "task_failed",
)


@dataclass
class Candidate:
    kind: str
    source_type: str | None
    source_ref: str | None
    title: str
    summary: str = ""
    application: str = DEFAULT_APPLICATION
    urgent: bool = False
    ref: dict[str, Any] = field(default_factory=dict)


def _now() -> datetime:
    return datetime.now(UTC)


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


def item_view(i: InboxItem) -> dict[str, Any]:
    return {
        "id": str(i.id),
        "application": i.application,
        "kind": i.kind,
        "title": i.title,
        "summary": i.summary,
        "ref": i.ref or {},
        "urgent": i.urgent,
        "unresolved": i.urgent and i.resolved_at is None,
        "read": i.read_at is not None,
        "pinned": i.pinned,
        "filed": i.filed_at is not None,
        "created_at": _iso(i.created_at),
        "read_at": _iso(i.read_at),
        "resolved_at": _iso(i.resolved_at),
    }


def _holds(i: InboxItem) -> bool:
    """An urgent item that is not resolved is not filed away by reading."""
    return i.urgent and i.resolved_at is None


class InboxStore:
    """Everything the inbox keeps, scoped to one person."""

    def __init__(self, db: AsyncSession, user_id: uuid.UUID) -> None:
        self.db, self.user_id = db, user_id

    def _mine(self) -> Any:
        return InboxItem.user_id == self.user_id

    async def get(self, item_id: uuid.UUID) -> InboxItem | None:
        return await self.db.scalar(select(InboxItem).where(self._mine(), InboxItem.id == item_id))

    async def add(self, c: Candidate) -> tuple[InboxItem, bool]:
        """One item per source: the item that exists is refreshed (while it is unread), a new one is
        made otherwise. Returns the item and whether it is new."""
        found = None
        if c.source_type is not None and c.source_ref is not None:
            found = await self.db.scalar(
                select(InboxItem).where(
                    self._mine(),
                    InboxItem.kind == c.kind,
                    InboxItem.source_type == c.source_type,
                    InboxItem.source_ref == c.source_ref,
                )
            )
        if found is not None:
            if found.read_at is None:
                found.title, found.summary = c.title[:200], c.summary[:600]
            if c.urgent and found.resolved_at is not None:
                found.resolved_at = None  # the thing is open again
                found.read_at = found.filed_at = None
            return found, False
        item = InboxItem(
            user_id=self.user_id,
            application=c.application,
            kind=c.kind,
            source_type=c.source_type,
            source_ref=c.source_ref,
            title=c.title[:200],
            summary=c.summary[:600],
            ref=c.ref,
            urgent=c.urgent,
        )
        self.db.add(item)
        await self.db.flush()
        return item, True

    async def listing(
        self, view: str, application: str | None = None, limit: int = 200
    ) -> Sequence[InboxItem]:
        where = [self._mine()]
        if application:
            where.append(InboxItem.application == application)
        if view == "inbox":
            where.append(
                or_(
                    InboxItem.read_at.is_(None),
                    and_(InboxItem.urgent.is_(True), InboxItem.resolved_at.is_(None)),
                )
            )
        elif view == "board":
            where.append(InboxItem.pinned.is_(True))
        else:
            where += [InboxItem.filed_at.is_not(None), InboxItem.pinned.is_(False)]
        order = [InboxItem.created_at.desc()]
        if view == "inbox":  # what needs an action first
            order.insert(
                0,
                (InboxItem.urgent.is_(True) & InboxItem.resolved_at.is_(None)).desc(),
            )
        rows = await self.db.scalars(select(InboxItem).where(*where).order_by(*order).limit(limit))
        return rows.all()

    async def counts(self) -> dict[str, Any]:
        rows = await self.db.execute(
            select(InboxItem.application, func.count())
            .where(self._mine(), InboxItem.read_at.is_(None))
            .group_by(InboxItem.application)
        )
        per_app = {app: n for app, n in rows}
        urgent = await self.db.scalar(
            select(func.count()).where(
                self._mine(), InboxItem.urgent.is_(True), InboxItem.resolved_at.is_(None)
            )
        )
        return {"unread": sum(per_app.values()), "urgent": urgent or 0, "applications": per_app}

    async def open(self, item_id: uuid.UUID) -> InboxItem | None:
        """Read it: it is marked read and, unless it is pinned or still urgent, filed."""
        item = await self.get(item_id)
        if item is None:
            return None
        now = _now()
        if item.read_at is None:
            item.read_at = now
        if not item.pinned and not _holds(item) and item.filed_at is None:
            item.filed_at = now
        return item

    async def pin(self, item_id: uuid.UUID, pinned: bool) -> InboxItem | None:
        item = await self.get(item_id)
        if item is None:
            return None
        item.pinned = pinned
        if not pinned and item.read_at is not None and not _holds(item) and item.filed_at is None:
            item.filed_at = _now()
        return item

    async def resolve(self, item_id: uuid.UUID) -> InboxItem | None:
        item = await self.get(item_id)
        if item is None:
            return None
        self._resolve(item)
        return item

    @staticmethod
    def _resolve(item: InboxItem, handled_elsewhere: bool = False) -> None:
        """Settled. An item that was read is filed; one whose matter was settled somewhere else
        (the question answered on the memory page) counts as read, too."""
        now = _now()
        if item.resolved_at is None:
            item.resolved_at = now
        if handled_elsewhere and item.read_at is None:
            item.read_at = now
        if item.read_at is not None and not item.pinned and item.filed_at is None:
            item.filed_at = now

    async def delete(self, item_id: uuid.UUID) -> bool:
        result = await self.db.execute(
            delete(InboxItem).where(self._mine(), InboxItem.id == item_id)
        )
        return bool(result.rowcount)

    async def read_all(self, application: str | None = None) -> int:
        """Mark every unread item read and file it, except what still waits for an action (an
        urgent item not yet settled). Returns how many were put away."""
        where = [self._mine(), InboxItem.read_at.is_(None)]
        if application:
            where.append(InboxItem.application == application)
        n = 0
        now = _now()
        for item in await self.db.scalars(select(InboxItem).where(*where)):
            if _holds(item):
                continue
            item.read_at = now
            if not item.pinned and item.filed_at is None:
                item.filed_at = now
            n += 1
        return n

    async def retire(self, kinds: Sequence[str]) -> int:
        """File the open items of kinds that make no item any more (read and settled)."""
        n = 0
        rows = await self.db.scalars(
            select(InboxItem).where(
                self._mine(),
                InboxItem.kind.in_(list(kinds)),
                or_(InboxItem.read_at.is_(None), InboxItem.resolved_at.is_(None)),
            )
        )
        for item in rows:
            self._resolve(item, handled_elsewhere=True)
            n += 1
        return n

    async def settle(self, kind: str, live: set[str]) -> int:
        """Resolve the open items of ``kind`` whose source is no longer in ``live``."""
        rows = await self.db.scalars(
            select(InboxItem).where(
                self._mine(),
                InboxItem.kind == kind,
                InboxItem.resolved_at.is_(None),
            )
        )
        n = 0
        for item in rows:
            if item.source_ref not in live:
                self._resolve(item, handled_elsewhere=True)
                n += 1
        return n


# --- looking at the sources -------------------------------------------------------------------


async def _applications(
    db: AsyncSession, user_id: uuid.UUID, ids: set[uuid.UUID]
) -> dict[uuid.UUID, str]:
    if not ids:
        return {}
    rows = await db.execute(
        select(Conversation.id, Conversation.application).where(
            Conversation.user_id == user_id, Conversation.id.in_(ids)
        )
    )
    return {cid: app or DEFAULT_APPLICATION for cid, app in rows}


async def sources(
    db: AsyncSession, user_id: uuid.UUID, proposals: Any | None
) -> dict[str, list[Candidate]]:
    """What is open or recent in the places the inbox looks at, by kind."""
    out: dict[str, list[Candidate]] = {k: [] for k in AUTO_RESOLVED}
    out["task_done"] = []

    for q in await MemoryStore(db, user_id).questions("open"):
        context = q.context or {}
        out["memory_question"].append(
            Candidate(
                "memory_question",
                "memory_question",
                str(q.id),
                q.topic or "Question",
                str(context.get("question") or ""),
                urgent=True,
                ref={"module": "page.memory"},
            )
        )

    for p in await MemoryStore(db, user_id).proposals("proposed"):
        out["instruction_proposal"].append(
            Candidate(
                "instruction_proposal",
                "memory_proposal",
                str(p.id),
                (p.text or p.action or "Proposal")[:120],
                p.reason or "",
                urgent=True,
                ref={"module": "page.instructions"},
            )
        )

    if proposals is not None:
        for p in await proposals.pending(user_id):
            out["proposal"].append(
                Candidate(
                    "proposal",
                    "agent_proposal",
                    p["id"],
                    f"{p.get('capability') or 'Action'} @ {p.get('node') or '?'}",
                    p.get("effect") or p.get("reason") or "",
                    application="admin",
                    urgent=True,
                    ref={"module": "page.tasks", "props": {"id": p.get("task_id") or ""}},
                )
            )

    reviews = list(await ReviewStore(db, user_id).unseen())
    task_rows = [
        t
        for t in await AgentStore(db, user_id).list(limit=60, origin=None)
        if t.deadline_at is not None
        and t.status in ("done", "failed")
        and (t.finished_at is None or t.finished_at >= _now() - RECENT_TASKS)
    ]
    apps = await _applications(
        db,
        user_id,
        {r.conversation_id for r in reviews}
        | {t.conversation_id for t in task_rows if t.conversation_id},
    )
    fresh = _now() - RECENT_REVIEWS
    for r in reviews:
        # only a fresh mistake makes an item; a note is shown at the answer (RETIRED_KINDS)
        if r.verdict != "error" or (r.created_at is not None and r.created_at < fresh):
            continue
        finding = (r.findings or [{}])[0] if r.findings else {}
        c = Candidate(
            "review_error",
            "chat_review",
            str(r.id),
            "A second look found a mistake",
            str(finding.get("claim") or "")[:300],
            application=apps.get(r.conversation_id, DEFAULT_APPLICATION),
            urgent=False,
            ref={"module": "page.chat", "props": {"id": str(r.conversation_id)}},
        )
        out.setdefault(c.kind, []).append(c)
    for t in task_rows:
        failed = t.status == "failed"
        c = Candidate(
            "task_failed" if failed else "task_done",
            "agent_task",
            str(t.id),
            t.title or "Task",
            (t.error if failed else t.summary) or "",
            application=apps.get(t.conversation_id, "admin") if t.conversation_id else "admin",
            urgent=failed,
            ref={"module": "page.tasks", "props": {"id": str(t.id)}},
        )
        out[c.kind].append(c)
    return out


async def sync(
    sessionmaker: async_sessionmaker[AsyncSession], user_id: uuid.UUID, proposals: Any | None = None
) -> bool:
    """Bring the person's items in line with the sources. True when something changed."""
    changed = False
    async with sessionmaker() as db:
        store = InboxStore(db, user_id)
        found = await sources(db, user_id, proposals)
        for candidates in found.values():
            for c in candidates:
                _, new = await store.add(c)
                changed = changed or new
        for kind in AUTO_RESOLVED:
            live = {c.source_ref for c in found.get(kind, []) if c.source_ref}
            changed = bool(await store.settle(kind, live)) or changed
        changed = bool(await store.retire(RETIRED_KINDS)) or changed
        await db.commit()
    return changed
