"""Chat API for the GUI (E2-01): conversations, streamed answers, settings.

All personal data goes through :class:`topcoat_hub.chat_store.ChatStore`, created for the
calling user (D20). Callers are the dev admin (GUI) or any user with an API key.
"""

import asyncio
import logging
import re
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import Response, StreamingResponse
from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from topcoat_hub import (
    app_scope,
    chat,
    chat_images,
    conversation_context,
    quality,
    research_chat,
    research_profile,
    secretary,
    tool_groups,
)
from topcoat_hub.auth import CurrentUser, api_user
from topcoat_hub.blocks import builtin as blocks_builtin
from topcoat_hub.blocks.service import BlockError
from topcoat_hub.chat_engine import pipeline
from topcoat_hub.chat_engine import turn as turn_prep
from topcoat_hub.chat_engine.answer import (
    _RESEARCH_LINE,
    _WATCHERS,
    _launch,
    _research_watch,
)
from topcoat_hub.chat_engine.common import (
    _chain_ids,
    _defaults,
    _error,
    _http,
    _iso,
    hub_language,
    message_view,
)
from topcoat_hub.chat_engine.turn import Turn
from topcoat_hub.chat_runs import AnswerRun
from topcoat_hub.chat_store import (
    ChatStore,
)
from topcoat_hub.digest_store import DigestStore
from topcoat_hub.github_access import GITHUB_NODE
from topcoat_hub.identity import IDENTITY_NODE
from topcoat_hub.inventory import INVENTORY_NODE
from topcoat_hub.knowledge import KNOWLEDGE_NODE
from topcoat_hub.models import (
    Conversation,
    Message,
    Node,
    User,
)
from topcoat_hub.personas import PersonaError, PersonaStore
from topcoat_hub.segments import SegmentStore, parse_breaks, split
from topcoat_hub.steering import SteeringError
from topcoat_hub.tools import Caller, ToolError
from topcoat_hub.topcoat_self import TOPCOAT_NODE

log = logging.getLogger(__name__)

SEGMENT_BREAKS_KEPT = 50  # newest "new section" moments stored per conversation

# the workspace a conversation belongs to when a browser session names none (app_scope.py)
DEFAULT_APPLICATION = "chat"

router = APIRouter(prefix="/api")

ModelName = Annotated[str, Field(min_length=1, max_length=255)]
Quality = Literal["fast", "balanced", "thorough"]
GroupKey = Literal[
    "docker",
    "journal",
    "host",
    "network",
    "monitors",
    "reach",
    "notes",
    "identity",
    "topcoat",
    "github",
    "shell",
    "workshop",
    "trace",
    "project",
    "wiki",
]


class ToolScope(BaseModel):
    """Which nodes and tool groups the assistant may use in a conversation, and whether it may use
    the internet (E3-04). ``null`` for nodes or groups means all of them, for ``internet`` the
    user's default; the scope only narrows what the user's grants allow. The internet (web search
    and reading pages) is its own switch: it does not depend on the servers chosen."""

    model_config = ConfigDict(extra="forbid")

    nodes: list[Annotated[str, Field(min_length=1, max_length=63)]] | None = Field(
        None, max_length=100
    )
    groups: list[GroupKey] | None = None
    internet: bool | None = None
    # false = incognito: the assistant neither uses nor keeps notes about the user in this
    # conversation; null = the user's setting. It can only switch notes off, never on.
    memory: bool | None = None


NodeName = Annotated[str, Field(min_length=1, max_length=63, pattern=r"^[a-z0-9][a-z0-9-]*$")]


class ConversationContext(BaseModel):
    """A desktop window's view, ids and title; cleaned again by conversation_context.clean."""

    model_config = ConfigDict(extra="forbid")

    module: str = Field(min_length=1, max_length=60)
    props: dict[str, str | int | bool] = Field(default_factory=dict, max_length=8)
    title: str = Field("", max_length=200)


class ConversationCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str = Field("", max_length=200)
    model: ModelName | None = None
    node: NodeName | None = None
    quality: Quality | None = None
    tool_scope: ToolScope | None = None  # null = the user's last choice
    persona_ref: uuid.UUID | None = None  # null = the persona the user has chosen as default
    # the workspace (application) the conversation is started in, e.g. ``dev``
    application: str | None = Field(None, max_length=40, pattern="^[a-z0-9][a-z0-9-]*$")
    # where it was started: the desktop window Clipper is attached to (conversation_context.py)
    context: ConversationContext | None = None


class ConversationUpdate(BaseModel):
    """Only the fields that are present change; ``null`` resets model / node to automatic."""

    model_config = ConfigDict(extra="forbid")

    title: str | None = Field(None, max_length=200)
    model: ModelName | None = None
    node: NodeName | None = None
    quality: Quality | None = None  # null = the user's default
    tool_scope: ToolScope | None = None  # null = everything the user may use
    persona_ref: uuid.UUID | None = None  # null = no persona (E3-06)
    pinned: bool | None = None  # kept open in Start (2026-11-13)
    done: bool | None = None  # marked done; false opens it again


class MessageFeedback(BaseModel):
    model_config = ConfigDict(extra="forbid")

    verdict: Literal["helped", "unfit"] | None = None  # null takes it back


class MessageCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    content: str = Field("", max_length=100_000)
    # E3-01d: a check of the user's servers before the answer. auto = when the question is about
    # them, on = always ("check the servers"), off = never.
    tools: Literal["auto", "on", "off"] = "auto"
    # D125: images (base64, a data URL is fine), re-encoded by the hub before anything keeps them
    images: list[Annotated[str, Field(max_length=chat_images.MAX_UPLOAD_CHARS)]] = Field(
        default_factory=list, max_length=chat_images.MAX_PER_MESSAGE
    )

    @model_validator(mode="after")
    def _says_something(self) -> "MessageCreate":
        if not self.content and not self.images:
            raise ValueError("a message needs text or an image")
        return self


class ChatSettingsUpdate(BaseModel):
    """The user's three settings; a missing or null value resets it to the default."""

    model_config = ConfigDict(extra="forbid")

    system_prompt: str | None = Field(None, max_length=4000)
    temperature: float | None = Field(None, ge=0, le=2)
    address: Literal["du", "sie"] | None = None
    memory_enabled: bool = True  # E2-02: the assistant keeps notes about the user
    # E3-04: the default quality level of the user's conversations (null = unchanged)
    quality: Quality | None = None
    # E3-04: the default of the per-conversation "Internet" switch (null = unchanged)
    internet: bool | None = None


def prompt_history(messages: list[Message]) -> list[dict[str, Any]]:
    """Where the prompt of a conversation changed: the first answer's hash, and every answer whose
    hash differs from the one before (a persona switch, a changed core entry, a different
    context size). Empty when no answer was stored with a hash."""
    out: list[dict[str, Any]] = []
    last: str | None = None
    for m in messages:
        if m.role != "assistant" or not m.prompt_hash or m.prompt_hash == last:
            continue
        out.append(
            {
                "message_id": m.id,
                "prompt_hash": m.prompt_hash,
                "persona_ref": m.persona_ref,
                "first": last is None,
                "at": _iso(m.created_at),
            }
        )
        last = m.prompt_hash
    return out


OPEN_FOR = timedelta(days=1)  # a conversation without activity this long counts as done


def is_open(c: Conversation, now: datetime | None = None) -> bool:
    """Open in Start: pinned, or active within the last day and not marked done since."""
    if c.pinned_at is not None:
        return True
    if c.updated_at is None:
        return False
    last = c.updated_at if c.updated_at.tzinfo else c.updated_at.replace(tzinfo=UTC)
    if last < (now or datetime.now(UTC)) - OPEN_FOR:
        return False
    return c.done_at is None or last > c.done_at


def conversation_view(c: Conversation, message_count: int | None = None) -> dict[str, Any]:
    view = {
        "id": str(c.id),
        "title": c.title,
        "model": c.model,
        "node": c.node,
        "quality": c.quality,
        "tool_scope": c.tool_scope,
        "persona_ref": c.persona_ref,
        "application": c.application,
        "context": c.context,
        "continues_id": str(c.continues_id) if c.continues_id else None,
        "created_at": _iso(c.created_at),
        "updated_at": _iso(c.updated_at),
        "pinned": c.pinned_at is not None,
        "done_at": _iso(c.done_at),
        "open": is_open(c),
    }
    if message_count is not None:
        view["message_count"] = message_count
    return view


# --- conversations ----------------------------------------------------------------------------


@router.get("/conversations")
async def list_conversations(
    request: Request,
    q: str | None = Query(None, max_length=200),
    user: CurrentUser = Depends(api_user),
) -> list[dict[str, Any]]:
    async with request.app.state.sessionmaker() as db:
        store = ChatStore(db, user.uuid)
        rows = await store.list_conversations(q)
        running = await store.running_conversations()
    return [{**conversation_view(c, n), "running": c.id in running} for c, n in rows]


@router.post("/conversations", status_code=201)
async def create_conversation(
    body: ConversationCreate, request: Request, user: CurrentUser = Depends(api_user)
) -> dict[str, Any]:
    async with request.app.state.sessionmaker() as db:
        if body.persona_ref is not None:
            await _visible_persona(db, user, body.persona_ref)
        context = conversation_context.clean(body.context.model_dump() if body.context else None)
        # a conversation from the browser belongs to Chat unless it names a workspace; a program
        # with an API key that names none keeps no ceiling beyond the person's grants (D20)
        application = body.application or (None if user.via_key else DEFAULT_APPLICATION)
        if application == DEFAULT_APPLICATION:
            # a talk at a document of the Ablage belongs to the application that made it (its
            # rights, its place among the open chats), not to Start (2026-11-13)
            made_by = await conversation_context.document_application(db, user.uuid, context)
            if made_by:
                application = made_by
        conv = await ChatStore(db, user.uuid).create_conversation(
            body.title,
            body.model,
            body.node,
            body.tool_scope.model_dump() if body.tool_scope else None,
            str(body.persona_ref) if body.persona_ref else None,
            application,
            body.quality,
            context,
        )
        await db.commit()
        return conversation_view(conv, 0)


