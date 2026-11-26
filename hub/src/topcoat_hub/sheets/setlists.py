"""The setlists of one person (D20: every query is scoped by ``user_id``; another person's setlist
or sheet is "not found", whatever its id).

A setlist is the order of songs for one evening. Each item names a sheet of the same person and
says whether that version is played or the newest version of its song; which version is newest is
decided where the songs are grouped (the reader), so the hub stores the choice, not the answer.
The items are written as a whole: adding, removing and reordering are one call with the new order.
"""

import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from topcoat_hub.models import SetlistItemRow, SetlistRow, SheetRow
from topcoat_hub.sheets.store import SheetError

MAX_ITEMS = 200
MAX_SETLISTS = 500


@dataclass(frozen=True)
class Item:
    sheet_id: uuid.UUID
    newest: bool = False


def _name(name: str) -> str:
    name = name.strip()
    if not name:
        raise SheetError(422, "invalid", "a setlist needs a name")
    return name[:200]


class SetlistStore:
    def __init__(self, db: AsyncSession, user_id: uuid.UUID) -> None:
        self.db = db
        self.user_id = user_id

    async def setlists(self) -> Sequence[SetlistRow]:
        stmt = (
            select(SetlistRow)
            .where(SetlistRow.user_id == self.user_id)
            .order_by(SetlistRow.updated_at.desc(), SetlistRow.name)
        )
        return (await self.db.scalars(stmt)).all()

    async def items(
        self, setlist_ids: Sequence[uuid.UUID]
    ) -> dict[uuid.UUID, list[SetlistItemRow]]:
        """The items of these setlists, each list in playing order."""
        out: dict[uuid.UUID, list[SetlistItemRow]] = {i: [] for i in setlist_ids}
        if not setlist_ids:
            return out
        stmt = (
            select(SetlistItemRow)
            .where(
                SetlistItemRow.user_id == self.user_id,
                SetlistItemRow.setlist_id.in_(list(setlist_ids)),
            )
            .order_by(SetlistItemRow.setlist_id, SetlistItemRow.position)
        )
        for row in (await self.db.scalars(stmt)).all():
            out[row.setlist_id].append(row)
        return out

    async def get(self, setlist_id: uuid.UUID) -> SetlistRow:
        row = await self.db.scalar(
            select(SetlistRow).where(
                SetlistRow.id == setlist_id, SetlistRow.user_id == self.user_id
            )
        )
        if row is None:
            raise SheetError(404, "not_found", "there is no such setlist")
        return row

    async def create(self, name: str, items: Sequence[Item] = ()) -> SetlistRow:
        count = len(await self.setlists())
        if count >= MAX_SETLISTS:
            raise SheetError(422, "invalid", f"a person has at most {MAX_SETLISTS} setlists")
        row = SetlistRow(user_id=self.user_id, name=_name(name))
        self.db.add(row)
        await self.db.flush()
        await self._write_items(row, items)
        return row

    async def update(
        self, setlist_id: uuid.UUID, *, name: str | None = None, items: Sequence[Item] | None = None
    ) -> SetlistRow:
        row = await self.get(setlist_id)
        if name is not None:
            row.name = _name(name)
        if items is not None:
            await self._write_items(row, items)
        row.updated_at = datetime.now(UTC)
        await self.db.flush()
        return row

    async def delete(self, setlist_id: uuid.UUID) -> None:
        row = await self.get(setlist_id)
        await self.db.delete(row)
        await self.db.flush()

    async def _write_items(self, row: SetlistRow, items: Sequence[Item]) -> None:
        if len(items) > MAX_ITEMS:
            raise SheetError(422, "invalid", f"a setlist has at most {MAX_ITEMS} songs")
        wanted = {i.sheet_id for i in items}
        if wanted:
            found = set(
                (
                    await self.db.scalars(
                        select(SheetRow.id).where(
                            SheetRow.user_id == self.user_id, SheetRow.id.in_(wanted)
                        )
                    )
                ).all()
            )
            if found != wanted:
                raise SheetError(404, "not_found", "there is no such sheet")
        await self.db.execute(
            delete(SetlistItemRow).where(
                SetlistItemRow.setlist_id == row.id, SetlistItemRow.user_id == self.user_id
            )
        )
        for position, item in enumerate(items):
            self.db.add(
                SetlistItemRow(
                    setlist_id=row.id,
                    user_id=self.user_id,
                    sheet_id=item.sheet_id,
                    position=position,
                    newest=item.newest,
                )
            )
        await self.db.flush()
