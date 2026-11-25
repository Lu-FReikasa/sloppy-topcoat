"""Hand-over between applications (docs/PLATFORM.md, section 3.8): the dossier is prepared, shown,
and sent exactly as shown."""

import uuid
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field

from topcoat_hub.auth import CurrentUser, api_user
from topcoat_hub.handover import HandoverError, HandoverService, view

router = APIRouter(prefix="/api/handovers")


def _service(request: Request) -> HandoverService:
    service: HandoverService | None = getattr(request.app.state, "handovers", None)
    if service is None:
        raise HTTPException(
            503,
            detail={"code": "agent_disabled", "message": "the agent is switched off on this hub"},
        )
    return service


def _fail(exc: HandoverError) -> HTTPException:
    return HTTPException(exc.status, detail={"code": exc.code, "message": exc.message})


class Prepare(BaseModel):
    model_config = ConfigDict(extra="forbid")

    to_application: str = Field(min_length=1, max_length=40)
    conversation_id: uuid.UUID | None = None
    incognito_tab: str | None = Field(default=None, max_length=64)
    note: str | None = Field(default=None, max_length=2000)


class Send(BaseModel):
    model_config = ConfigDict(extra="forbid")

    hash: str = Field(min_length=64, max_length=64)
    # the person's explicit approval for a dossier that holds a secret (only that click)
    approve_critical: bool = False


@router.get("")
async def listing(request: Request, user: CurrentUser = Depends(api_user)) -> dict[str, Any]:
    rows = await _service(request).listing(user.uuid)
    return {"handovers": [view(h) for h in rows]}


@router.post("", status_code=201)
async def prepare(
    body: Prepare, request: Request, user: CurrentUser = Depends(api_user)
) -> dict[str, Any]:
    try:
        row = await _service(request).prepare(
            user,
            body.to_application,
            conversation_id=body.conversation_id,
            incognito_tab=body.incognito_tab,
            note=body.note,
        )
    except HandoverError as exc:
        raise _fail(exc) from exc
    return view(row)


@router.get("/{handover_id}")
async def get(
    handover_id: uuid.UUID, request: Request, user: CurrentUser = Depends(api_user)
) -> dict[str, Any]:
    row = await _service(request).get(user.uuid, handover_id)
    if row is None:
        raise HTTPException(404, detail={"code": "not_found", "message": "no such hand-over"})
    return view(row)


@router.get("/{handover_id}/file")
async def file(
    handover_id: uuid.UUID, request: Request, user: CurrentUser = Depends(api_user)
) -> dict[str, Any]:
    """The Akte of a hand-over: the request, the report with its sources and the steps, as data
    and as the Markdown text the person reads."""
    row = await _service(request).file(user.uuid, handover_id)
    if row is None:
        raise HTTPException(404, detail={"code": "not_found", "message": "no such hand-over"})
    return {
        "id": str(row.id),
        "markdown": row.file_markdown,
        "file": row.file,
        "knowledge_path": row.knowledge_path,
    }


@router.post("/{handover_id}/send")
async def send(
    handover_id: uuid.UUID, body: Send, request: Request, user: CurrentUser = Depends(api_user)
) -> dict[str, Any]:
    try:
        sent = await _service(request).send(
            user, handover_id, body.hash, approved_critical=body.approve_critical
        )
        return view(sent)
    except HandoverError as exc:
        raise _fail(exc) from exc


@router.post("/{handover_id}/cancel")
async def cancel(
    handover_id: uuid.UUID, request: Request, user: CurrentUser = Depends(api_user)
) -> dict[str, Any]:
    try:
        return view(await _service(request).cancel(user, handover_id))
    except HandoverError as exc:
        raise _fail(exc) from exc