@router.get("/conversations/{conversation_id}")
async def get_conversation(
    conversation_id: uuid.UUID, request: Request, user: CurrentUser = Depends(api_user)
) -> dict[str, Any]:
    async with request.app.state.sessionmaker() as db:
        store = ChatStore(db, user.uuid)
        conv = await store.get_conversation(conversation_id)
        if conv is None:
            raise _http(404, "not_found", "unknown conversation")
        messages = await store.messages(conversation_id)
        pictures = await chat_images.ChatImageStore(db, user.uuid).of_messages(
            [m.id for m in messages if m.role == "user"]
        )
        history = prompt_history(messages)
        persona = None
        if conv.persona_ref:
            try:
                found = await PersonaStore(db, user.uuid).get(uuid.UUID(conv.persona_ref))
            except ValueError:
                found = None
            if found is not None:
                persona = {
                    "id": str(found.id),
                    "name": found.name,
                    "key": found.key,
                    "version": found.version,
                    "status": found.status,
                }
        return {
            **conversation_view(conv, len(messages)),
            "running": any(m.status == "running" for m in messages),
            "persona": persona,
            "prompt_hash": history[0]["prompt_hash"] if history else None,
            "prompt_changes": history[1:],
            "messages": [message_view(m, pictures.get(m.id)) for m in messages],
        }


async def _card_language(request: Request, user: CurrentUser, language: str | None) -> str:
    """The language of a work card: the one the GUI shows, else the person's stored one."""
    if language in ("en", "de"):
        return language
    async with request.app.state.sessionmaker() as db:
        row = await db.get(User, user.uuid)
        return (row.language if row else None) or "en"


@router.get("/conversations/{conversation_id}/orders")
async def conversation_orders(
    conversation_id: uuid.UUID,
    request: Request,
    language: str | None = Query(None, pattern="^(en|de)$"),
    user: CurrentUser = Depends(api_user),
) -> list[dict[str, Any]]:
    """The work cards of a conversation (D112, issue #37): each task the secretary handed on in
    it, with the employee, the task, the state and the result summary through the filter."""
    async with request.app.state.sessionmaker() as db:
        if await ChatStore(db, user.uuid).get_conversation(conversation_id) is None:
            raise _http(404, "not_found", "unknown conversation")
    lang = await _card_language(request, user, language)
    return await secretary.cards(request.app.state.sessionmaker, user.uuid, conversation_id, lang)


@router.post("/orders/{order_id}/direct")
async def talk_to_employee_directly(
    order_id: uuid.UUID,
    request: Request,
    language: str | None = Query(None, pattern="^(en|de)$"),
    user: CurrentUser = Depends(api_user),
) -> dict[str, Any]:
    """The button "talk to … directly" on a work card (issue #37): the employee's own
    conversation with the task and the result, made on the first click and found again on the
    next. It is the person's own click and changes nothing outside the person's conversations."""
    lang = await _card_language(request, user, language)
    found = await secretary.talk_directly(request.app.state.sessionmaker, user.uuid, order_id, lang)
    if found is None:
        raise _http(404, "not_found", "unknown order")
    conversation_id, application = found
    return {"conversation_id": str(conversation_id), "application": application}


@router.get("/chat/images/{image_id}")
async def get_chat_image(
    image_id: uuid.UUID, request: Request, user: CurrentUser = Depends(api_user)
) -> Response:
    """An image sent with a message (D125), only for its person (D20): another person's image is
    a 404, whatever its id."""
    async with request.app.state.sessionmaker() as db:
        image = await chat_images.ChatImageStore(db, user.uuid).get(image_id)
    if image is None:
        raise _http(404, "not_found", "unknown image")
    return Response(
        content=image.data, media_type=image.content_type, headers=chat_images.IMAGE_HEADERS
    )


@router.patch("/conversations/{conversation_id}")
async def update_conversation(
    conversation_id: uuid.UUID,
    body: ConversationUpdate,
    request: Request,
    user: CurrentUser = Depends(api_user),
) -> dict[str, Any]:
    fields = {k: getattr(body, k) for k in body.model_fields_set}
    pinned, done = fields.pop("pinned", None), fields.pop("done", None)
    if "tool_scope" in fields:
        fields["tool_scope"] = body.tool_scope.model_dump() if body.tool_scope else None
    async with request.app.state.sessionmaker() as db:
        if fields.get("persona_ref") is not None:
            await _visible_persona(db, user, fields["persona_ref"])
        if "persona_ref" in fields and fields["persona_ref"] is not None:
            fields["persona_ref"] = str(fields["persona_ref"])
        store = ChatStore(db, user.uuid)
        conv = await store.update_conversation(conversation_id, **fields)
        if conv is not None and (pinned is not None or done is not None):
            conv = await store.set_state(conversation_id, pinned=pinned, done=done)
        if conv is None:
            raise _http(404, "not_found", "unknown conversation")
        await db.commit()
        return conversation_view(conv)


async def _visible_persona(db: AsyncSession, user: CurrentUser, persona_id: uuid.UUID) -> None:
    found = await PersonaStore(db, user.uuid).get(persona_id)
    if found is None or found.status != "active":
        raise _http(404, "not_found", "unknown persona")


@router.post("/conversations/{conversation_id}/messages/{message_id}/reference")
async def message_reference(
    conversation_id: uuid.UUID,
    message_id: int,
    request: Request,
    user: CurrentUser = Depends(api_user),
) -> dict[str, str]:
    """The reference of one message of the person's own chat: handing it to the development
    assistant lets it read how that answer was handled (``trace.read``), and nothing else of the
    history. Only the person can ask for it; a model cannot make one up."""
    try:
        ref = await request.app.state.trace.reference(user.uuid, conversation_id, message_id)
    except ToolError as exc:
        raise _http(exc.status, exc.code, exc.message) from exc
    return {"reference": ref, "short": f"{str(conversation_id)[:8]}:{message_id}"}


