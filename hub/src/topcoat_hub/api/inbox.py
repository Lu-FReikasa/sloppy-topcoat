"""The inbox of the signed-in person (docs/PLATFORM.md, 3.7). Every route is scoped to the caller
(D20): somebody else's item is simply not found."""

import uuid
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request

from topcoat_hub import inbox
from topcoat_hub.auth import CurrentUser, api_user
from topcoat_hub.inbox import InboxStore, item_view

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
    application: str | None = Query(None, max_length=40),
    user: CurrentUser = Depends(api_user),
) -> dict[str, Any]:
    """Unread items and urgent ones that are not settled, in their groups (decide, errors,
    results). A read item is gone from here; the thing it is about stays in its application."""
    await _look(request, user)
    async with request.app.state.sessionmaker() as db:
        store = InboxStore(db, user.uuid)
        items = await store.listing(application)
        return {"items": [item_view(i) for i in items], "counts": await store.counts()}


@router.get("/counts")
async def counts(request: Request, user: CurrentUser = Depends(api_user)) -> dict[str, Any]:
    await _look(request, user)
    async with request.app.state.sessionmaker() as db:
        return await InboxStore(db, user.uuid).counts()


@router.post("/read-all")
async def read_all(
    request: Request, application: str | None = None, user: CurrentUser = Depends(api_user)
) -> dict[str, Any]:
    """Mark everything read (of one application, if named); what still waits for an action stays
    in the inbox."""
    async with request.app.state.sessionmaker() as db:
        n = await InboxStore(db, user.uuid).read_all(application)
        await db.commit()
    _changed(request, user)
    return {"read": n}


@router.post("/{item_id}/open")
async def open_item(
    item_id: uuid.UUID, request: Request, user: CurrentUser = Depends(api_user)
) -> dict[str, Any]:
    """Opening an item marks it read; it leaves the inbox unless it is urgent and not settled."""
    async with request.app.state.sessionmaker() as db:
        item = await InboxStore(db, user.uuid).open(item_id)
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
