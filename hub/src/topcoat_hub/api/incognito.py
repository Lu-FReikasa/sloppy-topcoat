"""The incognito folder (docs/PLATFORM.md, section 3.9). Nothing here touches the database: the
tabs live in the memory of the hub and are the caller's own (D20)."""

import asyncio
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import Response
from pydantic import BaseModel, ConfigDict, Field, model_validator

from topcoat_hub import chat_images
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

    content: str = Field("", max_length=MAX_MESSAGE_CHARS)
    # D125: re-encoded like the chat's, kept in the tab in memory only
    images: list[Annotated[str, Field(max_length=chat_images.MAX_UPLOAD_CHARS)]] = Field(
        default_factory=list, max_length=chat_images.MAX_PER_MESSAGE
    )

    @model_validator(mode="after")
    def _says_something(self) -> "Message":
        if not self.content and not self.images:
            raise ValueError("a message needs text or an image")
        return self


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
        images = [
            await asyncio.to_thread(chat_images.reencode, chat_images.decode_upload(i))
            for i in body.images
        ]
        answer = await _service(request).reply(user, tab, body.content, images)
    except chat_images.ImageError as exc:
        raise HTTPException(422, detail={"code": exc.code, "message": exc.message}) from exc
    except IncognitoError as exc:
        raise _fail(exc) from exc
    return {"message": answer, "asked": tab.messages[-2], "tab": tab.view()}


@router.get("/{tab_id}/images/{image_id}")
async def get_image(
    tab_id: str, image_id: str, request: Request, user: CurrentUser = Depends(api_user)
) -> Response:
    """An image of one of the person's tabs, from memory."""
    image = _tab(request, user, tab_id).images.get(image_id)
    if image is None:
        raise HTTPException(404, detail={"code": "not_found", "message": "unknown image"})
    return Response(
        content=image.data, media_type=image.content_type, headers=chat_images.IMAGE_HEADERS
    )


@router.delete("/{tab_id}", status_code=204)
async def delete_tab(tab_id: str, request: Request, user: CurrentUser = Depends(api_user)) -> None:
    if not _service(request).store.delete(user.uuid, tab_id):
        raise HTTPException(
            404, detail={"code": "not_found", "message": "there is no such incognito tab"}
        )


@router.delete("", status_code=204)
async def delete_all(request: Request, user: CurrentUser = Depends(api_user)) -> None:
    _service(request).store.delete_all(user.uuid)
