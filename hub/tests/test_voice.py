"""The voice path (D127, issue #55): the WebSocket from a client to the node's voice services."""

import base64
import contextlib
import json
import struct
import threading
import time
import uuid
from collections.abc import Iterator
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from starlette.websockets import WebSocketDisconnect

from tests.conftest import AUTH, DEV_TOKEN, TEST_DB_URL
from tests.fakenode import FakeNode, eventually, pair, tools_inventory
from tests.users import make_user
from topcoat_hub.voice import MAX_PCM_BYTES, pieces, speakable, wav

WAV_OUT = b"RIFF-fake-wav"
VOICE_CAPS = [
    {
        "name": "voice.transcribe",
        "description": "speech to text (test)",
        "read_only": True,
        "input_schema": {
            "type": "object",
            "additionalProperties": False,
            "required": ["audio_base64"],
            "properties": {
                "audio_base64": {"type": "string", "minLength": 1, "maxLength": 3400000},
                "model": {"type": "string"},
                "language": {"type": "string", "pattern": "^(auto|[a-z]{2,3})$"},
            },
        },
    },
    {
        "name": "voice.speak",
        "description": "text to speech (test)",
        "read_only": True,
        "input_schema": {
            "type": "object",
            "additionalProperties": False,
            "required": ["text"],
            "properties": {
                "text": {"type": "string", "minLength": 1, "maxLength": 600},
                "voice": {"type": "string"},
            },
        },
    },
]


def voice_node(client: TestClient, connect: Any, transcript: str = "wie spät ist es") -> FakeNode:
    node_id, key = pair(client, "node-voice", role="tools")
    node = connect(node_id, key)
    inv = tools_inventory()
    inv["capabilities"] += VOICE_CAPS
    node.inventory = inv
    node.capability_answers["voice.transcribe"] = {"text": f" {transcript} ", "language": "de"}

    def speak(args: dict[str, Any]) -> dict[str, Any]:
        if args.get("voice") == "missing-voice":
            return {"error": {"code": "capability_failed", "message": "no such voice"}}
        return {"audio_base64": base64.b64encode(WAV_OUT).decode(), "format": "wav"}

    node.capability_answers["voice.speak"] = speak
    node.handshake()
    node.serve()
    cfg = client.get(f"/api/nodes/{node_id}", headers=AUTH).json()["config"]
    cfg["capabilities"] = [*cfg["capabilities"], "voice.transcribe", "voice.speak"]
    assert client.put(f"/api/nodes/{node_id}/config", headers=AUTH, json=cfg).status_code == 200

    def stored() -> None:
        usable = client.get(f"/api/nodes/{node_id}/capabilities", headers=AUTH).json()["usable"]
        assert "voice.speak" in usable

    eventually(stored)
    node.node_id = node_id  # type: ignore[attr-defined]
    return node


@contextlib.contextmanager
def opened(client: TestClient, token: str = DEV_TOKEN) -> Iterator[Any]:
    with client.websocket_connect("/api/voice/ws") as ws:
        ws.send_text(json.dumps({"type": "auth", "token": token}))
        yield ws


def audit_rows() -> list[dict[str, Any]]:
    engine = create_engine(TEST_DB_URL)
    with engine.connect() as conn:
        rows = [
            dict(r)
            for r in conn.execute(
                text(
                    "SELECT capability, ok, input, output_excerpt FROM tool_calls "
                    "WHERE capability LIKE 'voice.%' ORDER BY id"
                )
            ).mappings()
        ]
    engine.dispose()
    return rows


# --- the pure parts ---------------------------------------------------------------------------


def test_the_wav_header_says_mono_16_bit_at_the_rate() -> None:
    out = wav(b"\x01\x00\x02\x00\x03", 16000)  # half a sample at the end is dropped
    assert out[:4] == b"RIFF" and out[8:16] == b"WAVEfmt "
    channels, rate = struct.unpack("<HI", out[22:28])
    assert (channels, rate) == (1, 16000)
    assert struct.unpack("<I", out[40:44])[0] == 4 and len(out) == 48


def test_an_answer_is_spoken_sentence_by_sentence_without_its_markdown() -> None:
    answer = (
        "**Ja**, der Container läuft. Siehe [die Liste](https://x.example/a).\n\n"
        "```\ndocker ps\n```\n- `web` ist oben!"
    )
    assert pieces(answer) == ["Ja, der Container läuft.", "Siehe die Liste.", "web ist oben!"]
    assert speakable("## Titel") == "Titel"


