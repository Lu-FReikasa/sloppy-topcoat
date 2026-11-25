"""Update channel endpoints (E2-06, ARCHITECTURE D50).

Admin (``topcoat-sign upload``, GUI): list, publish, delete. Nodes: fetch the signed manifest and
the program of the newest release of their channel. Nodes authenticate with the same signed
nonce as on the WebSocket; every answer is checked again by the node itself.
"""

import json
from collections.abc import AsyncIterator
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Path, Query, Request, status
from fastapi.responses import FileResponse, Response

from topcoat_hub.api.node import authenticated_node
from topcoat_hub.auth import CurrentUser, require_admin
from topcoat_hub.models import Node
from topcoat_hub.updates import (
    MAX_MANIFEST_BYTES,
    UpdateError,
    UpdateStore,
    node_channel,
)

router = APIRouter()

Channel = Annotated[str, Path(pattern=r"^[a-z][a-z0-9-]{0,31}$")]
Version = Annotated[str, Path(max_length=64)]
Platform = Annotated[str, Path(pattern=r"^[a-z0-9_]{1,16}-[a-z0-9_]{1,16}$")]


def _store(request: Request) -> UpdateStore:
    return request.app.state.updates


def _http(exc: UpdateError) -> HTTPException:
    return HTTPException(exc.status, detail={"code": exc.code, "message": exc.message})


@router.get("/api/updates")
async def list_updates(
    request: Request, _user: CurrentUser = Depends(require_admin)
) -> dict[str, Any]:
    store = _store(request)
    return {
        "enabled": store.enabled,
        "signature_check": bool(store.keys),
        "key_ids": store.key_ids,
        "releases": [r.public() for r in store.releases()],
    }


@router.put(
    "/api/updates/{channel}/{version}/{plat}/manifest", status_code=status.HTTP_204_NO_CONTENT
)
async def put_manifest(
    channel: Channel,
    version: Version,
    plat: Platform,
    request: Request,
    _user: CurrentUser = Depends(require_admin),
) -> Response:
    declared = request.headers.get("content-length")
    if declared is None or not declared.isdigit() or int(declared) > 4 * MAX_MANIFEST_BYTES:
        raise HTTPException(
            413, detail={"code": "too_large", "message": "the manifest upload is too large"}
        )
    try:
        body = json.loads(await request.body())
        manifest, signature = body["manifest"], body["signature"]
        if not isinstance(manifest, str) or not isinstance(signature, str):
            raise TypeError
    except (ValueError, KeyError, TypeError) as exc:
        raise HTTPException(
            400,
            detail={
                "code": "bad_request",
                "message": 'the body must be {"manifest": "...", "signature": "..."}',
            },
        ) from exc
    try:
        await _store(request).put_manifest(channel, version, plat, manifest, signature)
    except UpdateError as exc:
        raise _http(exc) from exc
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.put(
    "/api/updates/{channel}/{version}/{plat}/binary", status_code=status.HTTP_204_NO_CONTENT
)
async def put_binary(
    channel: Channel,
    version: Version,
    plat: Platform,
    request: Request,
    _user: CurrentUser = Depends(require_admin),
) -> Response:
    store = _store(request)
    declared = request.headers.get("content-length")
    if declared is None or not declared.isdigit():
        raise HTTPException(
            411, detail={"code": "length_required", "message": "send a Content-Length"}
        )
    if int(declared) > store.max_bytes:
        raise HTTPException(
            413, detail={"code": "too_large", "message": "the program is too large"}
        )

    async def chunks() -> AsyncIterator[bytes]:
        async for chunk in request.stream():
            yield chunk

    try:
        await store.put_binary(channel, version, plat, chunks())
    except UpdateError as exc:
        raise _http(exc) from exc
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.delete("/api/updates/{channel}/{version}/{plat}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_update(
    channel: Channel,
    version: Version,
    plat: Platform,
    request: Request,
    _user: CurrentUser = Depends(require_admin),
) -> Response:
    try:
        gone = await _store(request).delete(channel, version, plat)
    except UpdateError as exc:
        raise _http(exc) from exc
    if not gone:
        raise HTTPException(404, detail={"code": "not_found", "message": "no such release"})
    return Response(status_code=status.HTTP_204_NO_CONTENT)


def _release_for(request: Request, node: Node, version: str | None):
    store = _store(request)
    if not store.enabled:
        raise HTTPException(
            404, detail={"code": "no_release", "message": "this hub publishes no updates"}
        )
    channel = node_channel(node.config)
    try:
        rel = (
            store.get(channel, node.os, node.arch, version)
            if version
            else store.latest(channel, node.os, node.arch)
        )
    except UpdateError as exc:
        raise _http(exc) from exc
    if rel is None:
        raise HTTPException(
            404,
            detail={
                "code": "no_release",
                "message": f"no release on channel {channel} for {node.os}-{node.arch}",
            },
        )
    return rel


@router.get("/api/node/update/manifest")
async def node_manifest(
    request: Request,
    version: Annotated[str | None, Query(max_length=64)] = None,
    node: Node = Depends(authenticated_node),
) -> dict[str, str]:
    rel = _release_for(request, node, version)
    return {"manifest": rel.manifest_text, "signature": rel.signature}


@router.get("/api/node/update/binary")
async def node_binary(
    request: Request,
    version: Annotated[str | None, Query(max_length=64)] = None,
    node: Node = Depends(authenticated_node),
) -> FileResponse:
    rel = _release_for(request, node, version)
    return FileResponse(rel.binary, media_type="application/octet-stream", filename="topcoat-node")
