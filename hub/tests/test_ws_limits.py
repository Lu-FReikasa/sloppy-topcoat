"""The hub's WebSocket transport limits (serve.WS_LIMITS): what uvicorn buffers per message.

The limits sit below every route, so the tests run a real uvicorn server with the hub's settings
and a small echo app: the node socket and the voice socket see the same transport.
"""

import asyncio
import base64
import json
import socket
from typing import Any

import pytest
import uvicorn
from websockets.asyncio.client import connect
from websockets.exceptions import ConnectionClosed

from topcoat_hub import serve, tls

MIB = 1024 * 1024


async def echo_sizes(scope: dict[str, Any], receive: Any, send: Any) -> None:
    """Answer every message with its size in bytes."""
    assert scope["type"] == "websocket"
    await receive()  # websocket.connect
    await send({"type": "websocket.accept"})
    while True:
        message = await receive()
        if message["type"] == "websocket.disconnect":
            return
        data = message.get("bytes")
        size = len(data) if data is not None else len(message["text"].encode())
        await send({"type": "websocket.send", "text": str(size)})


@pytest.fixture
async def hub_url():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    config = uvicorn.Config(
        echo_sizes,
        host="127.0.0.1",
        port=port,
        lifespan="off",
        log_config=None,
        **serve.WS_LIMITS,
    )
    server = uvicorn.Server(config)
    task = asyncio.create_task(server.serve())
    for _ in range(500):
        if server.started or task.done():
            break
        await asyncio.sleep(0.01)
    assert server.started
    try:
        yield f"ws://127.0.0.1:{port}/"
    finally:
        server.should_exit = True
        await task


async def roundtrip(url: str, message: str | bytes) -> int:
    async with connect(url, max_size=None) as ws:
        await ws.send(message)
        return int(await asyncio.wait_for(ws.recv(), 10))


def test_the_limits_are_eight_mib_and_a_short_queue() -> None:
    assert serve.WS_LIMITS == {"ws_max_size": 8 * MIB, "ws_max_queue": 8}


async def test_a_message_of_exactly_the_limit_goes_through(hub_url: str) -> None:
    assert await roundtrip(hub_url, b"\0" * (8 * MIB)) == 8 * MIB
    assert await roundtrip(hub_url, "a" * (8 * MIB - 1)) == 8 * MIB - 1


async def test_one_byte_more_closes_the_socket_with_1009(hub_url: str) -> None:
    async with connect(hub_url, max_size=None) as ws:
        await ws.send(b"\0" * (8 * MIB + 1))
        with pytest.raises(ConnectionClosed) as closed:
            await asyncio.wait_for(ws.recv(), 10)
    assert closed.value.rcvd is not None and closed.value.rcvd.code == 1009


async def test_the_largest_node_answer_goes_through(hub_url: str) -> None:
    # voice.speak's answer: a WAV of up to 2_800_000 bytes (node/internal/voice/voice.go
    # MaxSpeechBytes), base64 in one action_result envelope (docs/PROTOCOL.md)
    wav = b"RIFF" + b"\x7f" * (2_800_000 - 4)
    output = {"audio_base64": base64.b64encode(wav).decode(), "format": "wav"}
    envelope = {
        "v": 1,
        "type": "action_result",
        "id": "0" * 36,
        "re": "1" * 36,
        "ts": "2026-11-27T12:00:00Z",
        "body": {
            "ok": True,
            "result": {
                "capability": "voice.speak",
                "output": output,
                "bytes": 3_733_360,
                "duration_ms": 4000,
                "at": "2026-11-27T12:00:00Z",
            },
        },
    }
    frame = json.dumps(envelope)
    assert 3_700_000 < len(frame) < 4 * MIB  # below the node's own read limit as well
    assert await roundtrip(hub_url, frame) == len(frame)


async def test_the_voice_clients_largest_frames_go_through(hub_url: str) -> None:
    # a whole recording in one binary frame (api/voice.py caps a recording at MAX_PCM_BYTES)
    # and the largest text frame the voice socket reads (MAX_TEXT_FRAME)
    from topcoat_hub.api.voice import MAX_TEXT_FRAME
    from topcoat_hub.voice import MAX_PCM_BYTES

    assert await roundtrip(hub_url, b"\1\0" * (MAX_PCM_BYTES // 2)) == MAX_PCM_BYTES
    text = json.dumps({"type": "speak", "text": "x" * (MAX_TEXT_FRAME - 64)})
    assert await roundtrip(hub_url, text) == len(text)


def test_the_hub_serves_with_these_limits(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict[str, Any] = {}
    monkeypatch.setattr(serve, "check_known", lambda *_: None)
    monkeypatch.setattr(tls, "prepare", lambda _settings: tls.TLSMaterial(mode="off"))
    monkeypatch.setattr(serve.uvicorn, "run", lambda *_a, **kw: seen.update(kw))
    serve.main(["--no-migrate"])
    assert seen["ws_max_size"] == 8 * MIB
    assert seen["ws_max_queue"] == 8
