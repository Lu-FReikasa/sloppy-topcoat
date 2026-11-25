"""The inbox of the signed-in person (docs/PLATFORM.md, 3.7). Every route is scoped to the caller
(D20): somebody else's item is simply not found."""

import uuid
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, ConfigDict

from topcoat_hub import inbox
from topcoat_hub.auth import CurrentUser, api_user
from topcoat_hub.inbox import InboxStore, item_view
from topcoat_hub.marks import MarkError, MarkStore, mark_view

router = APIRouter(prefix="/api/inbox")


def _not_found() -> HTTPException:
    return HTTPException(404, detail={"code": "not_found", "message": "there is no such item"})


def _changed(request: Request, user: CurrentUser) -> None:
    request.app.state.bus.publish("inbox", {"_user": user.id})


async def _look(request: Request, user: CurrentUser) -> None:
    """Bring the items in line with their sources; the other tabs hear about new ones."""
    state = request.app.state
    if await inbox.sync(state.sessionmaker, user.uuid, getattr(state, "proposals", None)):
        _changed(request, user)


@router.get("")
async def listing(
    request: Request,
    view: str = Query("inbox", pattern="^(inbox|board|filed)$"),
    application: str | None = Query(None, max_length=40),
    user: CurrentUser = Depends(api_user),
) -> dict[str, Any]:
    """``inbox``: unread items and urgent ones that are not settled; ``board``: pinned ones;
    ``filed``: read ones, put away in the corner of their application."""
    await _look(request, user)
    async with request.app.state.sessionmaker() as db:
        store = InboxStore(db, user.uuid)
        items = await store.listing(view, application)
        marks = await MarkStore(db, user.uuid).for_items([i.id for i in items])
        return {
            "items": [{**item_view(i), "marks": marks.get(str(i.id), [])} for i in items],
            "counts": await store.counts(),
        }


@router.get("/counts")
async def counts(request: Request, user: CurrentUser = Depends(api_user)) -> dict[str, Any]:
    await _look(request, user)
    async with request.app.state.sessionmaker() as db:
        return await InboxStore(db, user.uuid).counts()


@router.post("/read-all")
async def read_all(
    request: Request, application: str | None = None, user: CurrentUser = Depends(api_user)
) -> dict[str, Any]:
    """Mark everything read and file it (of one application, if named); what still waits for an
    action stays in the inbox."""
    async with request.app.state.sessionmaker() as db:
        n = await InboxStore(db, user.uuid).read_all(application)
        await db.commit()
    _changed(request, user)
    return {"filed": n}


@router.post("/{item_id}/open")
async def open_item(
    item_id: uuid.UUID, request: Request, user: CurrentUser = Depends(api_user)
) -> dict[str, Any]:
    """Opening an item marks it read; it is filed unless pinned or still urgent."""
    async with request.app.state.sessionmaker() as db:
        item = await InboxStore(db, user.uuid).open(item_id)
        if item is None:
            raise _not_found()
        await db.commit()
        view = item_view(item)
    _changed(request, user)
    return view


class PinBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    pinned: bool = True


@router.post("/{item_id}/pin")
async def pin_item(
    item_id: uuid.UUID, body: PinBody, request: Request, user: CurrentUser = Depends(api_user)
) -> dict[str, Any]:
    async with request.app.state.sessionmaker() as db:
        item = await InboxStore(db, user.uuid).pin(item_id, body.pinned)
        if item is None:
            raise _not_found()
        await db.commit()
        view = item_view(item)
    _changed(request, user)
    return view


@router.post("/{item_id}/resolve")
async def resolve_item(
    item_id: uuid.UUID, request: Request, user: CurrentUser = Depends(api_user)
) -> dict[str, Any]:
    """The person says the matter is settled (a failure acknowledged)."""
    async with request.app.state.sessionmaker() as db:
        item = await InboxStore(db, user.uuid).resolve(item_id)
        if item is None:
            raise _not_found()
        await db.commit()
        view = item_view(item)
    _changed(request, user)
    return view


@router.delete("/{item_id}", status_code=204)
async def delete_item(
    item_id: uuid.UUID, request: Request, user: CurrentUser = Depends(api_user)
) -> None:
    async with request.app.state.sessionmaker() as db:
        if not await InboxStore(db, user.uuid).delete(item_id):
            raise _not_found()
        await db.commit()
    _changed(request, user)


# --- marks (docs/PLATFORM.md, 3.7): data on an item, never a change of its text ---------------


class MarkBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: str
    text: str = ""
    line: int | None = None


def _mark_failure(exc: MarkError) -> HTTPException:
    return HTTPException(exc.status, detail={"code": exc.code, "message": exc.message})


@router.get("/{item_id}/marks")
async def item_marks(
    item_id: uuid.UUID, request: Request, user: CurrentUser = Depends(api_user)
) -> dict[str, Any]:
    async with request.app.state.sessionmaker() as db:
        if await InboxStore(db, user.uuid).get(item_id) is None:
            raise _not_found()
        found = await MarkStore(db, user.uuid).for_items([item_id])
        return {"marks": found.get(str(item_id), [])}


@router.post("/{item_id}/marks", status_code=201)
async def add_mark(
    item_id: uuid.UUID, body: MarkBody, request: Request, user: CurrentUser = Depends(api_user)
) -> dict[str, Any]:
    async with request.app.state.sessionmaker() as db:
        try:
            mark = await MarkStore(db, user.uuid).add(item_id, body.kind, body.text, line=body.line)
        except MarkError as exc:
            raise _mark_failure(exc) from exc
        await db.commit()
        return mark_view(mark)


@router.post("/marks/{mark_id}/resolve")
async def resolve_mark(
    mark_id: uuid.UUID,
    request: Request,
    resolved: bool = Query(True),
    user: CurrentUser = Depends(api_user),
) -> dict[str, Any]:
    async with request.app.state.sessionmaker() as db:
        mark = await MarkStore(db, user.uuid).resolve(mark_id, resolved)
        if mark is None:
            raise _not_found()
        await db.commit()
        return mark_view(mark)


@router.delete("/marks/{mark_id}", status_code=204)
async def delete_mark(
    mark_id: uuid.UUID, request: Request, user: CurrentUser = Depends(api_user)
) -> None:
    async with request.app.state.sessionmaker() as db:
        if not await MarkStore(db, user.uuid).delete(mark_id):
            raise _not_found()
        await db.commit()
