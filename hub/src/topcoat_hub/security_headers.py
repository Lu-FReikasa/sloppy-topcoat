"""Security headers on every HTTP response of the hub.

The GUI, the API and the node endpoints share one origin, and the GUI keeps a token a script can
read. These headers make a script that got onto a page by mistake (a dependency, a rendering bug)
far less useful: it cannot be loaded from elsewhere, cannot send anything anywhere but the hub, and
the page cannot be framed.

- ``Content-Security-Policy``: scripts only from the hub itself, no inline or ``eval``; styles from
  the hub plus inline styles (themes and React's ``style`` attributes need them); pictures from the
  hub, ``data:`` (Mermaid diagrams, report figures) and ``blob:`` (pictures fetched with the token);
  connections (``fetch``, the event stream, the voice socket) only to the hub. A response that sets
  its own policy (report HTML, chat images: ``default-src 'none'; sandbox``) keeps it.
- ``X-Content-Type-Options: nosniff``: a file is never run as something it was not served as.
- ``Referrer-Policy: no-referrer``: a link out of the GUI does not tell the other site where it came
  from (hub address, ids in the path).

The faked hub of the GUI checks (``web/scripts/lib/fake-hub.mjs``) sends the same policy, so those
checks fail when a change needs a source the policy does not allow; a test keeps the two equal.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, MutableMapping
from typing import Any

CONTENT_SECURITY_POLICY = (
    "default-src 'self'; "
    "script-src 'self'; "
    "style-src 'self' 'unsafe-inline'; "
    "img-src 'self' data: blob:; "
    "font-src 'self' data:; "
    "media-src 'self' data: blob:; "
    "connect-src 'self'; "
    "worker-src 'self'; "
    "object-src 'none'; "
    "base-uri 'none'; "
    "form-action 'self'; "
    "frame-ancestors 'none'"
)

SECURITY_HEADERS: tuple[tuple[bytes, bytes], ...] = (
    (b"content-security-policy", CONTENT_SECURITY_POLICY.encode()),
    (b"x-content-type-options", b"nosniff"),
    (b"referrer-policy", b"no-referrer"),
)

Scope = MutableMapping[str, Any]
Message = MutableMapping[str, Any]
Receive = Callable[[], Awaitable[Message]]
Send = Callable[[Message], Awaitable[None]]
ASGIApp = Callable[[Scope, Receive, Send], Awaitable[None]]


class SecurityHeaders:
    """ASGI middleware: adds :data:`SECURITY_HEADERS` to every HTTP response that does not set them.

    A plain ASGI wrapper rather than ``BaseHTTPMiddleware``: it only touches the start of a
    response, so streamed answers (the event stream, chat runs) pass through unchanged.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        async def send_with_headers(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = list(message.get("headers", []))
                present = {name.lower() for name, _ in headers}
                headers.extend((n, v) for n, v in SECURITY_HEADERS if n not in present)
                message["headers"] = headers
            await send(message)

        await self.app(scope, receive, send_with_headers)