@router.get("/conversations/{conversation_id}/background")
async def conversation_background(
    conversation_id: uuid.UUID, request: Request, user: CurrentUser = Depends(api_user)
) -> dict[str, str]:
    """What the hub still does in the background for this chat (``running``, ``waiting`` or
    ``idle``): the running summary and the second look. Writing never waits for it; the page
    shows a note while it is not ``idle``."""
    async with request.app.state.sessionmaker() as db:
        if await ChatStore(db, user.uuid).get_conversation(conversation_id) is None:
            raise _http(404, "not_found", "unknown conversation")
    return {"state": request.app.state.digester.work_state(conversation_id)}


@router.get("/conversations/{conversation_id}/segments")
async def conversation_segments(
    conversation_id: uuid.UUID, request: Request, user: CurrentUser = Depends(api_user)
) -> dict[str, Any]:
    """The archive of one continuous conversation: its segments, newest last, with the summary
    written for each closed one before the running summary existed; the last closed one shows
    the running summary of the chat (``null`` where there is none)."""
    settings = request.app.state.settings
    async with request.app.state.sessionmaker() as db:
        store = ChatStore(db, user.uuid)
        conv = await store.get_conversation(conversation_id)
        if conv is None:
            raise _http(404, "not_found", "unknown conversation")
        messages = await store.messages(conversation_id)
        breaks = parse_breaks(conv.segment_breaks)
        stored = {
            r.first_message_id: r
            for r in await SegmentStore(db, user.uuid).for_conversation(conversation_id)
        }
        digest = await DigestStore(db, user.uuid).get(conversation_id)
    segs = split(messages, settings.segment_idle_seconds, breaks)
    return {
        "enabled": settings.main_thread_enabled,
        "idle_seconds": settings.segment_idle_seconds,
        "segments": [
            {
                "first_message_id": s.first_id,
                "last_message_id": s.last_id,
                "started_at": s.started_at.isoformat(),
                "ended_at": s.ended_at.isoformat(),
                "messages": s.count,
                "current": i == len(segs) - 1,
                "summary": stored[s.first_id].summary
                if s.first_id in stored
                else (digest.summary if digest is not None and i == len(segs) - 2 else None),
            }
            for i, s in enumerate(segs)
        ],
    }


class SectionBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    force: bool = False  # "new section" asked by the user, whatever the idle time


@router.post("/conversations/{conversation_id}/section")
async def next_section(
    conversation_id: uuid.UUID,
    body: SectionBody,
    request: Request,
    user: CurrentUser = Depends(api_user),
) -> dict[str, Any]:
    """Before the next message of the continuous conversation: the chat to write in. The same
    chat while it was used within ``segment_idle_seconds``; after a rest, or when the user asked
    for a new section, a new chat that continues it (same model, servers and persona) and
    starts from a hand-over. Nothing is deleted. ``created`` tells which of the two it is."""
    settings = request.app.state.settings
    async with request.app.state.sessionmaker() as db:
        store = ChatStore(db, user.uuid)
        conv = await store.get_conversation(conversation_id)
        if conv is None:
            raise _http(404, "not_found", "unknown conversation")
        messages = [m for m in await store.messages(conversation_id) if m.kind != "progress"]
        last_at = messages[-1].created_at if messages else None
        if last_at is not None and last_at.tzinfo is None:
            last_at = last_at.replace(tzinfo=UTC)
        rested = last_at is not None and (
            (datetime.now(UTC) - last_at).total_seconds() >= settings.segment_idle_seconds
        )
        current = conversation_view(conv, len(messages))
        if not settings.main_thread_enabled or not messages or not (body.force or rested):
            return {"created": False, "conversation": current}
    # nothing to wait for: the new section starts from the running summary of the chat just left
    # (brought up to date after every answer) and the messages that summary does not cover yet
    async with request.app.state.sessionmaker() as db:
        store = ChatStore(db, user.uuid)
        conv = await store.get_conversation(conversation_id)
        if conv is None:
            raise _http(404, "not_found", "unknown conversation")
        nxt = await store.continue_conversation(conv)
        await db.commit()
        view = conversation_view(nxt, 0)
    return {"created": True, "conversation": view}


THREAD_SECTIONS = 8  # sections of the continuous conversation shown above the current one