def test_a_long_sentence_is_cut_below_the_node_limit() -> None:
    long = ", ".join(["ein Wort mehr"] * 80) + "."
    out = pieces(long)
    assert len(out) > 1 and all(len(p) <= 600 for p in out)
    assert " ".join(out).replace(" ", "") == long.replace(" ", "")


# --- the socket -------------------------------------------------------------------------------


def test_without_a_token_the_socket_closes(client: TestClient) -> None:
    with client.websocket_connect("/api/voice/ws") as ws:
        ws.send_text(json.dumps({"type": "auth", "token": "wrong"}))
        with pytest.raises(WebSocketDisconnect) as closed:
            ws.receive_text()
    assert closed.value.code == 4401


def test_the_status_says_whether_a_node_can_listen_and_speak(
    client: TestClient, connect: Any
) -> None:
    assert client.get("/api/voice", headers=AUTH).json() == {"transcribe": False, "speak": False}
    voice_node(client, connect)
    assert client.get("/api/voice", headers=AUTH).json() == {"transcribe": True, "speak": True}
    assert client.get("/api/voice").status_code == 401


def test_without_a_voice_node_ready_says_so_and_listening_fails_softly(
    client: TestClient,
) -> None:
    with opened(client) as ws:
        assert ws.receive_json() == {"type": "ready", "transcribe": False, "speak": False}
        ws.send_text(json.dumps({"type": "listen", "id": "u1", "rate": 16000}))
        ws.send_bytes(b"\x00\x01" * 800)
        ws.send_text(json.dumps({"type": "end", "id": "u1"}))
        msg = ws.receive_json()
        assert msg["type"] == "error" and msg["id"] == "u1" and msg["code"] == "no_voice_node"


def test_a_recording_becomes_its_transcript_and_nothing_of_it_is_kept(
    client: TestClient, connect: Any
) -> None:
    node = voice_node(client, connect)
    with opened(client) as ws:
        assert ws.receive_json() == {"type": "ready", "transcribe": True, "speak": True}
        ws.send_text(json.dumps({"type": "listen", "id": "u1", "rate": 16000, "language": "de"}))
        for _ in range(3):
            ws.send_bytes(b"\x10\x00" * 1600)
        ws.send_text(json.dumps({"type": "end", "id": "u1"}))
        assert ws.receive_json() == {
            "type": "transcript",
            "id": "u1",
            "text": "wie spät ist es",
            "language": "de",
        }
    call = node.capability_calls[-1]
    assert call["capability"] == "voice.transcribe" and call["input"]["language"] == "de"
    sent = base64.b64decode(call["input"]["audio_base64"])
    assert sent[:4] == b"RIFF" and len(sent) == 44 + 3 * 3200
    # audited without the audio and without what was said (D127)
    [row] = audit_rows()
    assert row["capability"] == "voice.transcribe" and row["ok"] is True
    assert "audio_base64" not in json.dumps(row["input"]) and row["output_excerpt"] is None
    # the hub wrote no message: the client posts the transcript through the chat
    engine = create_engine(TEST_DB_URL)
    with engine.connect() as conn:
        assert conn.execute(text("SELECT count(*) FROM messages")).scalar() == 0
    engine.dispose()


def test_an_incognito_recording_and_answer_leave_no_row(client: TestClient, connect: Any) -> None:
    voice_node(client, connect)
    with opened(client) as ws:
        ws.receive_json()
        ws.send_text(json.dumps({"type": "listen", "id": "u1", "incognito": True}))
        ws.send_bytes(b"\x10\x00" * 100)
        ws.send_text(json.dumps({"type": "end", "id": "u1"}))
        assert ws.receive_json()["type"] == "transcript"
        ws.send_text(json.dumps({"type": "speak", "id": "s1", "text": "Hallo.", "incognito": True}))
        assert ws.receive_json()["type"] == "audio"
        ws.receive_bytes()
        assert ws.receive_json()["type"] == "spoken"
    assert audit_rows() == []


