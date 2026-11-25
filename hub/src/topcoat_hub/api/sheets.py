"""The program Sheets (``topcoat_hub.sheets``): the person's chord sheets, a sheet as an instrument
shows it, and a sheet compared with one of its sources or with another sheet. Every route is
scoped to the caller (D20): another person's sheet is a 404, whatever its id."""

import uuid
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.ext.asyncio import AsyncSession

from topcoat_hub.auth import CurrentUser, api_user
from topcoat_hub.db import get_db
from topcoat_hub.models import SheetRow
from topcoat_hub.sheets import chordpro
from topcoat_hub.sheets.render import render
from topcoat_hub.sheets.research import Request as ResearchRequest
from topcoat_hub.sheets.store import MAX_TEXT, SheetError, SheetStore

router = APIRouter(prefix="/api/sheets")

Instrument = Literal["guitar", "piano"]


def _refused(exc: SheetError) -> HTTPException:
    return HTTPException(exc.status, detail={"code": exc.code, "message": exc.message})


def summary(row: SheetRow) -> dict[str, Any]:
    return {
        "id": str(row.id),
        "title": row.title,
        "artist": row.artist,
        "instrument": row.instrument,
        "key": row.key,
        "capo": row.capo,
        "capo_view": row.capo_view,
        "version_note": row.version_note,
        "sources": len(row.sources or []),
        "report_id": str(row.report_id) if row.report_id else None,
        "updated_at": row.updated_at.isoformat() if row.updated_at else None,
        "created_at": row.created_at.isoformat() if row.created_at else None,
    }


def full(
    row: SheetRow,
    instrument: str | None = None,
    capo_view: bool | None = None,
    transpose: int = 0,
) -> dict[str, Any]:
    """The sheet as its view shows it (the stored choices unless the call names others)."""
    shown = render(
        chordpro.parse(row.chordpro),
        instrument or row.instrument,
        row.capo_view if capo_view is None else capo_view,
        transpose,
    )
    sources = [
        {k: s.get(k) for k in ("url", "title", "version", "note")} for s in (row.sources or [])
    ]
    return {**summary(row), "view": shown, "source_list": sources}


@router.get("")
async def list_sheets(
    user: CurrentUser = Depends(api_user), db: AsyncSession = Depends(get_db)
) -> dict[str, Any]:
    return {"sheets": [summary(r) for r in await SheetStore(db, user.uuid).sheets()]}


class ResearchStart(BaseModel):
    """A research of the profile ``sheets``: the song, and for whom the sheet is."""

    model_config = ConfigDict(extra="forbid")

    song: str = Field(min_length=1, max_length=200)
    artist: str = Field("", max_length=200)
    instrument: Instrument = "guitar"
    wish: str = Field("", max_length=300)


@router.post("/research", status_code=202)
async def start_research(
    body: ResearchStart, request: Request, user: CurrentUser = Depends(api_user)
) -> dict[str, Any]:
    """Search the web for the song's chord sheets; the result is a sheet and a report."""
    service = request.app.state.sheet_research
    try:
        job = await service.start(
            user,
            ResearchRequest(body.song.strip(), body.artist.strip(), body.instrument, body.wish),
        )
    except SheetError as exc:
        raise _refused(exc) from exc
    return job.view()


@router.get("/research")
async def list_research(request: Request, user: CurrentUser = Depends(api_user)) -> dict[str, Any]:
    return {"jobs": [j.view() for j in request.app.state.sheet_research.jobs_of(user.uuid)]}


@router.get("/research/{job_id}")
async def get_research(
    job_id: uuid.UUID, request: Request, user: CurrentUser = Depends(api_user)
) -> dict[str, Any]:
    try:
        return request.app.state.sheet_research.job(user.uuid, job_id).view()
    except SheetError as exc:
        raise _refused(exc) from exc


class SheetCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    text: str = Field(min_length=1, max_length=MAX_TEXT)
    format: Literal["chordpro", "plain"] = "chordpro"
    instrument: Instrument = "guitar"
    title: str = Field("", max_length=200)
    artist: str = Field("", max_length=200)
    version_note: str = Field("", max_length=300)


@router.post("", status_code=201)
async def create_sheet(
    body: SheetCreate, user: CurrentUser = Depends(api_user), db: AsyncSession = Depends(get_db)
) -> dict[str, Any]:
    """A sheet from ChordPro or from a plain "chords over lyrics" text."""
    try:
        row = await SheetStore(db, user.uuid).create(
            body.text, body.instrument, body.format, body.title, body.artist, body.version_note
        )
    except SheetError as exc:
        raise _refused(exc) from exc
    await db.commit()
    await db.refresh(row)
    return full(row)


@router.get("/{sheet_id}")
async def get_sheet(
    sheet_id: uuid.UUID,
    instrument: Instrument | None = None,
    capo_view: bool | None = None,
    transpose: int = Query(default=0, ge=-11, le=11),
    user: CurrentUser = Depends(api_user),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """A sheet as a view shows it; ``instrument``, ``capo_view`` and ``transpose`` only change
    this view, not the stored sheet."""
    try:
        row = await SheetStore(db, user.uuid).get(sheet_id)
    except SheetError as exc:
        raise _refused(exc) from exc
    return full(row, instrument, capo_view, transpose)


class SheetUpdate(BaseModel):
    """Only the fields that are present change. ``capo`` keeps the sound (other shapes);
    ``transpose`` rewrites the sheet in another key."""

    model_config = ConfigDict(extra="forbid")

    text: str | None = Field(None, min_length=1, max_length=MAX_TEXT)
    format: Literal["chordpro", "plain"] = "chordpro"
    instrument: Instrument | None = None
    capo_view: bool | None = None
    capo: int | None = Field(None, ge=0, le=12)
    transpose: int = Field(0, ge=-11, le=11)
    title: str | None = Field(None, min_length=1, max_length=200)
    artist: str | None = Field(None, max_length=200)
    version_note: str | None = Field(None, max_length=300)


@router.patch("/{sheet_id}")
async def update_sheet(
    sheet_id: uuid.UUID,
    body: SheetUpdate,
    user: CurrentUser = Depends(api_user),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    try:
        row = await SheetStore(db, user.uuid).update(
            sheet_id,
            text=body.text,
            fmt=body.format,
            instrument=body.instrument,
            capo_view=body.capo_view,
            capo=body.capo,
            transpose=body.transpose,
            title=body.title,
            artist=body.artist,
            version_note=body.version_note,
        )
    except SheetError as exc:
        raise _refused(exc) from exc
    await db.commit()
    await db.refresh(row)
    return full(row)


@router.delete("/{sheet_id}", status_code=204)
async def delete_sheet(
    sheet_id: uuid.UUID, user: CurrentUser = Depends(api_user), db: AsyncSession = Depends(get_db)
) -> None:
    try:
        await SheetStore(db, user.uuid).delete(sheet_id)
    except SheetError as exc:
        raise _refused(exc) from exc
    await db.commit()


@router.get("/{sheet_id}/compare")
async def compare_sheet(
    sheet_id: uuid.UUID,
    source: int | None = Query(default=None, ge=0),
    other: uuid.UUID | None = None,
    user: CurrentUser = Depends(api_user),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """Exactly where the sheet and one of its sources (or another sheet) differ, after the
    transposition and the capo are taken out."""
    try:
        return await SheetStore(db, user.uuid).compare(sheet_id, source=source, other=other)
    except SheetError as exc:
        raise _refused(exc) from exc
