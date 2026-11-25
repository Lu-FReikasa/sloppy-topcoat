"""Marks on inbox items (docs/PLATFORM.md, 3.7): a comment, a Post-It, a sticker, a highlight.

A mark is data **on** an item, never a change **of** it: the text of a report is not rewritten by
anybody. It is written by the person or by an application (the re-check leaves "still correct" and
"outdated" marks), belongs to the person like the item it sits on (D20), and can be resolved (the
Post-It is dealt with) or removed."""

import uuid
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from topcoat_hub.models import InboxItem, ItemMark

KINDS = ("comment", "postit", "sticker", "highlight")
MAX_TEXT = 500
PERSON = "person"
RECHECK = "recheck"


class MarkError(Exception):
    def __init__(self, status: int, code: str, message: str) -> None:
        super().__init__(message)
        self.status, self.code, self.message = status, code, message


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


def mark_view(m: ItemMark) -> dict[str, Any]:
    return {
        "id": str(m.id),
        "item_id": str(m.item_id),
        "author": m.author,
        "kind": m.kind,
        "text": m.text,
        "line": m.line,
        "created_at": _iso(m.created_at),
        "resolved_at": _iso(m.resolved_at),
    }


class MarkStore:
    """Everything marks keep, scoped to one person."""

    def __init__(self, db: AsyncSession, user_id: uuid.UUID) -> None:
        self.db, self.user_id = db, user_id

    async def add(
        self,
        item_id: uuid.UUID,
        kind: str,
        text: str = "",
        *,
        author: str = PERSON,
        line: int | None = None,
    ) -> ItemMark:
        if kind not in KINDS:
            raise MarkError(422, "kind", f"a mark is one of: {', '.join(KINDS)}")
        text = " ".join(text.split())[:MAX_TEXT] if kind != "comment" else text.strip()[:MAX_TEXT]
        if not text and kind != "highlight":
            raise MarkError(422, "empty", "this kind of mark needs a text")
        if line is not None and not 0 <= line <= 100_000:
            raise MarkError(422, "line", "there is no such line")
        item = await self.db.scalar(
            select(InboxItem).where(InboxItem.id == item_id, InboxItem.user_id == self.user_id)
        )
        if item is None:
            raise MarkError(404, "not_found", "there is no such item")
        mark = ItemMark(
            user_id=self.user_id,
            item_id=item_id,
            author=author[:40],
            kind=kind,
            text=text,
            line=line,
        )
        self.db.add(mark)
        await self.db.flush()
        await self.db.refresh(mark)
        return mark

    async def for_items(self, item_ids: Sequence[uuid.UUID]) -> dict[str, list[dict[str, Any]]]:
        """The marks of these items (the person's own), oldest first, keyed by item id."""
        out: dict[str, list[dict[str, Any]]] = {}
        if not item_ids:
            return out
        rows = await self.db.scalars(
            select(ItemMark)
            .where(ItemMark.user_id == self.user_id, ItemMark.item_id.in_(item_ids))
            .order_by(ItemMark.created_at, ItemMark.id)
        )
        for m in rows:
            out.setdefault(str(m.item_id), []).append(mark_view(m))
        return out

    async def get(self, mark_id: uuid.UUID) -> ItemMark | None:
        return await self.db.scalar(
            select(ItemMark).where(ItemMark.id == mark_id, ItemMark.user_id == self.user_id)
        )

    async def resolve(self, mark_id: uuid.UUID, resolved: bool = True) -> ItemMark | None:
        mark = await self.get(mark_id)
        if mark is None:
            return None
        mark.resolved_at = datetime.now(UTC) if resolved else None
        await self.db.flush()
        return mark

    async def delete(self, mark_id: uuid.UUID) -> bool:
        result = await self.db.execute(
            delete(ItemMark).where(ItemMark.id == mark_id, ItemMark.user_id == self.user_id)
        )
        return bool(result.rowcount)  # type: ignore[attr-defined]

    async def drop_old_recheck(self, item_id: uuid.UUID, keep: int = 3) -> int:
        """The re-check leaves a mark each time; the oldest of its own are dropped so a pinned
        item does not collect one a day for ever."""
        rows = list(
            await self.db.scalars(
                select(ItemMark)
                .where(
                    ItemMark.user_id == self.user_id,
                    ItemMark.item_id == item_id,
                    ItemMark.author == RECHECK,
                )
                .order_by(ItemMark.created_at.desc(), ItemMark.id)
            )
        )
        for m in rows[keep:]:
            await self.db.delete(m)
        return max(0, len(rows) - keep)
