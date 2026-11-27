"""Security headers on the hub's responses (CSP, nosniff, Referrer-Policy)."""

from __future__ import annotations

import re
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from fastapi import FastAPI, WebSocket
from fastapi.responses import Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.testclient import TestClient

from topcoat_hub.config import Settings
from topcoat_hub.main import create_app
from topcoat_hub.security_headers import CONTENT_SECURITY_POLICY, SecurityHeaders

from .conftest import DEV_TOKEN

REPO = Path(__file__).resolve().parents[2]


def _small_app(web: Path) -> FastAPI:
    app = FastAPI()
    app.add_middleware(SecurityHeaders)

    @app.get("/api/own")
    def own() -> Response:
        return Response(
            "x",
            headers={"Content-Security-Policy": "default-src 'none'; sandbox"},
        )

    @app.get("/api/stream")
    def stream() -> StreamingResponse:
        async def parts() -> AsyncIterator[bytes]:
            for i in range(3):
                yield f"data: {i}\n\n".encode()

        return StreamingResponse(parts(), media_type="text/event-stream")

    @app.websocket("/api/ws")
    async def ws(sock: WebSocket) -> None:
        await sock.accept()
        await sock.send_text("hi")
        await sock.close()

    app.mount("/", StaticFiles(directory=web, html=True), name="web")
    return app


@pytest.fixture
def web(tmp_path: Path) -> Path:
    (tmp_path / "index.html").write_text("<!doctype html><div id=root></div>")
    (tmp_path / "app.js").write_text("console.log(1)")
    return tmp_path


def _assert_headers(headers: object, csp: str = CONTENT_SECURITY_POLICY) -> None:
    assert headers["content-security-policy"] == csp  # type: ignore[index]
    assert headers["x-content-type-options"] == "nosniff"  # type: ignore[index]
    assert headers["referrer-policy"] == "no-referrer"  # type: ignore[index]


def test_gui_files_api_and_errors_carry_the_headers(web: Path) -> None:
    with TestClient(_small_app(web)) as c:
        for path in ("/", "/app.js", "/missing.js"):
            _assert_headers(c.get(path).headers)


def test_a_stricter_policy_of_a_response_is_kept(web: Path) -> None:
    with TestClient(_small_app(web)) as c:
        r = c.get("/api/own")
    _assert_headers(r.headers, "default-src 'none'; sandbox")
    assert len(r.headers.get_list("content-security-policy")) == 1


def test_a_stream_passes_through_whole(web: Path) -> None:
    with TestClient(_small_app(web)) as c:
        r = c.get("/api/stream")
    assert r.text == "data: 0\n\ndata: 1\n\ndata: 2\n\n"
    _assert_headers(r.headers)


def test_a_websocket_is_left_alone(web: Path) -> None:
    with TestClient(_small_app(web)) as c, c.websocket_connect("/api/ws") as sock:
        assert sock.receive_text() == "hi"


def test_the_policy_allows_no_script_from_elsewhere_or_inline() -> None:
    parts = dict(p.strip().split(" ", 1) for p in CONTENT_SECURITY_POLICY.split(";"))
    assert parts["script-src"] == "'self'"
    assert parts["connect-src"] == "'self'"
    assert parts["object-src"] == "'none'"
    assert parts["base-uri"] == "'none'"
    assert parts["frame-ancestors"] == "'none'"
    assert "unsafe-eval" not in CONTENT_SECURITY_POLICY
    assert "http" not in CONTENT_SECURITY_POLICY  # no outside host anywhere


def test_the_gui_checks_send_the_same_policy() -> None:
    """The faked hub of the GUI checks sends the hub's policy, so those checks see violations."""
    fake = (REPO / "web" / "scripts" / "lib" / "fake-hub.mjs").read_text(encoding="utf-8")
    found = re.search(r'export const CSP =\s*"([^"]+)"', fake)
    assert found, "fake-hub.mjs exports no CSP"
    assert found.group(1) == CONTENT_SECURITY_POLICY


def test_the_hub_sends_them_on_its_gui_and_api(settings: Settings, web: Path) -> None:
    app = create_app(settings.model_copy(update={"web_dist": web}))
    with TestClient(app) as c:
        _assert_headers(c.get("/").headers)
        _assert_headers(c.get("/app.js").headers)
        me = c.get("/api/me", headers={"Authorization": f"Bearer {DEV_TOKEN}"})
        assert me.status_code == 200
        _assert_headers(me.headers)
        _assert_headers(c.get("/api/me").headers)  # a refusal too
