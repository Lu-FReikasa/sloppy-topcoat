"""The voice path from a client to the node's voice services (D127, issue #55).

The browser or the desktop client holds one WebSocket to the hub (``api/voice.py``) over the
person's TLS connection. It sends recorded audio in chunks and gets the transcript back; it sends
the text of an answer, sentence by sentence as the answer is written, and gets audio chunks back.
This module is the part without the socket: which node may do it for this person, the WAV around
the raw recording, the pieces a text is spoken in, the voice of the conversation's persona.

The rules it keeps:

- **Nothing is stored.** The audio lives in memory for one request. The calls go through
  :meth:`ToolService.invoke` (grant, node config, schema: every check of a capability call) and are
  audited without their input or output (``private``); an incognito call leaves no row at all.
- **The transcript is not sent anywhere from here.** The client posts it as the person's message
  through the normal chat path, so every rule of the chat applies to it and a spoken "yes" is the
  same as a typed one: it never confirms an action, the approval cards stay click or key
  (D15, D83). Nothing in this module or its socket reaches an approval.
"""

import base64
import logging
import re
import struct
import uuid
from dataclasses import dataclass
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from topcoat_hub.chat_store import ChatStore
from topcoat_hub.personas import PersonaStore
from topcoat_hub.tools import Caller, ToolError, ToolService

log = logging.getLogger(__name__)

TRANSCRIBE = "voice.transcribe"
SPEAK = "voice.speak"

# the node's limits (node/internal/voice/voice.go): a WAV of at most 2.5 MB, 600 characters a call
MAX_WAV_BYTES = 2_500_000
WAV_HEADER_BYTES = 44
MAX_PCM_BYTES = MAX_WAV_BYTES - WAV_HEADER_BYTES
MAX_PIECE_CHARS = 600
# what one ``speak`` request may hold: a long answer, not a book
MAX_SPEAK_CHARS = 8000
MIN_RATE, MAX_RATE = 8000, 48000
# whisper on a CPU takes a while for a minute of speech; Piper is quick
TRANSCRIBE_TIMEOUT = 120.0
SPEAK_TIMEOUT = 60.0
# the base64 of the largest WAV plus the few other fields
TRANSCRIBE_INPUT_BYTES = (MAX_WAV_BYTES * 4) // 3 + 4096
PRIVATE = "voice: audio and speech are not kept (D127)"

VOICE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
LANGUAGE = re.compile(r"^(auto|[a-z]{2,3})$")


