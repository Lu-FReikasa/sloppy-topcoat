"""The incognito folder (docs/PLATFORM.md, section 3.9). Nothing here touches the database: the
tabs live in the memory of the hub and are the caller's own (D20)."""

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field

from topcoat_hub.auth import CurrentUser, api_user
from topcoat_hub.incognito import MAX_MESSAGE_CHARS, IncognitoError, IncognitoService, Tab

router = APIRouter(prefix="/api/incognito")


def _service(request: Request) -> IncognitoService:
    service: IncognitoService = request.app.state.incognito
    return service


def _fail(exc: IncognitoError) -> HTTPException:
    return HTTPException(exc.status, detail={"code": exc.code, "message": exc.message})


def _tab(request: Request, user: CurrentUser, tab_id: str) -> Tab:
    tab = _service(request).store.get(user.uuid, tab_id)
    if tab is None:
        raise HTTPException(
            404, detail={"code": "not_found", "message": "there is no such incognito tab"}
        )
    return tab


class TabCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    internet: bool = True


class TabUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    internet: bool


class Message(BaseModel):
    model_config = ConfigDict(extra="forbid")

    content: str = Field(min_length=1, max_length=MAX_MESSAGE_CHARS)


@router.get("")
async def listing(request: Request, user: CurrentUser = Depends(api_user)) -> dict[str, Any]:
    return {"tabs": [t.view() for t in _service(request).store.listing(user.uuid)]}


@router.post("", status_code=201)
async def create(
    body: TabCreate, request: Request, user: CurrentUser = Depends(api_user)
) -> dict[str, Any]:
    try:
        return _service(request).store.create(user.uuid, body.internet).view(True)
    except IncognitoError as exc:
        raise _fail(exc) from exc


@router.get("/{tab_id}")
async def get_tab(
    tab_id: str, request: Request, user: CurrentUser = Depends(api_user)
) -> dict[str, Any]:
    return _tab(request, user, tab_id).view(True)


@router.patch("/{tab_id}")
async def update_tab(
    tab_id: str, body: TabUpdate, request: Request, user: CurrentUser = Depends(api_user)
) -> dict[str, Any]:
    tab = _tab(request, user, tab_id)
    tab.internet = body.internet
    return tab.view(True)


@router.post("/{tab_id}/messages")
async def send(
    tab_id: str, body: Message, request: Request, user: CurrentUser = Depends(api_user)
) -> dict[str, Any]:
    """Ask in a tab; the answer comes back whole (nothing of it is streamed to a store)."""
    tab = _tab(request, user, tab_id)
    try:
        answer = await _service(request).reply(user, tab, body.content)
    except IncognitoError as exc:
        raise _fail(exc) from exc
    return {"message": answer, "tab": tab.view()}


@router.delete("/{tab_id}", status_code=204)
async def delete_tab(tab_id: str, request: Request, user: CurrentUser = Depends(api_user)) -> None:
    if not _service(request).store.delete(user.uuid, tab_id):
        raise HTTPException(
            404, detail={"code": "not_found", "message": "there is no such incognito tab"}
        )


@router.delete("", status_code=204)
async def delete_all(request: Request, user: CurrentUser = Depends(api_user)) -> None:
    _service(request).store.delete_all(user.uuid)