@router.get("/conversations/{conversation_id}/thread")
async def conversation_thread(
    conversation_id: uuid.UUID, request: Request, user: CurrentUser = Depends(api_user)
) -> dict[str, Any]:
    """The continuous conversation as the user sees it: the chats the open one continues, oldest
    first and the open one last, each with the summary the hub wrote when it was left (``null``
    while there is none). ``more``: older sections exist beyond the ones listed."""
    settings = request.app.state.settings
    async with request.app.state.sessionmaker() as db:
        store = ChatStore(db, user.uuid)
        conv = await store.get_conversation(conversation_id)
        if conv is None:
            raise _http(404, "not_found", "unknown conversation")
        ids = await _chain_ids(store, conv)
        more = len(ids) > THREAD_SECTIONS
        sections: list[dict[str, Any]] = []
        for cid in [*reversed(ids[:THREAD_SECTIONS]), conv.id]:
            c = conv if cid == conv.id else await store.get_conversation(cid)
            if c is None:
                continue
            msgs = [m for m in await store.messages(cid) if m.kind != "progress"]
            summary = None
            if cid != conv.id and msgs:
                last = split(msgs, settings.segment_idle_seconds, parse_breaks(c.segment_breaks))[
                    -1
                ]
                rows = await SegmentStore(db, user.uuid).for_conversation(cid)
                summary = next(
                    (r.summary for r in rows if r.first_message_id == last.first_id), None
                )
                if summary is None:  # the running summary of that chat
                    digest = await DigestStore(db, user.uuid).get(cid)
                    summary = digest.summary if digest is not None else None
            sections.append(
                {
                    "id": str(cid),
                    "title": c.title,
                    "messages": len(msgs),
                    "started_at": _iso(msgs[0].created_at) if msgs else _iso(c.created_at),
                    "ended_at": _iso(msgs[-1].created_at) if msgs else _iso(c.created_at),
                    "summary": summary,
                    "current": cid == conv.id,
                }
            )
    return {
        "enabled": settings.main_thread_enabled,
        "idle_seconds": settings.segment_idle_seconds,
        "sections": sections,
        "more": more,
    }


@router.post("/conversations/{conversation_id}/segments/break")
async def new_segment(
    conversation_id: uuid.UUID, request: Request, user: CurrentUser = Depends(api_user)
) -> dict[str, Any]:
    """ "New section": the next message starts a new segment, as if the conversation had rested.
    Nothing is deleted; the section just closed is handed over like any other.
    Asked twice without a message in between it changes nothing (``created`` false)."""
    async with request.app.state.sessionmaker() as db:
        store = ChatStore(db, user.uuid)
        conv = await store.get_conversation(conversation_id)
        if conv is None:
            raise _http(404, "not_found", "unknown conversation")
        breaks = parse_breaks(conv.segment_breaks)
        messages = [m for m in await store.messages(conversation_id) if m.kind != "progress"]
        last_at = messages[-1].created_at if messages else None
        if last_at is not None and last_at.tzinfo is None:
            last_at = last_at.replace(tzinfo=UTC)
        pending = not messages or (breaks and last_at is not None and breaks[-1] >= last_at)
        if pending:
            return {"created": False}
        breaks.append(datetime.now(UTC))
        conv.segment_breaks = [b.isoformat() for b in breaks[-SEGMENT_BREAKS_KEPT:]]
        await db.commit()
    return {"created": True, "enabled": request.app.state.settings.main_thread_enabled}


@router.post("/conversations/{conversation_id}/messages/{message_id}/feedback")
async def message_feedback(
    conversation_id: uuid.UUID,
    message_id: int,
    body: MessageFeedback,
    request: Request,
    user: CurrentUser = Depends(api_user),
) -> dict[str, Any]:
    """ "Helped" / "did not fit" for one answer — the count per persona is the user's own."""
    value = {"helped": 1, "unfit": -1, None: None}[body.verdict]
    async with request.app.state.sessionmaker() as db:
        msg = await ChatStore(db, user.uuid).set_feedback(conversation_id, message_id, value)
        if msg is None:
            raise _http(404, "not_found", "unknown message")
        await db.commit()
        return message_view(msg)


@router.get("/chat/prompt-snapshots/{digest}")
async def get_prompt_snapshot(
    digest: str, request: Request, user: CurrentUser = Depends(api_user)
) -> dict[str, Any]:
    """The system prompt as it was sent (read-only). Only the user's own (D20): a hash another
    user has is "not found" here."""
    if not re.fullmatch(r"[0-9a-f]{64}", digest):
        raise _http(404, "not_found", "unknown prompt")
    async with request.app.state.sessionmaker() as db:
        snap = await ChatStore(db, user.uuid).get_snapshot(digest)
        if snap is None:
            raise _http(404, "not_found", "unknown prompt")
        persona = None
        if snap.persona_ref:
            try:
                pid = uuid.UUID(snap.persona_ref.split("@")[0])
                found = await PersonaStore(db, user.uuid).get(pid)
                persona = {"id": str(pid), "name": found.name if found else None}
            except ValueError:
                persona = None
        return {
            "hash": snap.hash,
            "text": snap.text,
            "persona_ref": snap.persona_ref,
            "persona": persona,
            "core_version": snap.core_version,
            "created_at": _iso(snap.created_at),
        }


@router.delete("/conversations/{conversation_id}", status_code=204)
async def delete_conversation(
    conversation_id: uuid.UUID, request: Request, user: CurrentUser = Depends(api_user)
) -> None:
    async with request.app.state.sessionmaker() as db:
        if not await ChatStore(db, user.uuid).delete_conversation(conversation_id):
            raise _http(404, "not_found", "unknown conversation")
        await db.commit()


# --- models and settings ------------------------------------------------------------------


@router.get("/chat/models")
async def chat_models(request: Request, _user: CurrentUser = Depends(api_user)) -> list[Any]:
    """Models the picker offers: those on connected nodes that allow gateway requests."""
    return await request.app.state.gateway.list_models()


