"""The sheets of one person (D20: every query is scoped by ``user_id``; another person's sheet is
"not found", whatever its id).

A sheet is stored normalised: parsed and written again in the stored form (sounding pitch,
``{x_capo}``, a key, estimated if the text names none), so every reader sees the same text and the
list can show key and capo without parsing. A plain "chords over lyrics" text is turned into
ChordPro first.
"""

import uuid
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from topcoat_hub.models import SheetRow
from topcoat_hub.sheets import chordpro, compare, plaintext, rules
from topcoat_hub.sheets.chordpro import Sheet
from topcoat_hub.sheets.render import INSTRUMENTS, sounding_key

FORMATS = ("chordpro", "plain")
MAX_TEXT = 100_000


class SheetError(ValueError):
    def __init__(self, status: int, code: str, message: str) -> None:
        super().__init__(message)
        self.status, self.code, self.message = status, code, message


def read(text: str, fmt: str = "chordpro", title: str = "", artist: str = "") -> Sheet:
    """A text as a sheet: ChordPro, or a plain chord page (``fmt="plain"``)."""
    if fmt not in FORMATS:
        raise SheetError(422, "invalid", f"format is one of {', '.join(FORMATS)}")
    if not text.strip():
        raise SheetError(422, "invalid", "the sheet is empty")
    if len(text) > MAX_TEXT:
        raise SheetError(422, "invalid", f"a sheet has at most {MAX_TEXT} characters")
    if fmt == "plain":
        text = plaintext.to_chordpro(text)
    sheet = chordpro.parse(text)
    sheet.title = title.strip() or sheet.title
    sheet.artist = artist.strip() or sheet.artist
    rules.apply(
        sheet, chordpro.writes_german(text)
    )  # chords written as text, tab blocks, a header (sheets/rules.py)
    if not sheet.chords():
        raise SheetError(422, "invalid", "the sheet has no chords")
    if sheet.key is None:
        sheet.key, _ = sounding_key(sheet)
    if not 0 <= sheet.capo <= 12:
        raise SheetError(422, "invalid", "the capo is between 0 and 12")
    return sheet


def _check_instrument(instrument: str) -> None:
    if instrument not in INSTRUMENTS:
        raise SheetError(422, "invalid", f"instrument is one of {', '.join(INSTRUMENTS)}")


def _source_sheet(source: dict[str, Any]) -> Sheet:
    return chordpro.parse(str(source.get("chordpro") or ""))


class SheetStore:
    def __init__(self, db: AsyncSession, user_id: uuid.UUID) -> None:
        self.db = db
        self.user_id = user_id

    async def sheets(self) -> Sequence[SheetRow]:
        stmt = (
            select(SheetRow)
            .where(SheetRow.user_id == self.user_id)
            .order_by(SheetRow.updated_at.desc(), SheetRow.title)
        )
        return (await self.db.scalars(stmt)).all()

    async def get(self, sheet_id: uuid.UUID) -> SheetRow:
        row = await self.db.scalar(
            select(SheetRow).where(SheetRow.id == sheet_id, SheetRow.user_id == self.user_id)
        )
        if row is None:
            raise SheetError(404, "not_found", "there is no such sheet")
        return row

    def _store(self, row: SheetRow, sheet: Sheet) -> None:
        if not sheet.title:
            raise SheetError(422, "invalid", "a sheet needs a title")
        row.title = sheet.title[:200]
        row.artist = sheet.artist[:200]
        row.key = sheet.key.name if sheet.key else None
        row.capo = sheet.capo
        row.chordpro = chordpro.write(sheet)

    async def create(
        self,
        text: str,
        instrument: str = "guitar",
        fmt: str = "chordpro",
        title: str = "",
        artist: str = "",
        version_note: str = "",
        sources: list[dict[str, Any]] | None = None,
        report_id: uuid.UUID | None = None,
    ) -> SheetRow:
        _check_instrument(instrument)
        sheet = read(text, fmt, title, artist)
        row = SheetRow(
            user_id=self.user_id,
            instrument=instrument,
            capo_view=True,
            version_note=version_note[:300],
            sources=list(sources or []),
            report_id=report_id,
        )
        self._store(row, sheet)
        self.db.add(row)
        await self.db.flush()
        return row

    async def update(
        self,
        sheet_id: uuid.UUID,
        *,
        text: str | None = None,
        fmt: str = "chordpro",
        instrument: str | None = None,
        capo_view: bool | None = None,
        capo: int | None = None,
        transpose: int = 0,
        title: str | None = None,
        artist: str | None = None,
        version_note: str | None = None,
    ) -> SheetRow:
        """Change a sheet. ``capo`` moves the capo and keeps the sound (other shapes);
        ``transpose`` moves the sound itself (the sheet is rewritten in the new key)."""
        row = await self.get(sheet_id)
        sheet = read(text, fmt) if text is not None else chordpro.parse(row.chordpro)
        if text is not None and not sheet.title:
            sheet.title = row.title
        if instrument is not None:
            _check_instrument(instrument)
            row.instrument = instrument
        if capo_view is not None:
            row.capo_view = capo_view
        if capo is not None:
            if not 0 <= capo <= 12:
                raise SheetError(422, "invalid", "the capo is between 0 and 12")
            sheet.capo = capo
        if transpose % 12:
            sheet = sheet.transpose(transpose)
        if title is not None:
            sheet.title = title.strip()
        if artist is not None:
            sheet.artist = artist.strip()
        if version_note is not None:
            row.version_note = version_note[:300]
        self._store(row, sheet)
        row.updated_at = datetime.now(UTC)
        await self.db.flush()
        return row

    async def delete(self, sheet_id: uuid.UUID) -> None:
        row = await self.get(sheet_id)
        await self.db.delete(row)
        await self.db.flush()

    async def compare(
        self, sheet_id: uuid.UUID, source: int | None = None, other: uuid.UUID | None = None
    ) -> dict[str, Any]:
        """The sheet against one of its sources (by index) or another sheet of the person."""
        row = await self.get(sheet_id)
        a = chordpro.parse(row.chordpro)
        if (source is None) == (other is None):
            raise SheetError(422, "invalid", "compare with one source or one other sheet")
        if other is not None:
            b = chordpro.parse((await self.get(other)).chordpro)
            against: dict[str, Any] = {"sheet": str(other)}
        else:
            sources = list(row.sources or [])
            if source is None or not 0 <= source < len(sources):
                raise SheetError(404, "not_found", "there is no such source")
            b = _source_sheet(sources[source])
            against = {"source": source, "url": sources[source].get("url")}
        return {"against": against, **compare.compare(a, b).to_dict()}