class VoiceError(Exception):
    """A voice request that cannot be done; ``code`` goes to the client."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code, self.message = code, message


def wav(pcm: bytes, rate: int) -> bytes:
    """A mono 16-bit PCM WAV around raw little-endian samples."""
    if not MIN_RATE <= rate <= MAX_RATE:
        raise VoiceError("invalid_audio", f"the sample rate must be {MIN_RATE} to {MAX_RATE} Hz")
    if len(pcm) % 2:
        pcm = pcm[:-1]  # half a sample is a broken chunk's end
    header = b"RIFF" + struct.pack("<I", 36 + len(pcm)) + b"WAVE"
    header += b"fmt " + struct.pack("<IHHIIHH", 16, 1, 1, rate, rate * 2, 2, 16)
    header += b"data" + struct.pack("<I", len(pcm))
    return header + pcm


_SENTENCE_END = re.compile(r"(?<=[.!?…:;])\s+|\n+")
_MARKUP = [
    (re.compile(r"```.*?(```|$)", re.S), " "),  # code is not read out
    (re.compile(r"`([^`]*)`"), r"\1"),
    (re.compile(r"!\[[^\]]*\]\([^)]*\)"), " "),  # an image
    (re.compile(r"\[([^\]]+)\]\([^)]*\)"), r"\1"),  # a link: its text
    (re.compile(r"https?://\S+"), " "),
    (re.compile(r"^\s{0,3}(#{1,6}|>|[-*+]|\d+[.)])\s+", re.M), ""),
    (re.compile(r"(\*\*|__|\*|_|~~)(?=\S)(.+?)(?<=\S)\1"), r"\2"),
    (re.compile(r"\|"), " "),
]


def speakable(text: str) -> str:
    """The text without the marks of Markdown that a voice would read out as signs."""
    for pattern, repl in _MARKUP:
        text = pattern.sub(repl, text)
    return re.sub(r"[ \t]+", " ", text).strip()


def pieces(text: str, limit: int = MAX_PIECE_CHARS) -> list[str]:
    """The text in the pieces it is spoken in: one sentence each (the first sound comes sooner),
    a sentence longer than ``limit`` cut at a comma or a space before it."""
    out: list[str] = []
    for sentence in _SENTENCE_END.split(speakable(text)):
        rest = sentence.strip()
        while len(rest) > limit:
            cut = max(rest.rfind(", ", 0, limit), rest.rfind(" ", 0, limit))
            cut = cut + 1 if cut > limit // 3 else limit
            out.append(rest[:cut].strip())
            rest = rest[cut:].strip()
        if any(ch.isalnum() for ch in rest):
            out.append(rest)
    return out


@dataclass(frozen=True)
class Availability:
    transcribe: bool
    speak: bool

    def view(self) -> dict[str, bool]:
        return {"transcribe": self.transcribe, "speak": self.speak}


class VoiceService:
    def __init__(self, sessionmaker: async_sessionmaker[AsyncSession], tools: ToolService) -> None:
        self.sessionmaker = sessionmaker
        self.tools = tools

    async def _node(self, caller: Caller, capability: str) -> uuid.UUID | None:
        """A connected node that offers the capability and that this person may use there."""
        for entry in await self.tools.usable_tools(caller):
            if entry["capability"] == capability:
                return uuid.UUID(entry["node_id"])
        return None

    async def available(self, caller: Caller) -> Availability:
        usable = {e["capability"] for e in await self.tools.usable_tools(caller)}
        return Availability(TRANSCRIBE in usable, SPEAK in usable)

    async def _call(
        self,
        caller: Caller,
        capability: str,
        tool_input: dict[str, Any],
        *,
        incognito: bool,
        wait_seconds: float,
        max_input_bytes: int | None = None,
    ) -> dict[str, Any]:
        node_id = await self._node(caller, capability)
        if node_id is None:
            raise VoiceError(
                "no_voice_node",
                f"no connected node offers {capability} to you (switch on tools.voice on a node)",
            )
        try:
            result = await self.tools.invoke(
                caller,
                node_id,
                capability,
                tool_input,
                unrecorded=incognito,
                private=PRIVATE,
                wait_seconds=wait_seconds,
                **({"max_input_bytes": max_input_bytes} if max_input_bytes else {}),
            )
        except ToolError as exc:
            raise VoiceError(exc.code, exc.message) from exc
        if not result.get("ok"):
            err = result.get("error") or {}
            raise VoiceError(err.get("code", "error"), err.get("message", f"{capability} failed"))
        output = result.get("output")
        if not isinstance(output, dict):
            raise VoiceError("error", f"{capability} gave no answer")
        return output

    async def transcribe(
        self, caller: Caller, pcm: bytes, rate: int, language: str = "auto", incognito: bool = False
    ) -> dict[str, str]:
        """The text of one recording (raw 16-bit mono samples at ``rate``)."""
        if not pcm:
            return {"text": "", "language": ""}
        if len(pcm) > MAX_PCM_BYTES:
            raise VoiceError("too_long", "the recording is too long")
        if not LANGUAGE.match(language):
            raise VoiceError("invalid_input", "the language must be auto or a language code")
        audio = base64.b64encode(wav(pcm, rate)).decode("ascii")
        out = await self._call(
            caller,
            TRANSCRIBE,
            {"audio_base64": audio, "language": language},
            incognito=incognito,
            wait_seconds=TRANSCRIBE_TIMEOUT,
            max_input_bytes=TRANSCRIBE_INPUT_BYTES,
        )
        return {"text": str(out.get("text", "")).strip(), "language": str(out.get("language", ""))}

    async def speak(
        self, caller: Caller, text: str, voice: str | None = None, incognito: bool = False
    ) -> bytes:
        """One piece (at most 600 characters) as a WAV. A persona's voice that the node does not
        have is not a failure: the node's own voice speaks then."""
        tool_input: dict[str, Any] = {"text": text[:MAX_PIECE_CHARS]}
        if voice:
            tool_input["voice"] = voice
        try:
            out = await self._call(
                caller, SPEAK, tool_input, incognito=incognito, wait_seconds=SPEAK_TIMEOUT
            )
        except VoiceError as exc:
            if not voice or exc.code in ("no_voice_node", "not_granted", "node_offline"):
                raise
            log.info("voice %s failed (%s), speaking with the node's voice", voice, exc.code)
            out = await self._call(
                caller,
                SPEAK,
                {"text": tool_input["text"]},
                incognito=incognito,
                wait_seconds=SPEAK_TIMEOUT,
            )
        try:
            return base64.b64decode(str(out.get("audio_base64", "")), validate=True)
        except ValueError as exc:
            raise VoiceError("error", "voice.speak gave no audio") from exc

    async def voice_of(self, user_id: uuid.UUID, conversation_id: uuid.UUID | None) -> str | None:
        """The voice of the persona this conversation speaks with (else of the person's default
        persona); ``None`` = the node's own voice. Only what this user may read (D20)."""
        async with self.sessionmaker() as db:
            personas = PersonaStore(db, user_id)
            persona = None
            if conversation_id is not None:
                conv = await ChatStore(db, user_id).get_conversation(conversation_id)
                ref = conv.persona_ref if conv is not None else None
                if ref:
                    try:
                        persona = await personas.get(uuid.UUID(ref.split("@", 1)[0]))
                    except ValueError:
                        persona = None
            if persona is None:
                persona = await personas.default()
            name = (persona.defaults or {}).get("voice") if persona is not None else None
        return name if isinstance(name, str) and VOICE_NAME.match(name) else None