@router.get("/chat/tools")
async def chat_tools_overview(
    request: Request,
    application: str | None = Query(None, max_length=40),
    user: CurrentUser = Depends(api_user),
) -> dict[str, Any]:
    """What the user could let the assistant use in a conversation: the nodes and tool groups of
    the capabilities they may run (their grants, D20) — the choice never goes beyond this. With
    ``application``: only what that application allows (its ceiling, app_scope.py)."""
    available = await request.app.state.agents.tools.available(Caller.of(user, "agent"))
    limit: dict[str, Any] | None = None
    if application is not None:
        async with request.app.state.sessionmaker() as db:
            limit = await app_scope.ceiling(db, user, application)
        allowed = set(app_scope.groups_within(list(tool_groups.KEYS), limit))
        available = [
            t
            for t in available
            if (tool_groups.group_of(t["capability"]) == tool_groups.WEB)
            or tool_groups.group_of(t["capability"]) in allowed
        ]
        if not app_scope.internet_allowed(limit):
            available = [
                t for t in available if tool_groups.group_of(t["capability"]) != tool_groups.WEB
            ]
    nodes: dict[str, dict[str, list[str]]] = {}
    sources: dict[str, set[str]] = {}  # group -> "node" | "hub": where its tools run
    for t in available:
        group = tool_groups.group_of(t["capability"])
        if group is None or group == tool_groups.WEB:
            continue
        own = t["node"] in (
            INVENTORY_NODE,
            TOPCOAT_NODE,
            GITHUB_NODE,
            KNOWLEDGE_NODE,
            IDENTITY_NODE,
        )
        sources.setdefault(group, set()).add("hub" if own else "node")
        if own:  # the hub's own view, not a server to pick
            continue
        nodes.setdefault(t["node"], {}).setdefault(group, []).append(t["capability"])
    # machines without tools (a model node) belong in the picture too: the user sees them, and the
    # panel says plainly that there is nothing the assistant could do on them. Admins only (they
    # see every machine anyway); the web node is the internet switch, not a server.
    known: dict[str, str] = {}
    if user.is_admin and limit is None:  # machines without tools: not in an application's list
        async with request.app.state.sessionmaker() as db:
            known = {n: s for n, s in (await db.execute(select(Node.name, Node.state))).all()}
        with_tools = {t["node"] for t in available}
        for name in known:
            if name not in with_tools:
                nodes.setdefault(name, {})
    keys = [k for k in tool_groups.present(available) if k != tool_groups.WEB]
    labels = dict(zip(tool_groups.KEYS, [lbl for _, lbl, _ in tool_groups.GROUPS], strict=True))
    return {
        "groups": [
            {"key": k, "label": labels[k], "via": "+".join(sorted(sources.get(k, {"node"})))}
            for k in keys
        ],
        "nodes": [
            {
                "name": n,
                "groups": [k for k in tool_groups.KEYS if k in g],
                "state": known.get(n),
                # what exactly the assistant can call on this server, per group
                "capabilities": {k: sorted(g[k]) for k in tool_groups.KEYS if k in g},
            }
            for n, g in sorted(nodes.items())
        ],
        # the internet: web search / page reading come from one node the hub picks, whatever
        # servers are chosen; null when no node offers them to this user
        "internet": {"node": tool_groups.pick_web_node(available)},
    }


@router.get("/chat/settings")
async def get_chat_settings(request: Request, user: CurrentUser = Depends(api_user)) -> Any:
    async with request.app.state.sessionmaker() as db:
        return await _defaults(db, request, ChatStore(db, user.uuid))


@router.put("/chat/settings")
async def put_chat_settings(
    body: ChatSettingsUpdate, request: Request, user: CurrentUser = Depends(api_user)
) -> Any:
    async with request.app.state.sessionmaker() as db:
        store = ChatStore(db, user.uuid)
        await store.set_settings(
            body.system_prompt,
            body.temperature,
            body.address,
            body.memory_enabled,
            body.quality,
            body.internet,
        )
        await db.commit()
    # the notes that mirror the system prompt and the form of address follow (E3-06)
    try:
        await request.app.state.core.sync_settings(user.uuid, body.system_prompt, body.address)
    except PersonaError as exc:
        raise _http(exc.status, exc.code, exc.message) from exc
    async with request.app.state.sessionmaker() as db:
        return await _defaults(db, request, ChatStore(db, user.uuid))


async def _stream_turn(
    request: Request,
    user: CurrentUser,
    conversation_id: uuid.UUID,
    content: str | None,
    *,
    continue_last: bool = False,
    tools: str = "auto",
    images: list[chat_images.Encoded] | None = None,
) -> Any:
    turn = await turn_prep.prepare(
        request, user, conversation_id, content, continue_last=continue_last, images=images
    )
    # The answer is made by a task of the hub from here on, the preparation included (E3-07): a
    # reader that goes away — or presses stop while the model still reads the prompt — changes
    # nothing about it. Before the first byte a failure is still an HTTP error, as it always was.
    outcome: asyncio.Future[Response | None] = asyncio.get_running_loop().create_future()
    run = await _launch(request, user, turn, pipeline.pipeline(request, user, turn, tools, outcome))
    failed = await asyncio.shield(outcome)
    if failed is not None:
        return failed
    return _sse_response(run.subscribe(0))


