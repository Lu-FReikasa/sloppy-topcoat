"""The voice WebSocket (D127, issue #55; docs/HUB-API.md "Voice"): audio chunks in, transcript out;
answer text in, audio chunks out; cancel at any time (barge-in, #60 builds on it).

A browser cannot put a header on a WebSocket, so the token is the first frame. Without an ambient
credential (no cookie) another site's page cannot open this socket for the person."""

import asyncio
import contextlib
import json
import logging
import uuid
from dataclasses import dataclass, field
from typing import Any

from fastapi import APIRouter, Depends, Request, WebSocket, WebSocketDisconnect

from topcoat_hub.auth import CurrentUser, api_user, user_of_token
from topcoat_hub.tools import Caller
from topcoat_hub.voice import (
    LANGUAGE,
    MAX_PCM_BYTES,
    MAX_RATE,
    MAX_SPEAK_CHARS,
    MIN_RATE,
    VoiceError,
    VoiceService,
    pieces,
)

log = logging.getLogger(__name__)

router = APIRouter()

CLOSE_UNAUTHORIZED = 4401
AUTH_TIMEOUT = 10.0
MAX_TEXT_FRAME = 64 * 1024
MAX_QUEUED = 64  # speak requests waiting (sentences of the answers being written)
# recordings being turned into text at once: push to talk has one, a quick second press may add
# one more; each holds up to 2.5 MB and a node call, so a client cannot start an unbounded number
MAX_TRANSCRIBING = 2
ID_MAX = 64


@dataclass
class _Utterance:
    id: str
    rate: int
    language: str
    incognito: bool
    pcm: bytearray = field(default_factory=bytearray)
    too_long: bool = False


@dataclass
class _Speech:
    id: str
    text: str
    conversation_id: uuid.UUID | None
    incognito: bool


