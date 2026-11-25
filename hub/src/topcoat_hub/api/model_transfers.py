"""Direct model transfers between nodes (E2-07, ARCHITECTURE D51): start, list, read, cancel.

Admin only (the maintainer confirms each transfer, D15); every row belongs to a person and is
only ever read through their own scope (D20)."""

import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, ConfigDict, Field

from topcoat_hub.auth import CurrentUser, require_admin
from topcoat_hub.model_transfers import TransferCoordinator, TransferError, view

router = APIRouter(prefix="/api/model-transfers")

ModelName = Annotated[str, Field(min_length=1, max_length=255)]


class TransferRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source_node_id: uuid.UUID
    target_node_id: uuid.UUID
    model: ModelName
    target_name: ModelName | None = None
    move: bool = False


def _error(status: int, code: str, message: str) -> HTTPException:
    return HTTPException(status, detail={"code": code, "message": message})


def _coordinator(request: Request) -> TransferCoordinator:
    return request.app.state.transfers


def _http(exc: TransferError) -> HTTPException:
    return _error(exc.status, exc.code, exc.message)


@router.post("", status_code=201)
async def create_transfer(
    body: TransferRequest, request: Request, user: CurrentUser = Depends(require_admin)
) -> dict[str, Any]:
    try:
        row = await _coordinator(request).create(
            uuid.UUID(user.id),
            body.source_node_id,
            body.target_node_id,
            body.model,
            body.target_name,
            body.move,
        )
    except TransferError as exc:
        raise _http(exc) from exc
    return view(row)


@router.get("")
async def list_transfers(
    request: Request,
    node_id: Annotated[uuid.UUID | None, Query()] = None,
    user: CurrentUser = Depends(require_admin),
) -> dict[str, Any]:
    rows = await _coordinator(request).list(uuid.UUID(user.id), node_id)
    return {"transfers": [view(r) for r in rows]}


@router.get("/{transfer_id}")
async def get_transfer(
    transfer_id: uuid.UUID, request: Request, user: CurrentUser = Depends(require_admin)
) -> dict[str, Any]:
    row = await _coordinator(request).get(uuid.UUID(user.id), transfer_id)
    if row is None:
        raise _error(404, "not_found", "unknown transfer")
    return view(row)


@router.post("/{transfer_id}/cancel")
async def cancel_transfer(
    transfer_id: uuid.UUID, request: Request, user: CurrentUser = Depends(require_admin)
) -> dict[str, Any]:
    try:
        row = await _coordinator(request).cancel(uuid.UUID(user.id), transfer_id)
    except TransferError as exc:
        raise _http(exc) from exc
    return view(row)