def test_a_recording_over_the_limit_is_refused(client: TestClient, connect: Any) -> None:
    node = voice_node(client, connect)
    with opened(client) as ws:
        ws.receive_json()
        ws.send_text(json.dumps({"type": "listen", "id": "u1"}))
        chunk = b"\x00" * 500_000
        for _ in range(MAX_PCM_BYTES // len(chunk) + 1):
            ws.send_bytes(chunk)
        ws.send_text(json.dumps({"type": "end", "id": "u1"}))
        assert ws.receive_json()["code"] == "too_long"
    assert not any(c["capability"] == "voice.transcribe" for c in node.capability_calls)


def test_only_a_few_recordings_are_read_at_once(client: TestClient, connect: Any) -> None:
    node = voice_node(client, connect)
    gate = threading.Event()
    answer: dict[str, Any] = node.capability_answers["voice.transcribe"]

    def slow(args: dict[str, Any]) -> dict[str, Any]:
        gate.wait(5)
        return answer

    node.capability_answers["voice.transcribe"] = slow
    with opened(client) as ws:
        ws.receive_json()
        for n in range(3):  # two are read, the third waits for none of them
            ws.send_text(json.dumps({"type": "listen", "id": f"u{n}"}))
            ws.send_bytes(b"\x10\x00" * 100)
            ws.send_text(json.dumps({"type": "end", "id": f"u{n}"}))
        refused = ws.receive_json()
        assert (refused["id"], refused["code"]) == ("u2", "busy")
        gate.set()
        assert {ws.receive_json()["id"] for _ in range(2)} == {"u0", "u1"}


def test_an_answer_comes_back_as_audio_pieces_in_order(client: TestClient, connect: Any) -> None:
    node = voice_node(client, connect)
    with opened(client) as ws:
        ws.receive_json()
        ws.send_text(json.dumps({"type": "speak", "id": "s1", "text": "Erstens. Zweitens!"}))
        ws.send_text(json.dumps({"type": "speak", "id": "s2", "text": "Drittens?"}))
        got = []
        while len(got) < 5:
            msg = ws.receive_json()
            if msg["type"] == "audio":
                assert ws.receive_bytes() == WAV_OUT and msg["bytes"] == len(WAV_OUT)
                got.append((msg["id"], msg["seq"], msg["text"]))
            else:
                got.append((msg["type"], msg["id"]))
        assert got == [
            ("s1", 0, "Erstens."),
            ("s1", 1, "Zweitens!"),
            ("spoken", "s1"),
            ("s2", 0, "Drittens?"),
            ("spoken", "s2"),
        ]
    assert [c["input"] for c in node.capability_calls if c["capability"] == "voice.speak"] == [
        {"text": "Erstens."},
        {"text": "Zweitens!"},
        {"text": "Drittens?"},
    ]


def test_cancel_stops_the_speech_that_waits(client: TestClient, connect: Any) -> None:
    node = voice_node(client, connect)
    gate = threading.Event()
    fast = node.capability_answers["voice.speak"]

    def slow(args: dict[str, Any]) -> dict[str, Any]:
        gate.wait(5)
        return fast(args)

    node.capability_answers["voice.speak"] = slow
    with opened(client) as ws:
        ws.receive_json()
        ws.send_text(json.dumps({"type": "speak", "id": "s1", "text": "Eins. Zwei. Drei."}))
        ws.send_text(json.dumps({"type": "speak", "id": "s2", "text": "Vier."}))
        time.sleep(0.3)  # the first piece is with the node now
        ws.send_text(json.dumps({"type": "cancel"}))  # the person speaks again (barge-in)
        cancelled = {ws.receive_json()["id"], ws.receive_json()["id"]}
        assert cancelled == {"s1", "s2"}
        gate.set()
        ws.send_text(json.dumps({"type": "ping"}))
        assert ws.receive_json() == {"type": "pong"}  # nothing of s1 or s2 came after the cancel
    spoken = [c for c in node.capability_calls if c["capability"] == "voice.speak"]
    assert len(spoken) == 1


def test_the_persona_of_the_chat_speaks_with_its_voice(client: TestClient, connect: Any) -> None:
    node = voice_node(client, connect)
    persona = client.post(
        "/api/personas",
        headers=AUTH,
        json={"name": "Sprecher", "components": {"free_text": "Du sprichst kurz."}},
    ).json()
    r = client.put(f"/api/personas/{persona['id']}", headers=AUTH, json={"voice": "de_DE-thorsten"})
    assert r.status_code == 200, r.text
    assert r.json()["defaults"]["voice"] == "de_DE-thorsten"
    assert r.json()["version"] == persona["version"]  # the text did not change
    conv = client.post(
        "/api/conversations", headers=AUTH, json={"persona_ref": persona["id"]}
    ).json()
    with opened(client) as ws:
        ws.receive_json()
        ws.send_text(
            json.dumps(
                {"type": "speak", "id": "s1", "text": "Hallo.", "conversation_id": conv["id"]}
            )
        )
        assert ws.receive_json()["type"] == "audio"
        ws.receive_bytes()
        ws.receive_json()
    assert node.capability_calls[-1]["input"] == {"text": "Hallo.", "voice": "de_DE-thorsten"}
    assert (
        client.put(
            f"/api/personas/{persona['id']}", headers=AUTH, json={"voice": "../evil"}
        ).status_code
        == 422
    )


def test_a_voice_the_node_lacks_falls_back_to_its_own(client: TestClient, connect: Any) -> None:
    node = voice_node(client, connect)
    persona = client.post(
        "/api/personas",
        headers=AUTH,
        json={"name": "Sprecher", "components": {"free_text": "Du sprichst kurz."}},
    ).json()
    client.put(f"/api/personas/{persona['id']}", headers=AUTH, json={"voice": "missing-voice"})
    client.put("/api/personas/default", headers=AUTH, json={"persona_id": persona["id"]})
    with opened(client) as ws:
        ws.receive_json()
        ws.send_text(json.dumps({"type": "speak", "id": "s1", "text": "Hallo."}))
        assert ws.receive_json()["type"] == "audio"
    assert [c["input"] for c in node.capability_calls][-2:] == [
        {"text": "Hallo.", "voice": "missing-voice"},
        {"text": "Hallo."},
    ]


def test_another_users_conversation_lends_no_voice(
    client: TestClient, connect: Any, settings: Any
) -> None:
    node = voice_node(client, connect)
    persona = client.post(
        "/api/personas",
        headers=AUTH,
        json={"name": "Sprecher", "components": {"free_text": "Du sprichst kurz."}},
    ).json()
    client.put(f"/api/personas/{persona['id']}", headers=AUTH, json={"voice": "admin-voice"})
    conv = client.post(
        "/api/conversations", headers=AUTH, json={"persona_ref": persona["id"]}
    ).json()
    other_id, other = make_user(settings.database_url, "other")
    client.put(
        f"/api/nodes/{node.node_id}/grants/{other_id}",  # type: ignore[attr-defined]
        headers=AUTH,
        json={"capabilities": ["voice.speak"]},
    )
    with opened(client, other["Authorization"].split(" ", 1)[1]) as ws:
        assert ws.receive_json() == {"type": "ready", "transcribe": False, "speak": True}
        ws.send_text(
            json.dumps(
                {"type": "speak", "id": "s1", "text": "Hallo.", "conversation_id": conv["id"]}
            )
        )
        assert ws.receive_json()["type"] == "audio"
    assert node.capability_calls[-1]["input"] == {"text": "Hallo."}


def test_a_spoken_yes_reaches_no_approval(client: TestClient, connect: Any) -> None:
    """D15/D83: the socket has no path to a confirmation. Whatever is said, only a transcript
    comes back; the approval endpoints are never called and nothing changes on the node."""
    node = voice_node(client, connect, transcript="ja, mach das")
    with opened(client) as ws:
        ws.receive_json()
        for kind in ("confirm", "approve", "execute"):
            ws.send_text(json.dumps({"type": kind, "id": "x"}))
            assert ws.receive_json()["code"] == "invalid_input"
        ws.send_text(json.dumps({"type": "listen", "id": "u1"}))
        ws.send_bytes(b"\x10\x00" * 100)
        ws.send_text(json.dumps({"type": "end", "id": "u1"}))
        assert ws.receive_json()["text"] == "ja, mach das"
    assert node.execute_calls == []
    assert {c["capability"] for c in node.capability_calls} == {"voice.transcribe"}


def test_bad_messages_get_an_error_not_a_closed_socket(client: TestClient) -> None:
    with opened(client) as ws:
        ws.receive_json()
        for bad in ("not json", "[1]", json.dumps({"type": "speak", "id": "s", "text": ""})):
            ws.send_text(bad)
            assert ws.receive_json()["type"] == "error"
        ws.send_text(json.dumps({"type": "listen", "id": "u", "rate": 1}))
        assert ws.receive_json()["code"] == "invalid_input"
        ws.send_text(json.dumps({"type": "end", "id": str(uuid.uuid4())}))
        assert ws.receive_json()["code"] == "not_listening"