class VoiceSession:
    """One client's socket: at most one recording at a time, speech spoken in the order asked."""

    def __init__(
        self, ws: WebSocket, service: VoiceService, caller: Caller, user: CurrentUser
    ) -> None:
        self.ws, self.service, self.caller, self.user = ws, service, caller, user
        self.send_lock = asyncio.Lock()
        self.utterance: _Utterance | None = None
        self.transcribing: dict[str, asyncio.Task[None]] = {}
        self.queue: list[_Speech] = []
        self.wake = asyncio.Event()
        self.current: _Speech | None = None
        self.current_task: asyncio.Task[None] | None = None
        self.voices: dict[uuid.UUID | None, str | None] = {}

    async def send(self, message: dict[str, Any], audio: bytes | None = None) -> None:
        """A message, and the audio it announces right behind it (nothing gets between them)."""
        async with self.send_lock:
            await self.ws.send_text(json.dumps(message))
            if audio is not None:
                await self.ws.send_bytes(audio)

    async def error(self, rid: str | None, code: str, message: str) -> None:
        await self.send({"type": "error", "id": rid, "code": code, "message": message})

    # --- the loop -----------------------------------------------------------------------------

    async def run(self) -> None:
        speaker = asyncio.create_task(self._speaker())
        try:
            while True:
                frame = await self.ws.receive()
                if frame["type"] == "websocket.disconnect":
                    return
                if frame.get("bytes") is not None:
                    self._audio(frame["bytes"])
                elif frame.get("text") is not None:
                    await self._message(frame["text"])
        finally:
            speaker.cancel()
            for task in [*self.transcribing.values(), self.current_task]:
                if task is not None:
                    task.cancel()

    def _audio(self, chunk: bytes) -> None:
        u = self.utterance
        if u is None or u.too_long:
            return  # a chunk after "end" or "cancel" is late, not an error
        if len(u.pcm) + len(chunk) > MAX_PCM_BYTES:
            u.too_long = True
            u.pcm.clear()
            return
        u.pcm.extend(chunk)

    async def _message(self, raw: str) -> None:
        if len(raw) > MAX_TEXT_FRAME:
            return await self.error(None, "invalid_input", "the message is too large")
        try:
            msg = json.loads(raw)
        except ValueError:
            return await self.error(None, "invalid_input", "a message is a JSON object")
        if not isinstance(msg, dict):
            return await self.error(None, "invalid_input", "a message is a JSON object")
        rid = msg.get("id")
        if rid is not None and (not isinstance(rid, str) or not 0 < len(rid) <= ID_MAX):
            return await self.error(None, "invalid_input", "an id is a short string")
        kind = msg.get("type")
        if kind == "listen":
            await self._listen(rid, msg)
        elif kind == "end":
            await self._end(rid)
        elif kind == "speak":
            await self._speak(rid, msg)
        elif kind == "cancel":
            await self._cancel(rid)
        elif kind == "ping":
            await self.send({"type": "pong"})
        else:
            await self.error(rid, "invalid_input", f"unknown message type {kind!r}")

    # --- listening ----------------------------------------------------------------------------

    async def _listen(self, rid: str | None, msg: dict[str, Any]) -> None:
        rate, language = msg.get("rate", 16000), msg.get("language", "auto")
        if rid is None:
            return await self.error(None, "invalid_input", "listen needs an id")
        if not isinstance(rate, int) or not MIN_RATE <= rate <= MAX_RATE:
            return await self.error(rid, "invalid_input", "the sample rate is out of range")
        if not isinstance(language, str) or not LANGUAGE.match(language):
            return await self.error(rid, "invalid_input", "the language is auto or a code")
        # a new recording replaces one that was never ended (the key was let go elsewhere)
        self.utterance = _Utterance(rid, rate, language, msg.get("incognito") is True)

    async def _end(self, rid: str | None) -> None:
        u = self.utterance
        if u is None or u.id != rid:
            return await self.error(rid, "not_listening", "there is no recording with this id")
        self.utterance = None
        if u.too_long:
            return await self.error(rid, "too_long", "the recording is too long")
        if len(self.transcribing) >= MAX_TRANSCRIBING:
            u.pcm.clear()
            return await self.error(rid, "busy", "earlier recordings are still being read")
        task = asyncio.create_task(self._transcribe(u))
        self.transcribing[u.id] = task

        def forget(_task: asyncio.Task[None], key: str = u.id) -> None:
            self.transcribing.pop(key, None)

        task.add_done_callback(forget)

    async def _transcribe(self, u: _Utterance) -> None:
        try:
            out = await self.service.transcribe(
                self.caller, bytes(u.pcm), u.rate, u.language, u.incognito
            )
        except VoiceError as exc:
            await self.error(u.id, exc.code, exc.message)
            return
        finally:
            u.pcm.clear()  # the recording is gone once it is read
        await self.send({"type": "transcript", "id": u.id, **out})

    # --- speaking -----------------------------------------------------------------------------

    async def _speak(self, rid: str | None, msg: dict[str, Any]) -> None:
        text, conv = msg.get("text"), msg.get("conversation_id")
        if rid is None:
            return await self.error(None, "invalid_input", "speak needs an id")
        if not isinstance(text, str) or not text.strip() or len(text) > MAX_SPEAK_CHARS:
            return await self.error(
                rid, "invalid_input", f"speak needs a text of 1 to {MAX_SPEAK_CHARS} characters"
            )
        conversation_id = None
        if conv is not None:
            try:
                conversation_id = uuid.UUID(str(conv))
            except ValueError:
                return await self.error(rid, "invalid_input", "conversation_id is not an id")
        if len(self.queue) >= MAX_QUEUED:
            return await self.error(rid, "busy", "too much is waiting to be spoken")
        self.queue.append(_Speech(rid, text, conversation_id, msg.get("incognito") is True))
        self.wake.set()

    async def _cancel(self, rid: str | None) -> None:
        """Stop what ``rid`` names, or everything (barge-in: the person speaks again)."""
        cancelled: list[str] = []
        if self.utterance is not None and rid in (None, self.utterance.id):
            cancelled.append(self.utterance.id)
            self.utterance = None
        for key, task in list(self.transcribing.items()):
            if rid in (None, key):
                task.cancel()
                cancelled.append(key)
        kept = []
        for s in self.queue:
            (cancelled.append(s.id) if rid in (None, s.id) else kept.append(s))
        self.queue = kept
        if self.current is not None and rid in (None, self.current.id) and self.current_task:
            self.current_task.cancel()
            cancelled.append(self.current.id)
        for key in dict.fromkeys(cancelled):
            await self.send({"type": "cancelled", "id": key})

    async def _speaker(self) -> None:
        while True:
            await self.wake.wait()
            while self.queue:
                speech = self.queue.pop(0)
                self.current = speech
                self.current_task = asyncio.create_task(self._say(speech))
                with contextlib.suppress(asyncio.CancelledError):
                    await self.current_task
                self.current, self.current_task = None, None
            self.wake.clear()

    async def _say(self, speech: _Speech) -> None:
        if speech.conversation_id not in self.voices:
            self.voices[speech.conversation_id] = await self.service.voice_of(
                self.caller.user_id, speech.conversation_id
            )
        voice = self.voices[speech.conversation_id]
        parts = pieces(speech.text)
        try:
            for seq, part in enumerate(parts):
                audio = await self.service.speak(self.caller, part, voice, speech.incognito)
                await self.send(
                    {
                        "type": "audio",
                        "id": speech.id,
                        "seq": seq,
                        "format": "wav",
                        "text": part,
                        "bytes": len(audio),
                    },
                    audio,
                )
        except VoiceError as exc:
            await self.error(speech.id, exc.code, exc.message)
            return
        await self.send({"type": "spoken", "id": speech.id, "pieces": len(parts)})


@router.get("/api/voice")
async def status(request: Request, user: CurrentUser = Depends(api_user)) -> dict[str, bool]:
    """Whether push to talk can work for this person now: the client shows its key only then."""
    service: VoiceService = request.app.state.voice
    return (await service.available(Caller.of(user))).view()


@router.websocket("/api/voice/ws")
async def voice_ws(ws: WebSocket) -> None:
    await ws.accept()
    state = ws.app.state
    try:
        first = await asyncio.wait_for(ws.receive_text(), AUTH_TIMEOUT)
        msg = json.loads(first)
        token = msg.get("token") if isinstance(msg, dict) and msg.get("type") == "auth" else None
    except (TimeoutError, WebSocketDisconnect, ValueError, KeyError, RuntimeError):
        token = None
    user = None
    if isinstance(token, str) and token:
        async with state.sessionmaker() as db:
            user = await user_of_token(db, token, state.settings.dev_admin_token)
    if user is None:
        with contextlib.suppress(Exception):
            await ws.close(CLOSE_UNAUTHORIZED, "missing or invalid token")
        return
    caller = Caller.of(user)
    service: VoiceService = state.voice
    session = VoiceSession(ws, service, caller, user)
    await session.send({"type": "ready", **(await service.available(caller)).view()})
    with contextlib.suppress(WebSocketDisconnect):
        await session.run()