HUB_NOTICE = (
    "[Message from the hub, not from the user] A second model checked your last answer and found "
    "the problem described above. Compare it with the conversation. Tell the user in at most "
    "three sentences what was wrong and what is right, in the language of the conversation, "
    "without long apologies."
)


async def speak_up(
    app: Any, user_id: uuid.UUID, conversation_id: uuid.UUID, message_id: int
) -> bool:
    """The second look found an error in answer ``message_id``: the agent of the chat takes it up
    in a message of its own, without waiting for the next question. Only while the chat is quiet
    (no answer is being written) and that answer is still the last message; otherwise the finding
    waits and stands in front of the next answer. Returns whether an answer was started."""
    state = app.state
    if state.runs.running(conversation_id) is not None:
        return False
    async with state.sessionmaker() as db:
        last = await db.scalar(
            select(func.max(Message.id)).where(
                Message.conversation_id == conversation_id, Message.user_id == user_id
            )
        )
        account = await db.get(User, user_id)
    if last != message_id or account is None:
        return False
    user = CurrentUser(id=str(user_id), name=account.name, role=account.role)
    request: Any = SimpleNamespace(app=app)
    run = None
    # the first try looks at the facts (a forced check); a chat that has no tool for it answers
    # without one
    for tools in ("on", "off"):
        turn = await turn_prep.prepare(request, user, conversation_id, None, hidden=HUB_NOTICE)
        turn.from_hub = True
        outcome: asyncio.Future[Response | None] = asyncio.get_running_loop().create_future()
        run = await _launch(
            request, user, turn, pipeline.pipeline(request, user, turn, tools, outcome)
        )
        if await asyncio.shield(outcome) is None:
            break
        run = None
    if run is None:
        return False
    state.bus.publish(
        "chat_message",
        {
            "conversation_id": str(conversation_id),
            "message_id": run.message_id,
            "_user": str(user_id),
        },
    )
    return True


def _is_thorough(turn: Turn) -> bool:
    return research_profile.CHAT_DEPTH.get(quality.level_of(turn.quality), "default") == "thorough"


SSE_HEADERS = {"Cache-Control": "no-cache", "X-Accel-Buffering": "no"}


def _sse_response(body: AsyncIterator[str]) -> StreamingResponse:
    return StreamingResponse(body, media_type="text/event-stream", headers=SSE_HEADERS)


@router.post("/conversations/{conversation_id}/messages")
async def send_message(
    conversation_id: uuid.UUID,
    body: MessageCreate,
    request: Request,
    user: CurrentUser = Depends(api_user),
) -> Any:
    """Add the user's message and stream the answer (``text/event-stream``). Its images are
    re-encoded first (D125): one that is not a picture refuses the message, nothing is stored."""
    try:
        images = [
            await asyncio.to_thread(chat_images.reencode, chat_images.decode_upload(i))
            for i in body.images
        ]
    except chat_images.ImageError as exc:
        return _error(422, exc.code, exc.message)
    return await _stream_turn(
        request, user, conversation_id, body.content, tools=body.tools, images=images
    )


@router.post("/conversations/{conversation_id}/regenerate")
async def regenerate(
    conversation_id: uuid.UUID,
    request: Request,
    tools: Literal["auto", "on", "off"] = Query("auto"),
    user: CurrentUser = Depends(api_user),
) -> Any:
    """Replace the last answer with a new one (or answer a message that got no answer)."""
    return await _stream_turn(request, user, conversation_id, None, tools=tools)


@router.post("/conversations/{conversation_id}/continue")
async def continue_answer(
    conversation_id: uuid.UUID, request: Request, user: CurrentUser = Depends(api_user)
) -> Any:
    """Continue the last answer if it was cut off (status ``truncated``); the new text is
    appended to that message."""
    return await _stream_turn(request, user, conversation_id, None, continue_last=True)


# --- answers that outlive the connection (E3-07) -------------------------------------------


@router.get("/chat/messages/{message_id}/stream")
async def attach_to_answer(
    message_id: int,
    request: Request,
    offset: int = Query(0, ge=0),
    user: CurrentUser = Depends(api_user),
) -> Any:
    """Follow an answer from the event ``offset`` on (the same events, numbered, that the request
    which started it got): the rest of a running answer, or — when the run is over and gone, or the
    hub restarted — one ``snapshot`` event with the stored message. Only the user's own."""
    run = request.app.state.runs.live(message_id)
    if run is not None and run.user_id == user.uuid:
        return _sse_response(run.subscribe(offset))
    async with request.app.state.sessionmaker() as db:
        row = await db.scalar(
            select(Message).where(Message.id == message_id, Message.user_id == user.uuid)
        )
        if row is None or row.role != "assistant":
            raise _http(404, "not_found", "unknown answer")
        view = message_view(row)
        conversation_id = str(row.conversation_id)

    async def snapshot() -> AsyncIterator[str]:
        yield chat.sse("snapshot", {"conversation_id": conversation_id, "message": view})

    return _sse_response(snapshot())


