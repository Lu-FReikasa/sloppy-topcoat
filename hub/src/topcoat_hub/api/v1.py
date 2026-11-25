"""OpenAI-compatible gateway endpoints (E1-05). Clients use ``Authorization: Bearer <API
key>`` (created in the GUI) and the hub's ``/v1`` base URL; ``model@node`` pins a node."""

import json
import logging
from typing import Any

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse
from starlette.types import Receive, Scope, Send

from topcoat_hub.auth import CurrentUser, gateway_user
from topcoat_hub.gateway import Gateway, GatewayError, Upstream
from topcoat_hub.job_log import set_job_context

log = logging.getLogger(__name__)

router = APIRouter(prefix="/v1")

_TYPES = {400: "invalid_request_error", 401: "authentication_error", 404: "not_found_error"}


def error_response(exc: GatewayError) -> JSONResponse:
    headers = {"Retry-After": str(exc.retry_after)} if exc.retry_after else None
    return JSONResponse(
        {
            "error": {
                "message": exc.message,
                "type": _TYPES.get(exc.status, "server_error"),
                "code": exc.code,
            }
        },
        status_code=exc.status,
        headers=headers,
    )


class UpstreamResponse(StreamingResponse):
    """Streams a gateway answer and always ends the upstream stream afterwards, also when
    the client left before the first byte was read (the body generator never ran then)."""

    def __init__(self, upstream: Upstream, **kwargs: Any) -> None:
        super().__init__(upstream.body, **kwargs)
        self.upstream = upstream

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        try:
            await super().__call__(scope, receive, send)
        finally:
            self.upstream.close()


def _gateway(request: Request) -> Gateway:
    return request.app.state.gateway


async def _payload(request: Request) -> dict[str, Any]:
    limit = request.app.state.settings.gateway_max_request_bytes
    declared = request.headers.get("content-length")
    if declared and declared.isdigit() and int(declared) > limit:
        raise GatewayError(413, "request_too_large", f"request body larger than {limit} bytes")
    raw = await request.body()
    if len(raw) > limit:
        raise GatewayError(413, "request_too_large", f"request body larger than {limit} bytes")
    try:
        payload = json.loads(raw)
    except ValueError as exc:
        raise GatewayError(400, "invalid_json", "body is not valid JSON") from exc
    if not isinstance(payload, dict):
        raise GatewayError(400, "invalid_request", "body must be a JSON object")
    model = payload.get("model")
    if not isinstance(model, str) or not model.strip():
        raise GatewayError(400, "invalid_request", "field 'model' (string) is required")
    return payload


@router.get("/models")
async def list_models(request: Request, _user: CurrentUser = Depends(gateway_user)) -> Any:
    return {"object": "list", "data": await _gateway(request).list_models()}


async def _proxy(request: Request, path: str, user: CurrentUser) -> Response:
    # a request through the gateway is the work of the row ``api`` of the table of applications
    set_job_context(application="api")
    try:
        payload = await _payload(request)
        upstream = await _gateway(request).forward(
            payload["model"].strip(),
            "POST",
            path,
            payload,
            accept=request.headers.get("accept", ""),
        )
    except GatewayError as exc:
        log.info("gateway: %s for user %s -> %s %s", path, user.id, exc.status, exc.code)
        return error_response(exc)
    log.info("gateway: %s for user %s served by %s", path, user.id, upstream.node)
    return UpstreamResponse(
        upstream,
        status_code=upstream.status,
        media_type=upstream.content_type,
        headers={"X-Topcoat-Node": upstream.node, "Cache-Control": "no-cache"},
    )


@router.post("/chat/completions")
async def chat_completions(request: Request, user: CurrentUser = Depends(gateway_user)) -> Any:
    return await _proxy(request, "/v1/chat/completions", user)


@router.post("/completions")
async def completions(request: Request, user: CurrentUser = Depends(gateway_user)) -> Any:
    return await _proxy(request, "/v1/completions", user)


@router.post("/embeddings")
async def embeddings(request: Request, user: CurrentUser = Depends(gateway_user)) -> Any:
    return await _proxy(request, "/v1/embeddings", user)
