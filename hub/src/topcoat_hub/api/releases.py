"""Administration → Releases (NS-01, D80): builds, their artifacts, and publishing with a signature
the browser made. Everything here wants the signed-in admin (``require_admin``: the dev token, never
an API key); the one route a node calls is the artifact upload, signed like the update download."""

import uuid
from collections.abc import AsyncIterator
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, status
from pydantic import BaseModel, ConfigDict, Field

from topcoat_hub.api.node import authenticated_node
from topcoat_hub.auth import CurrentUser, require_admin
from topcoat_hub.models import Node
from topcoat_hub.releases import (
    MAX_ARTIFACT_BYTES,
    ReleaseError,
    ReleaseService,
    build_view,
)

router = APIRouter()


def _service(request: Request) -> ReleaseService:
    return request.app.state.releases  # type: ignore[no-any-return]


def _http(exc: ReleaseError) -> HTTPException:
    return HTTPException(exc.status, detail={"code": exc.code, "message": exc.message})


class StartBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: str = Field(max_length=16)
    ref: str = Field(min_length=1, max_length=200)
    version: str | None = Field(None, max_length=40)
    source: str = Field("release_repo", max_length=16)


class PublishBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    channel: str = Field(max_length=32)
    created: str = Field(max_length=20)
    # one signature line per platform; the private key never reaches the hub
    signatures: dict[str, str] = Field(max_length=8)


@router.get("/api/releases")
async def overview(request: Request, user: CurrentUser = Depends(require_admin)) -> dict[str, Any]:
    service = _service(request)
    updates = request.app.state.updates
    return {
        "enabled": service.enabled,
        "channel_enabled": updates.enabled,
        "key_ids": updates.key_ids,
        "build_nodes": await service.build_nodes() if service.enabled else [],
        "rollout_node": service.rollout_node or None,
        "builds": [build_view(b) for b in await service.recent(user.uuid)],
        "published": [r.public() for r in updates.releases()],
    }


@router.post("/api/releases/builds", status_code=status.HTTP_201_CREATED)
async def start_build(
    body: StartBody, request: Request, user: CurrentUser = Depends(require_admin)
) -> dict[str, Any]:
    try:
        build = await _service(request).start(
            user.uuid, kind=body.kind, ref=body.ref, version=body.version, source=body.source
        )
    except ReleaseError as exc:
        raise _http(exc) from exc
    return build_view(build)


@router.get("/api/releases/builds/{build_id}")
async def read_build(
    build_id: uuid.UUID, request: Request, user: CurrentUser = Depends(require_admin)
) -> dict[str, Any]:
    try:
        return build_view(await _service(request).get(user.uuid, build_id))
    except ReleaseError as exc:
        raise _http(exc) from exc


@router.get("/api/releases/builds/{build_id}/manifests")
async def manifests(
    build_id: uuid.UUID,
    request: Request,
    channel: Annotated[str, Query(max_length=32)] = "stable",
    user: CurrentUser = Depends(require_admin),
) -> dict[str, Any]:
    """What the admin signs: the manifests as the hub will publish them, the time that goes into
    them and the keys the hub accepts. Nothing is signed or published by asking."""
    service = _service(request)
    try:
        build = await service.get(user.uuid, build_id)
        created = service.now_created()
        listed = service.manifests(build, channel, created)
    except ReleaseError as exc:
        raise _http(exc) from exc
    return {
        "channel": channel,
        "created": created,
        "previous_version": service.previous_version(channel),
        "key_ids": request.app.state.updates.key_ids,
        "build": build_view(build),
        "manifests": listed,
    }


@router.post("/api/releases/builds/{build_id}/publish")
async def publish(
    build_id: uuid.UUID,
    body: PublishBody,
    request: Request,
    user: CurrentUser = Depends(require_admin),
) -> dict[str, Any]:
    try:
        build = await _service(request).publish(
            user.uuid,
            build_id,
            channel=body.channel,
            created=body.created,
            signatures=body.signatures,
        )
    except ReleaseError as exc:
        raise _http(exc) from exc
    return build_view(build)


class RolloutBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    dry_run: bool = True


@router.post("/api/releases/builds/{build_id}/rollout")
async def rollout(
    build_id: uuid.UUID,
    body: RolloutBody,
    request: Request,
    user: CurrentUser = Depends(require_admin),
) -> dict[str, Any]:
    """Roll the hub out to a built image: its own click, apart from the build and the signature.
    The default is the dry run; a real run wants a passed dry run of the same image first."""
    try:
        build = await _service(request).rollout(user.uuid, build_id, dry_run=body.dry_run)
    except ReleaseError as exc:
        raise _http(exc) from exc
    return build_view(build)


@router.put("/api/node/builds/{build_id}/artifacts/{name}", status_code=status.HTTP_204_NO_CONTENT)
async def upload_artifact(
    build_id: uuid.UUID,
    name: str,
    request: Request,
    os: Annotated[str, Query(max_length=16)],
    arch: Annotated[str, Query(max_length=16)],
    node: Node = Depends(authenticated_node),
) -> Response:
    declared = request.headers.get("content-length")
    if declared is None or not declared.isdigit():
        raise HTTPException(
            411, detail={"code": "length_required", "message": "send a Content-Length"}
        )
    if int(declared) > MAX_ARTIFACT_BYTES:
        raise HTTPException(
            413, detail={"code": "too_large", "message": "the artifact is too large"}
        )

    async def chunks() -> AsyncIterator[bytes]:
        async for chunk in request.stream():
            yield chunk

    try:
        await _service(request).receive_artifact(node, build_id, name, os, arch, chunks())
    except ReleaseError as exc:
        raise _http(exc) from exc
    return Response(status_code=status.HTTP_204_NO_CONTENT)