@router.post("/chat/messages/{message_id}/stop")
async def stop_answer(
    message_id: int, request: Request, user: CurrentUser = Depends(api_user)
) -> dict[str, Any]:
    """Stop a running answer for real (closing the page does not): the text so far is kept as
    the answer, status ``stopped``. ``409 not_running`` if it is over already."""
    stopped = await request.app.state.runs.stop(user.uuid, message_id)
    async with request.app.state.sessionmaker() as db:
        row = await db.scalar(
            select(Message).where(Message.id == message_id, Message.user_id == user.uuid)
        )
        if row is None or row.role != "assistant":
            raise _http(404, "not_found", "unknown answer")
        if not stopped:
            raise _http(409, "not_running", "this answer is not being generated any more")
        return message_view(row)


class SteerBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    text: str = Field(min_length=1, max_length=2000)


class PauseBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    paused: bool


def _steerable(request: Request, user: CurrentUser, message_id: int) -> AnswerRun:
    """The running answer of this user whose check can be steered, else a clear error."""
    run = request.app.state.runs.live(message_id)
    if run is None or run.user_id != user.uuid or run.finished:
        raise _http(409, "not_running", "this answer is not being generated any more")
    if run.agent_task_id is None:
        raise _http(409, "no_check", "no server check is running for this answer")
    return run


@router.post("/chat/messages/{message_id}/steer")
async def steer_check(
    message_id: int, body: SteerBody, request: Request, user: CurrentUser = Depends(api_user)
) -> dict[str, Any]:
    """A note from the person to the check that runs for this answer: it reads it before its next
    turn (E7-01). The check keeps its rights, tools and limits; the note is text, nothing else."""
    run = _steerable(request, user, message_id)
    assert run.agent_task_id is not None  # noqa: S101  (checked by _steerable)
    try:
        note = request.app.state.steering.push(run.agent_task_id, body.text)
    except SteeringError as exc:
        raise _http(409 if exc.code == "too_many" else 422, exc.code, str(exc)) from exc
    _, chunk = run.turn.activity.add("steer", status="info", detail=note)
    if chunk:
        run.publish(chunk)
    return {"queued": True, "text": note}


@router.post("/chat/messages/{message_id}/pause")
async def pause_check(
    message_id: int, body: PauseBody, request: Request, user: CurrentUser = Depends(api_user)
) -> dict[str, Any]:
    """Hold the check of this answer before its next turn, or let it go on (E7-01). A pause does
    not use up its time budget; one nobody ends is ended after a quarter of an hour."""
    run = _steerable(request, user, message_id)
    assert run.agent_task_id is not None  # noqa: S101
    board = request.app.state.steering
    if body.paused:
        board.pause(run.agent_task_id)
    else:
        board.resume(run.agent_task_id)
    _, chunk = run.turn.activity.add(
        "steer", status="info", data={"event": "pause" if body.paused else "resume"}
    )
    if chunk:
        run.publish(chunk)
    return {"paused": body.paused}


@router.post("/conversations/{conversation_id}/stop")
async def stop_conversation_answer(
    conversation_id: uuid.UUID, request: Request, user: CurrentUser = Depends(api_user)
) -> dict[str, Any]:
    """Stop the answer that is being generated in this conversation — for a reader that does not
    know the message yet (the model is still reading the prompt). ``409 not_running`` if there is
    none."""
    if not await request.app.state.runs.stop_conversation(user.uuid, conversation_id):
        raise _http(409, "not_running", "no answer is being generated in this conversation")
    return {"stopped": True}


class ResearchInChat(BaseModel):
    model_config = ConfigDict(extra="forbid")

    question: str = Field(min_length=3, max_length=2000)
    # the message that offered it: marked as taken, so its button does not start a second one
    offer_message_id: int | None = None


@router.post("/conversations/{conversation_id}/research", status_code=202)
async def start_research_in_chat(
    conversation_id: uuid.UUID,
    body: ResearchInChat,
    request: Request,
    user: CurrentUser = Depends(api_user),
) -> Any:
    """The person clicked "Als Recherche starten": the research runs as a job of its own, its
    progress is written into a message of this conversation and its end brings the chat's summary
    (the same as a thorough research the chat starts itself, ``_research_start_events``)."""
    from topcoat_hub.api.research import _may_research

    state = request.app.state
    await _may_research(request, user)
    question = body.question.strip()
    language = hub_language("", question)
    async with state.sessionmaker() as db:
        store = ChatStore(db, user.uuid)
        if await store.get_conversation(conversation_id) is None:
            return _error(404, "not_found", "no such conversation")
    try:
        run = state.blocks.start(
            user, blocks_builtin.PROGRAMS[research_chat.PROGRAM], {"question": question}
        )
    except BlockError as exc:
        return _error(exc.status, exc.code, exc.message)
    async with state.sessionmaker() as db:
        store = ChatStore(db, user.uuid)
        if body.offer_message_id is not None:
            await store.update_message(
                conversation_id,
                body.offer_message_id,
                report={"research_offer": {"question": question, "started": str(run.run_id)}},
            )
        msg = await store.add_message(
            conversation_id, "assistant", _RESEARCH_LINE[language], status="running"
        )
        await db.commit()
        await db.refresh(msg)
        view = message_view(msg)
    watch = asyncio.ensure_future(
        _research_watch(request.app, user, conversation_id, msg.id, run.run_id, language, question)
    )
    _WATCHERS.add(watch)
    watch.add_done_callback(_WATCHERS.discard)
    state.bus.publish(
        "chat_message",
        {"conversation_id": str(conversation_id), "message_id": msg.id, "_user": str(user.uuid)},
    )
    return view
