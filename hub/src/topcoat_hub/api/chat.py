"""Chat API for the GUI (E2-01): conversations, streamed answers, settings.

All personal data goes through :class:`topcoat_hub.chat_store.ChatStore`, created for the
calling user (D20). Callers are the dev admin (GUI) or any user with an API key.
"""

import asyncio
import contextlib
import json
import logging
import re
import time
import uuid
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse
from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from topcoat_hub import (
    agent_contract,
    answer_gate,
    answer_guard,
    app_scope,
    application_brief,
    applications,
    chat,
    chat_images,
    chat_loop,
    chat_policy,
    chat_review,
    chat_tools,
    check_order,
    check_steps,
    claims,
    conversation_context,
    effort_stages,
    handoff,
    local_tools,
    project_changesets,
    project_issues,
    project_iterations,
    project_write,
    quality,
    reaction,
    report,
    research_chat,
    research_profile,
    secretary,
    tool_groups,
    wiki_tools,
)
from topcoat_hub.activity import ActivityLog, CheckTrace, ThinkingTrace
from topcoat_hub.agent_service import AgentError
from topcoat_hub.agent_store import AgentStore
from topcoat_hub.auth import CurrentUser, api_user
from topcoat_hub.blocks import builtin as blocks_builtin
from topcoat_hub.blocks.service import BlockError
from topcoat_hub.chat_runs import AnswerRun
from topcoat_hub.chat_store import (
    ChatStore,
    title_from,
)
from topcoat_hub.digest_store import DigestStore
from topcoat_hub.gateway import (
    CONTEXT_FULL_MESSAGE,
    GatewayError,
    StreamStatus,
    Upstream,
    is_context_full,
    spawn,
    split_model,
)
from topcoat_hub.github_access import GITHUB_NODE
from topcoat_hub.identity import IDENTITY_NODE
from topcoat_hub.inventory import INVENTORY_NODE
from topcoat_hub.job_log import set_job_context
from topcoat_hub.knowledge import KNOWLEDGE_NODE
from topcoat_hub.knowledge_rules import keywords
from topcoat_hub.memory import Recalled, explicit_request, is_followup, is_personal
from topcoat_hub.memory import _answer_text as memory_answer_text
from topcoat_hub.models import (
    AgentTask,
    Conversation,
    Message,
    Node,
    NodeInventory,
    ProjectChangeSet,
    User,
)
from topcoat_hub.personas import PersonaError, PersonaStore
from topcoat_hub.project_base import ProjectError
from topcoat_hub.project_changes import ChangeStore
from topcoat_hub.project_changesets import ChangeSetStore, SetError
from topcoat_hub.project_events import PERSON
from topcoat_hub.project_store import ProjectStore
from topcoat_hub.promise import announced_action
from topcoat_hub.report_store import ReportStore
from topcoat_hub.review_store import ReviewStore
from topcoat_hub.segments import SegmentStore, parse_breaks, split
from topcoat_hub.steering import SteeringError
from topcoat_hub.tools import Caller, ToolError
from topcoat_hub.topcoat_self import TOPCOAT_NODE, TOPCOAT_NODE_ID
from topcoat_hub.ui.access import resolve_access
from topcoat_hub.ui.store import TemplateStore
from topcoat_hub.views import node_summary

log = logging.getLogger(__name__)

RECALL_MAX_TOKENS = 400  # at most this much of the prompt goes to memory notes
SEGMENT_BREAKS_KEPT = 50  # newest "new section" moments stored per conversation
SEGMENT_BLOCK_TOKENS = 600  # at most this much goes to hand-over and recalled segments

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


def _error(status: int, code: str, message: str, retry_after: int | None = None) -> JSONResponse:
    detail: dict[str, Any] = {"code": code, "message": message}
    headers = {}
    if retry_after:
        detail["retry_after"] = retry_after
        headers["Retry-After"] = str(retry_after)
    return JSONResponse({"detail": detail}, status_code=status, headers=headers)


def _http(status: int, code: str, message: str) -> HTTPException:
    return HTTPException(status, detail={"code": code, "message": message})


def _iso(value: Any) -> str | None:
    """A moment as UTC ISO text (E3-07 D2): the database stores timestamptz, the API always says
    UTC, whatever time zone the session of the database connection has; the browser shows it in the
    time zone of its own system."""
    if value is None:
        return None
    if value.tzinfo is None:
        return str(value.replace(tzinfo=UTC).isoformat())
    return str(value.astimezone(UTC).isoformat())


def message_view(m: Message, images: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    """A stored message for the browser; ``images`` are the pictures sent with it (D125)."""
    return {
        "id": m.id,
        "role": m.role,
        "content": m.content,
        "model": m.model,
        "node": m.node,
        "fallback_from": m.fallback_from,
        "status": m.status,
        "error": m.error,
        "prompt_tokens": m.prompt_tokens,
        "completion_tokens": m.completion_tokens,
        "first_token_ms": m.first_token_ms,
        "duration_ms": m.duration_ms,
        "context_trimmed": m.context_trimmed,
        "verification": m.verification,
        "memory_refs": m.memory_refs or [],
        "agent_task_id": str(m.agent_task_id) if m.agent_task_id else None,
        "prompt_hash": m.prompt_hash,
        "persona_ref": m.persona_ref,
        "kind": m.kind,
        "report": m.report,
        "activity": m.activity or [],
        "feedback": {1: "helped", -1: "unfit"}.get(m.feedback or 0),
        "created_at": _iso(m.created_at),
        **(
            {"images": [{k: i[k] for k in ("id", "width", "height")} for i in images]}
            if images
            else {}
        ),
    }


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


CHAIN_MAX = 30  # links of a chain of sections that are followed (newest first)


async def _chain_ids(store: ChatStore, conv: Conversation) -> list[uuid.UUID]:
    """The chats ``conv`` continues, nearest first (a section's predecessors)."""
    out: list[uuid.UUID] = []
    seen = {conv.id}
    cur = conv
    while cur.continues_id is not None and cur.continues_id not in seen and len(out) < CHAIN_MAX:
        prev = await store.get_conversation(cur.continues_id)
        if prev is None:
            break
        out.append(prev.id)
        seen.add(prev.id)
        cur = prev
    return out


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


async def _defaults(db: AsyncSession, request: Request, store: ChatStore) -> dict[str, Any]:
    settings = await store.get_settings()
    user = {
        "system_prompt": settings.system_prompt if settings else None,
        "temperature": settings.temperature if settings else None,
        "address": (settings.address if settings else None) or chat.DEFAULT_ADDRESS,
        "memory_enabled": settings.memory_enabled if settings else True,
        "quality": quality.level_of(settings.chat_quality if settings else None),
        "internet": settings.internet if settings else True,
        "persona_id": str(settings.persona_id) if settings and settings.persona_id else None,
    }
    return {
        "user": user,
        "hub": {
            # model families whose quality level also sets how long the model thinks
            "effort_families": chat.effort_families(request.app.state.settings.chat_effort),
        },
        # what a conversation without its own pick uses: the model of the row of the chat
        "effective_model": (
            await request.app.state.gateway.resolver.resolve("answer", application="chat")
        ).model,
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


# --- answering -----------------------------------------------------------------------------


async def _brief_block(request: Request, user: CurrentUser, turn: "Turn") -> str:
    """What the application of this conversation is about right now (its projects, its
    repositories), so the assistant knows what "the project" means before it looks anything up.
    Not for a continuation: the first part of the answer had it."""
    if turn.continues is not None:
        return ""
    talk = " ".join(c for r, c in turn.history[-6:] if r == "user")
    brief = await application_brief.brief(request.app.state, user.uuid, turn.application, talk)
    if not turn.context:
        return brief
    async with request.app.state.sessionmaker() as db:
        place = await conversation_context.block(db, user.uuid, turn.context)
    return "\n\n".join(x for x in (place, brief) if x)


async def _core_text(request: Request, user: CurrentUser, turn: "Turn", context_length: int) -> str:
    """The core block that opens this conversation's system prompt (E3-06): the same text for as
    long as no core entry changed, so the prompt prefix stays byte-identical. A failure here must
    not cost the user their answer: the chat then runs with the fixed rules alone."""
    try:
        result = await request.app.state.core.block(
            user.uuid, turn.conversation_id, context_length, turn.memory_enabled
        )
    except Exception:
        log.exception("core block failed; answering without it")
        return ""
    turn.persona_ref, turn.core_stamp = result.persona_ref, result.stamp
    return result.text


REPORT_LEAD = "[Report card of the server check, already shown to the user]"
# the model sometimes copies the lead from the history and writes a card of its own ("Search
# running ...") although no check ran: such a made-up card never reaches the user or the history
_FAKE_CARD = re.compile(r"\n*\[Report card of the server check[^\]]*\].*\Z", re.S | re.I)


def starts_with_fake_card(text: str) -> bool:
    """Does the answer open with a report card the model wrote itself? Everything after it is
    just as made up (claimed commits, pushes, results), so such an answer counts as empty."""
    return bool(re.match(r"\s*\[Report card of the server check", text, re.I))


def strip_fake_card(text: str) -> str:
    """The answer without a report card the model wrote itself (the hub alone builds cards)."""
    return _FAKE_CARD.sub("", text).rstrip() or text


def history_pairs(
    messages: list[Message], keep_last: bool = False, extra: dict[int, str] | None = None
) -> list[tuple[str, str]]:
    """The stored conversation as ``(role, text)`` for the prompt (E3-07 D3): a progress line is
    not part of it, a report card is (the model must be able to answer a follow-up from it), and
    the hub's confirmation line is left out, and the card and the closing bubble
    become one turn (a chat template wants the roles to alternate). ``keep_last``: the last
    message is a cut-off answer that is being continued and stays a turn of its own. ``extra``:
    text that follows a message (by id), where its images stand (D125)."""
    out: list[tuple[str, str]] = []
    body = messages[:-1] if keep_last and messages else messages
    for m in body:
        if m.kind == "progress":
            continue
        if m.role == "assistant" and m.content in CONFIRMATIONS.values():
            continue  # the hub's own "looking now" line: the model must not learn to open with it
        text = f"{REPORT_LEAD}\n{m.content}" if m.kind == "report" else m.content
        if extra and m.id in extra:
            text = (text + extra[m.id]).strip()
        if out and out[-1][0] == m.role == "assistant":
            out[-1] = (m.role, f"{out[-1][1]}\n\n{text}")
        else:
            out.append((m.role, text))
    if keep_last and messages:
        out.append((messages[-1].role, messages[-1].content))
    return out


def task_pairs(messages: list[Message]) -> list[tuple[str, str]]:
    """The conversation as a check's task reads it (chat_tools.conversation_for_task): the person's
    messages and the answers, a report card only as its summary line (its evidence would crowd out
    what was said), no progress lines and no confirmation bubbles."""
    out: list[tuple[str, str]] = []
    for m in messages:
        if m.kind == "progress":
            continue
        if m.role == "assistant" and m.content in CONFIRMATIONS.values():
            continue
        text = chat_tools.report_gist(m.content) if m.kind == "report" else m.content.strip()
        if not text:
            continue
        if out and out[-1][0] == m.role == "assistant":
            out[-1] = (m.role, f"{out[-1][1]}\n{text}")
        else:
            out.append((m.role, text))
    return out


@dataclass
class Turn:
    """Everything one answer needs, read in one short transaction."""

    conversation_id: uuid.UUID
    requested: str  # model or model@node for the gateway
    model: str
    history: list[tuple[str, str]]  # stored messages; a continued answer is the last one
    user_prompt: str | None
    address: str
    capabilities: chat.Capabilities
    memory_enabled: bool
    temperature: float | None
    new_user_message: dict[str, Any] | None
    started: float  # monotonic time of the request, for first-token and total time
    continues: Message | None = None  # the truncated answer being continued
    # Notes the previous answer used (its ``memory_refs``): given again for a follow-up such
    # as "Wie sicher bist du dir?".
    last_refs: tuple[uuid.UUID, ...] = ()
    # an earlier answer of this conversation already asked about an outdated note (E3-05: at most
    # one such question per conversation)
    care_asked: bool = False
    # The question mentions the machine world without clearly asking about the user's own: it is
    # answered plainly and the browser offers a check (``meta.check_offer``).
    check_offer: bool = False
    # a server check ran in this turn: its findings back what the answer says (answer_guard)
    checked: bool = False
    # The model is offered ``look_at_my_systems`` in this turn and decides itself (E3-04); the
    # groups of tools it would look at (labels for the function's description).
    decide: bool = False
    decide_groups: tuple[str, ...] = ()
    # the machines and group keys the fixed fields may name, and the tools the scope offers
    # (pruefauftrag T3.1/T3.2); empty when the model is offered the old one-field function
    decide_targets: tuple[str, ...] = ()
    decide_keys: tuple[str, ...] = ()
    decide_available: tuple[dict[str, Any], ...] = ()
    # the programs the model may propose a hand-over to (``hand_over``): (key, what it can do)
    handover_targets: tuple[tuple[str, str], ...] = ()
    # the conversation belongs to a program of the desktop (not one a program opened by the API)
    in_program: bool = False
    # the nodes / tool groups the user allowed in this conversation and the internet switch (E3-04),
    # settled with the user's default: always a dict, ``internet`` always set
    scope: dict[str, Any] | None = None
    internet: bool = True
    # the node the web search of this answer comes from (the hub's pick; None = no node offers it)
    internet_node: str | None = None
    # quality level of this answer and the models it uses (E3-04, quality.py)
    quality: str = "balanced"
    verify_model: str | None = None
    # the chat policy at this answer's effort (the setting, chat_policy.py)
    policy: chat_policy.ChatPolicy = chat_policy.DEFAULT
    second_look: bool = False
    # the persona and core stamp the system prompt of this answer was built from (E3-06, 11a)
    persona_ref: str | None = None
    core_stamp: str | None = None
    # the message the answer is written into (a placeholder with status "running", or the
    # truncated one that is continued); set when the run starts (E3-07)
    message_id: int | None = None
    # the hub is going down: a cut answer is "interrupted", not "stopped"
    interrupted: bool = False
    # E3-07 D3: the id the run is registered under (the placeholder), and the language of the user
    # (the hub's own sentences, such as the confirmation bubble, are written in it)
    run_key: int | None = None
    empty_answer: bool = False  # the model finished without a word: nothing was stored (B-A)
    language: str = "en"
    # hand-over of the last closed segment and recalled older ones (continuous conversation)
    segment_block: str = ""
    # the answer list put another node first than usual (model_resolver.py): who it replaces
    chain_from: str | None = None
    chain_state: str | None = None
    # the person (or the picker's memory of their last choice) named the model or the node
    picked: bool = False
    # the node chosen before the prompt is built (SCH-03 step 2)
    placement: Any = None
    # the application (workspace) of the conversation: the notes this answer may use
    application: str = "chat"
    # E7-01 (D56): what the hub does for this answer, in order; streamed as ``activity``
    # events and stored with the answer
    activity: ActivityLog = field(default_factory=ActivityLog)
    # the hub started this answer (a finding of the second look), the user did not ask for it
    from_hub: bool = False
    # a thorough research started as a job: its watcher writes the progress and the end, so the
    # run must not settle the message (it would show "error" in between)
    research_job: bool = False
    # the question a "ja bitte" accepted (the offer first, the request before it as context)
    accepted_question: str = ""
    # a "ja bitte" to an offer to explain: answered without a check, told what the yes agreed to
    accepted_hint: str = ""
    # where the conversation was started (a desktop window, conversation_context.py)
    context: dict[str, Any] | None = None
    # the conversation as a check's task reads it (task_pairs): answers without report cards
    task_history: list[tuple[str, str]] = field(default_factory=list)
    # D125: the history with the images where they stand (only when a model that sees answers),
    # the bytes of the images that go to it by id, and what happened to them (``meta.vision``)
    vision_history: list[tuple[str, str]] | None = None
    image_data: dict[str, tuple[str, bytes]] = field(default_factory=dict)
    vision: dict[str, Any] | None = None
    vision_chunk: str = ""  # the activity note on it, sent right after ``meta``

    @property
    def last_user_text(self) -> str:
        if self.accepted_question:
            return self.accepted_question
        return next((c for r, c in reversed(self.history) if r == "user"), "")


async def _prepare(
    request: Request,
    user: CurrentUser,
    conversation_id: uuid.UUID,
    content: str | None,
    *,
    continue_last: bool = False,
    hidden: str | None = None,
    images: list[chat_images.Encoded] | None = None,
) -> Turn:
    """Store the user's message (or drop the last answer to regenerate it, or pick up the
    truncated one to continue it) and collect what the model needs. ``hidden``: the hub speaks up
    (:func:`speak_up`) -- a note for the model that is not stored and not shown as a message of the
    user. Raises HTTPException for unknown conversations / nothing to answer."""
    started = time.monotonic()
    async with request.app.state.sessionmaker() as db:
        store = ChatStore(db, user.uuid)
        conv = await store.get_conversation(conversation_id)
        if conv is None:
            raise _http(404, "not_found", "unknown conversation")
        messages = await store.messages(conversation_id)
        new_user_message = None
        continues: Message | None = None
        if continue_last:
            if (
                not messages
                or messages[-1].role != "assistant"
                or messages[-1].status not in ("truncated", "interrupted")
            ):
                raise _http(409, "nothing_to_continue", "the last answer is not truncated")
            continues = messages[-1]
        elif content is not None:
            if not messages and not conv.title:
                await store.update_conversation(conversation_id, title=title_from(content))
            msg = await store.add_message(conversation_id, "user", content)
            added = (
                await chat_images.ChatImageStore(db, user.uuid).add(conversation_id, msg.id, images)
                if images
                else []
            )
            messages.append(msg)
            new_user_message = message_view(
                msg, [chat_images.image_view(r.id, r.width, r.height) for r in added]
            )
        elif hidden is not None:
            if not messages:
                raise _http(409, "nothing_to_answer", "the conversation is empty")
        else:  # regenerate: everything the last question got — bubbles, progress line, report card
            while messages and messages[-1].role == "assistant":
                await store.delete_message(conversation_id, messages[-1].id)
                messages.pop()
            if not messages or messages[-1].role != "user":
                raise _http(409, "nothing_to_answer", "the conversation has no message to answer")
        pictures = await chat_images.ChatImageStore(db, user.uuid).of_messages(
            [m.id for m in messages if m.role == "user"]
        )
        chain_ids = await _chain_ids(store, conv)
        d = await _defaults(db, request, store)
        account = await db.get(User, user.uuid)
        language = (account.language if account else None) or "en"
        # the application's tools are the most this conversation may use (app_scope.py)
        limit = await app_scope.ceiling(db, user, conv.application)
        await db.commit()
    scope = app_scope.cap(tool_groups.resolve_scope(conv.tool_scope, d["user"]["internet"]), limit)
    level = quality.level_of(conv.quality or d["user"]["quality"])
    chosen = quality.models_for(level, conv.model, d["effective_model"])
    model = chosen["answer"]
    policy = await chat_policy.load(request.app.state.sessionmaker, level)
    requested = f"{model}@{conv.node}" if conv.node else model
    chain_from = chain_state = None
    if conv.model is None and conv.node is None and not continue_last:
        # no pick of the person: the answer role of this application (model_resolver.py); with
        # no list ordered, or none of its nodes able to answer now, it stays the default model
        choice = await request.app.state.gateway.resolver.resolve(
            "answer", application=conv.application, fallback=model, if_none="default"
        )
        model, requested = choice.model, choice.requested
        chain_from, chain_state = choice.fallback_from, choice.fallback_state
    if policy.node and "@" not in requested:  # placement by the policy, unless chosen
        requested = f"{model}@{policy.node}"
    segment_block = ""
    if request.app.state.settings.main_thread_enabled and not continue_last:
        segments = request.app.state.segments
        plan = await segments.plan(
            user.uuid,
            conversation_id,
            messages,
            next((m.content for m in reversed(messages) if m.role == "user"), ""),
            SEGMENT_BLOCK_TOKENS,
            parse_breaks(conv.segment_breaks),
            chain_ids,
        )
        segment_block = plan.block  # the running summary of the chat: never waited for
        messages = plan.messages  # only the current segment is replayed verbatim
    # D125: the images of the replayed messages; a model that cannot see reads that one was there
    shown = {m.id: pictures[m.id] for m in messages if m.role == "user" and m.id in pictures}
    history = history_pairs(
        messages,
        keep_last=continue_last,
        extra={k: chat_images.hidden(len(v)) for k, v in shown.items()},
    )
    vision_history: list[tuple[str, str]] | None = None
    image_data: dict[str, tuple[str, bytes]] = {}
    vision: dict[str, Any] | None = None
    if shown and not continue_last:
        sight = await chat_images.sight(
            request.app.state.gateway, requested, picked=bool(conv.model or conv.node)
        )
        if sight.sees:
            model, requested = sight.model, sight.requested
            newest_first = [
                (i["id"], i["bytes"]) for k in sorted(shown, reverse=True) for i in shown[k]
            ]
            async with request.app.state.sessionmaker() as db:
                image_data = await chat_images.ChatImageStore(db, user.uuid).data(
                    chat_images.pick_for_prompt(newest_first)
                )
            vision_history = history_pairs(
                messages,
                extra={k: chat_images.marked([i["id"] for i in v]) for k, v in shown.items()},
            )
        vision = {
            "sees": sight.sees,
            "model": sight.model,
            "images": len(image_data),
            **({"routed_from": sight.routed_from} if sight.routed_from else {}),
            **({"node": sight.node} if sight.node else {}),
        }
    for_task = task_pairs(messages)
    if hidden is not None:
        history.append(("user", hidden))
        for_task.append(("user", hidden))
    last_answer = next((m for m in reversed(messages) if m.role == "assistant"), None)
    last_refs = tuple(
        uuid.UUID(r["id"])
        for r in (last_answer.memory_refs or [] if last_answer else [])
        if isinstance(r, dict) and isinstance(r.get("id"), str)
    )
    care_asked = any(
        isinstance(r, dict) and r.get("ask")
        for m in messages
        if m.role == "assistant"
        for r in (m.memory_refs or [])
    )
    memory_on = tool_groups.memory_on(scope, d["user"]["memory_enabled"])
    second_look = policy.wants_second_look(level)
    verify_model = None
    if second_look:
        verify_model = model
    turn = Turn(
        conversation_id=conversation_id,
        requested=requested,
        model=model,
        history=history,
        user_prompt=d["user"]["system_prompt"],
        address=d["user"]["address"],
        capabilities=_capabilities(request, memory_on),
        memory_enabled=memory_on,
        temperature=d["user"]["temperature"],
        new_user_message=new_user_message,
        started=started,
        continues=continues,
        last_refs=last_refs,
        care_asked=care_asked,
        quality=level,
        verify_model=verify_model,
        policy=policy,
        second_look=second_look,
        scope=scope,
        internet=scope["internet"],
        language=language,
        segment_block=segment_block,
        chain_from=chain_from,
        chain_state=chain_state,
        picked=bool(conv.model or conv.node),
        application=conv.application or "chat",
        in_program=conv.application is not None,
        context=conv.context,
        task_history=for_task,
        vision_history=vision_history,
        image_data=image_data,
        vision=vision,
    )
    if vision is not None and (not vision["sees"] or vision.get("routed_from")):
        # the chat says what happened to the images: another model looked, or none could
        event = "vision_routed" if vision["sees"] else "vision_blind"
        where = f"{vision['model']} @ {vision['node']}" if vision.get("node") else vision["model"]
        _, turn.vision_chunk = turn.activity.add(
            "note", where, status="info", data={"event": event}
        )
    return turn


def _capabilities(_request: Request, memory_enabled: bool) -> chat.Capabilities:
    """What the assistant can do in a chat right now: text, and its notes about the user if
    memory is on for this user. Later stages (knowledge and web search, tools) switch their
    entries on here, per user."""
    return chat.Capabilities(memory=memory_enabled)


async def _store_answer(
    request: Request, user: CurrentUser, turn: Turn, content: str, **meta: Any
) -> dict[str, Any]:
    """Store a finished, stopped or broken answer; a continued answer is appended to the
    message it continues (one answer for the user, sums for tokens and time)."""
    content = strip_fake_card(content)
    if turn.continues is None:  # a continued answer is judged once, as the whole it becomes
        verdict = answer_guard.review(
            content,
            checked=turn.checked,
            language=chat.hub_language(turn.language, turn.last_user_text),
        )
        content = verdict.text
    async with request.app.state.sessionmaker() as db:
        store = ChatStore(db, user.uuid)
        prev = turn.continues
        # the system prompt this answer was sent with is kept (once per hash) and named on it
        prompt_text = meta.pop("prompt_text", None)
        core_version = meta.pop("core_version", None)
        if prev is None and prompt_text:
            meta["prompt_hash"] = await store.save_snapshot(
                prompt_text, meta.get("persona_ref"), core_version
            )
        if prev is None:
            msg = None
            if turn.message_id is not None:  # the placeholder of this run gets the answer
                msg = await store.update_message(
                    turn.conversation_id, turn.message_id, content=content, **meta
                )
            if msg is None:
                msg = await store.add_message(turn.conversation_id, "assistant", content, **meta)
        else:
            msg = await store.update_message(
                turn.conversation_id,
                prev.id,
                content=prev.content + content,
                status=meta["status"],
                error=meta["error"],
                prompt_tokens=meta["prompt_tokens"],
                completion_tokens=(prev.completion_tokens or 0) + (meta["completion_tokens"] or 0),
                duration_ms=(prev.duration_ms or 0) + (meta["duration_ms"] or 0),
                context_trimmed=max(prev.context_trimmed, meta["context_trimmed"]),
            )
            if msg is None:  # deleted meanwhile
                meta.pop("prompt_hash", None)
                msg = await store.add_message(turn.conversation_id, "assistant", content, **meta)
        await db.commit()
        return message_view(msg)


def _meta_event(
    turn: Turn,
    node: str,
    fallback_from: str | None,
    fallback_state: str | None,
    context_length: int,
    estimated: int,
    trimmed: int,
    recalled: list[Recalled],
    checking: str | None = None,
    check_max_calls: int | None = None,
) -> str:
    """The ``meta`` event that opens an answer: who answers, from where, with what. ``checking``
    (``forced`` | ``detected``) says that a server check runs before the answer;
    ``check_max_calls`` that it is held to that many tool calls (a slow stand-in machine)."""
    return (
        chat.sse(
            "meta",
            {
                "conversation_id": str(turn.conversation_id),
                "user_message": turn.new_user_message,
                "continues": turn.continues.id if turn.continues else None,
                "memory": [n.view() for n in recalled],
                "message_id": turn.message_id,
                "model": turn.model,
                "node": node,
                "fallback_from": fallback_from,
                "fallback_state": fallback_state,
                "context": {
                    "length": context_length,
                    "estimated_prompt_tokens": estimated,
                    "trimmed": trimmed,
                },
                **({"checking": checking} if checking else {}),
                **({"check_max_calls": check_max_calls} if check_max_calls else {}),
                **({"check_offer": True} if turn.check_offer else {}),
                **({"tool_scope": turn.scope} if tool_groups.restricts_servers(turn.scope) else {}),
                "internet": turn.internet,
                **({"internet_node": turn.internet_node} if turn.internet_node else {}),
                "quality": turn.quality,
                **({"verify_model": turn.verify_model} if turn.verify_model else {}),
                **({"vision": turn.vision} if turn.vision else {}),
            },
        )
        + turn.vision_chunk
    )


async def _answer(
    request: Request,
    user: CurrentUser,
    turn: Turn,
    upstream: Upstream,
    prompt: chat.Prompt,
    context_length: int,
    recalled: list[Recalled],
    *,
    send_meta: bool = True,
    agent_task_id: uuid.UUID | None = None,
    on_tool: Callable[[str, str], AsyncIterator[str]] | None = None,
    after_done: Callable[[dict[str, Any]], AsyncIterator[str]] | None = None,
    closing: bool = False,
    scope: tuple[str, list[str], list[str]] | None = None,
    change_facts: tuple[int, list[dict[str, Any]], tuple[bool, str] | None] | None = None,
    prepared_sets: list[Any] | None = None,
    refused: str = "",
    held: "Held | None" = None,
) -> AsyncIterator[str]:
    """SSE for the browser: ``meta``, ``delta``…, ``done`` | ``error``. The answer is stored
    however the stream ends (complete, stopped by the user, or broken).

    With ``on_tool`` the model was offered ``look_at_my_systems``: if it calls it, nothing of this
    stream is the answer — the browser is told to drop what was shown (``discard``), the check
    runs (``on_tool(what)``) and the answer follows from it.

    An answer based on a server check (``agent_task_id``) keeps its ``[live]`` / ``[doc]`` /
    ``[assumption]`` tags, learns nothing about the user from it (the facts are the servers')
    and sends no second ``meta`` (the check sent it)."""
    started = turn.started
    first_token_ms: int | None = None
    # when the first token of any kind arrived (a thought counts): the completion tokens of the
    # node include the thinking, so the speed is measured from here and not from the first
    # visible token
    first_generated: float | None = None
    parts: list[str] = []
    markers: Any = chat.PassthroughFilter() if agent_task_id else chat.MarkerFilter()
    reader = chat.SseReader()
    usage: dict[str, int | None] = {"prompt": None, "completion": None}
    finished = False
    finish_reason: str | None = None
    error: str | None = None
    stored = False
    tool_args, tool_called, tool_name = "", False, ""

    def meta(status: str, err: str | None = None) -> dict[str, Any]:
        text = "".join(parts)
        turn.activity.settle()  # whatever still shows as running did not finish
        return {
            "model": turn.model,
            "node": upstream.node,
            "fallback_from": upstream.fallback_from,
            "status": status,
            "error": err,
            "prompt_tokens": usage["prompt"] or prompt.estimated_tokens,
            "completion_tokens": usage["completion"] or chat.estimate_tokens(text),
            "first_token_ms": first_token_ms,
            "duration_ms": int((time.monotonic() - started) * 1000),
            "context_trimmed": prompt.trimmed,
            "prompt_text": system_text,
            "persona_ref": turn.persona_ref,
            "core_version": turn.core_stamp,
            **({"memory_refs": [n.view() for n in recalled]} if recalled else {}),
            **({"agent_task_id": agent_task_id} if agent_task_id else {}),
            **({"activity": turn.activity.snapshot()} if len(turn.activity) else {}),
        }

    system_text = prompt.messages[0]["content"] if prompt.messages else ""
    splitter = chat.ThinkSplitter()
    # a research answer shows neither its thinking nor its steps: one line says it is in progress
    thinking = ThinkingTrace(
        _SilentLog() if turn.application == research_profile.APPLICATION else turn.activity
    )
    request.app.state.memory.activity.begin()
    if send_meta:
        yield _meta_event(
            turn,
            upstream.node,
            upstream.fallback_from,
            upstream.fallback_state,
            context_length,
            prompt.estimated_tokens,
            prompt.trimmed,
            recalled,
        )
    try:
        # A node that pauses for its user (E2-05 B) says so while the answer stands still.
        body = upstream.status_body() if upstream.status_body else upstream.body
        async for data in body:
            if isinstance(data, StreamStatus):
                yield chat.sse(
                    "status",
                    {"state": data.state, "reasons": list(data.reasons), "node": upstream.node},
                )
                continue
            for c in reader.feed(data):
                if c.error:
                    error, finished = c.error, True
                    break
                if c.prompt_tokens is not None:
                    usage["prompt"] = c.prompt_tokens
                if c.completion_tokens is not None:
                    usage["completion"] = c.completion_tokens
                if first_generated is None and (c.text or c.reasoning or c.tool_call):
                    first_generated = time.monotonic()
                if c.tool_call and (on_tool is not None or turn.handover_targets):
                    tool_called, tool_args = True, tool_args + c.tool_args
                    tool_name = tool_name or c.tool_name
                    for chunk in thinking.end():  # the model has made up its mind
                        yield chunk
                if c.reasoning and not tool_called:
                    for chunk in thinking.feed(c.reasoning):
                        yield chunk
                for kind, piece in splitter.feed(c.text) if c.text and not tool_called else ():
                    if kind == "think":  # a <think> block in the text is a thought, not the answer
                        for chunk in thinking.feed(piece):
                            yield chunk
                        continue
                    for chunk in thinking.end():  # the answer proper has begun
                        yield chunk
                    if first_token_ms is None:
                        first_token_ms = int((time.monotonic() - started) * 1000)
                    shown = markers.feed(piece)
                    if shown:
                        parts.append(shown)
                        yield chat.sse("delta", {"text": shown})
                if c.finish_reason:
                    finish_reason = c.finish_reason
                finished = finished or c.finished or c.done
            if error:
                break
        # a check that cannot answer: the question is about projects, this conversation has no
        # project tools, another program has them (live 2026-11-14: Chat read notes and wiki for
        # "the tasks of project X", repeated itself and said it does not know)
        if (
            tool_called
            and tool_name == chat.DECIDER_FUNCTION
            and not error
            and (owner := project_owner(turn)) is not None
        ):
            tool_name = chat.HANDOVER_FUNCTION
            tool_args = json.dumps({"to": owner, "request": turn.last_user_text})
        if tool_called and tool_name == chat.HANDOVER_FUNCTION and not error:
            # the model proposes a hand-over: a dossier is prepared for the person, nothing sent
            upstream.close()
            for chunk in thinking.end():
                yield chunk
            if parts:
                yield chat.sse("discard", {})
                parts.clear()
            text, proposed = await _propose_handover(request, user, turn, tool_args)
            parts.append(text)
            yield chat.sse("delta", {"text": text})
            message = await _store_answer(request, user, turn, text, **meta("complete"))
            stored = True
            if proposed is not None:
                yield chat.sse("handover", proposed)
            yield chat.sse("done", {"message": message})
            return
        if tool_called and on_tool is not None and not error:
            upstream.close()
            for chunk in thinking.end():
                yield chunk
            if parts:
                # the lead-in the model said before it called is not the answer, but it is not lost
                # either: it stays in the activity of this answer (E7-01)
                _, chunk = turn.activity.add("lead_in", status="info", detail="".join(parts))
                if chunk:
                    yield chunk
                yield chat.sse("discard", {})
            stored = True  # nothing of this stream is stored: the answer comes after the check
            async for ev in on_tool(tool_name or "", tool_args):
                yield ev
            return
        for kind, piece in splitter.flush():  # a tag the stream ended in the middle of
            if kind == "think":
                for chunk in thinking.feed(piece):
                    yield chunk
                continue
            shown = markers.feed(piece)
            if shown:
                parts.append(shown)
                yield chat.sse("delta", {"text": shown})
        for chunk in thinking.end():
            yield chunk
        tail = markers.flush()
        if tail:
            parts.append(tail)
            yield chat.sse("delta", {"text": tail})
        text = "".join(parts)
        if (
            not finished
            and not error
            and text.strip()
            and upstream.ended.get("code") == "interrupted"
        ):
            # The node unloaded the model during the answer (a long pause for its user, memory
            # another program needed): what was said is valid, "Continue" carries on (D21/D24).
            message = await _store_answer(request, user, turn, text, **meta("truncated"))
            stored = True
            yield chat.sse("status", {"state": "interrupted", "node": upstream.node})
            yield chat.sse("done", {"message": message})
        elif error or not finished:
            error = error or "the connection to the node was lost during the answer"
            code = "stream_broken"
            if is_context_full(error):
                # the window of the model is used up (by other requests on it, most likely)
                code, error = "context_full", CONTEXT_FULL_MESSAGE
            message = await _store_answer(request, user, turn, text, **meta("error", error))
            stored = True
            yield chat.sse(
                "error",
                {
                    "code": code,
                    "message": error,
                    "message_row": message,
                    "retry": code == "context_full",
                },
            )
        else:
            # "length": the model ran into the end of its context (or max tokens): the answer
            # is cut off, not finished.
            status = "truncated" if finish_reason == "length" else "complete"
            if scope is not None:  # "does not exist" only after every node was asked (E3-07 D4)
                text = chat.guard_scope(text, *scope)
            if closing:  # under a report card an answer cut off by the cap still counts as written
                status = "complete"
            if status == "complete" and text.strip():
                # B-C: a back-reference nothing in the conversation backs, a path no result shows
                sources = "\n".join(str(m.get("content") or "") for m in prompt.messages)
                visible = "\n".join(str(m.get("content") or "") for m in prompt.messages[1:])
                language = hub_language(turn.language, turn.last_user_text)
                note = chat.source_notes(
                    language,
                    chat.unsupported_backrefs(text, visible),
                    chat.unsupported_paths(text, sources) if agent_task_id else [],
                )
                if note:
                    log.warning(
                        "chat: answer with a statement that has no source: %s", note.strip()
                    )
                    text += note
                    yield chat.sse("delta", {"text": note})
                if refused and (refusal := claims.refused_note(language, refused)):
                    text += refusal  # AGT-01: no check was made, and why
                    yield chat.sse("delta", {"text": refusal})
                if change_facts is not None:  # AGT-01: a change is claimed only if the hub has it
                    claim_note = claims.note(language, text, *change_facts)
                    if claim_note:
                        log.warning(
                            "chat: a claimed change the record does not back: %s",
                            claim_note.strip(),
                        )
                        text += claim_note
                        yield chat.sse("delta", {"text": claim_note})
                if prepared_sets and (
                    listed := project_changesets.listing(prepared_sets, language)
                ):
                    text += listed  # what the click would write, not the model's retelling of it
                    yield chat.sse("delta", {"text": listed})
                # the offer the person already said yes to is not made again (chat_loop.py)
                earlier = [c for r, c in turn.history[:-1] if r == "assistant"]
                trimmed = chat_loop.without_repeated_offer(text, earlier)
                if trimmed != text and trimmed.strip():
                    log.info("chat: a repeated offer was cut from the answer")
                    yield chat.sse("discard", {})
                    yield chat.sse("delta", {"text": trimmed})
                    text = trimmed
            if (closing and chat.only_tool_call_text(text)) or starts_with_fake_card(text):
                # a call written as text, or a card and claims the model made up: no answer
                yield chat.sse("discard", {})
                text = ""
            elif closing and (clean := chat.strip_tool_call_text(text)) != text:
                yield chat.sse("discard", {})  # prose plus a call written as text: keep the prose
                yield chat.sse("delta", {"text": clean})
                text = clean
            if not text.strip() and status == "complete" and turn.continues is None:
                # a finished answer without a word is never stored or shown (B-A): a closing
                # bubble is asked for again by the caller, any other answer is an error
                stored = True
                turn.empty_answer = True
                log.warning("chat: %s on %s finished without any text", turn.model, upstream.node)
                if turn.message_id is not None:
                    await _drop_message(request, user, turn, turn.message_id)
                    turn.message_id = None
                if not closing:
                    yield _sse_error("empty_answer", "the model finished without any text")
                return
            if status == "complete" and agent_task_id is None and turn.continues is None:
                # E7-02 (D57): an answer that ends on "I will look at that now" and used no tool
                # is not a finished answer; it is shown as what it is, with the sentence
                promised = announced_action(text)
                if promised is not None:
                    log.info("chat: answer ends on an announcement: %s", promised)
                    _, chunk = turn.activity.add("announce", status="failed", detail=promised)
                    if chunk:
                        yield chunk
            if held is not None and status == "complete":
                # Gründlich (PLAN 2a): the check and any revision come first; the final text is
                # shown and stored here, the stream above has held its text back until then
                async for ev in _finalize_held(request, user, turn, held, text, meta(status)):
                    yield ev
                text = held.text
                message = held.message
            else:
                if held is not None and not held.released:
                    # an answer that did not finish has nothing to check: its text is shown as it is
                    held.released = True
                    yield chat.sse("delta", {"text": text})
                message = await _store_answer(request, user, turn, text, **meta(status))
            stored = True
            log.info(
                "chat answer: model=%s node=%s prompt_tokens=%s trimmed=%s first_token_ms=%s "
                "duration_ms=%s checked=%s",
                turn.model,
                upstream.node,
                usage["prompt"] or prompt.estimated_tokens,
                prompt.trimmed,
                first_token_ms,
                int((time.monotonic() - started) * 1000),
                agent_task_id is not None,
            )
            if usage["completion"] and first_generated is not None and status == "complete":
                # a real answer teaches the node choice how fast this node is with this model
                gen_seconds = time.monotonic() - first_generated
                spawn(
                    request.app.state.gateway.note_speed(
                        upstream.node, turn.model, usage["completion"], gen_seconds
                    )
                )
            yield chat.sse("done", {"message": message})
            if after_done is not None and status == "complete":
                async for ev in after_done(message):
                    yield ev
            if turn.memory_enabled and turn.policy.write_memory and turn.continues is None:
                note = await _remember(request, user, turn, message["id"], text, recalled)
                if note is not None:
                    yield chat.sse("memory", note)
                # the running summary follows this answer (a new chat reads its key terms), and
                # when the chat rests it is checked by a second model
                request.app.state.digester.after_answer(user.uuid, turn.conversation_id)
                # a second model reads this answer now; what it finds reaches the next one
                # (unless the chat policy says otherwise at this effort)
                if turn.policy.wants_review(True):
                    request.app.state.reviewer.after_answer(
                        user.uuid,
                        turn.conversation_id,
                        message["id"],
                        turn.model,
                        upstream.node,
                        may_speak=not turn.from_hub,
                    )
            elif turn.continues is None and (
                explicit_request(turn.last_user_text) is not None or chat.claims_noted(text)
            ):
                # memory is off for this user: nothing is noted, and the user is told so
                yield chat.sse("memory", _not_noted(message["id"], "memory_off"))
    except (asyncio.CancelledError, GeneratorExit):
        # The run was stopped (the user's "stop" call) or the hub goes down: keep what was said.
        # The answer is generated by a task of the hub, not by the request, so this can await.
        if not stored and parts:
            await _store_stopped(
                request,
                user,
                turn,
                "".join(parts),
                meta("interrupted" if turn.interrupted else "stopped"),
            )
        raise
    finally:
        request.app.state.memory.activity.end()
        upstream.close()


async def _second_look_block(request: Request, user: CurrentUser, turn: Turn) -> str:
    """What the second model found in earlier answers of this chat, once, as context for the
    answer that starts now ("" if nothing is pending or the chat does not use memory)."""
    if not turn.memory_enabled or turn.continues is not None:
        return ""
    try:
        async with request.app.state.sessionmaker() as db:
            store = ReviewStore(db, user.uuid)
            pending = await store.pending_for_agent(turn.conversation_id)
            if not pending:
                return ""
            findings = [f for r in pending for f in r.findings]
            await store.mark_told([r.id for r in pending])
            await db.commit()
        return chat.review_context(findings)
    except Exception:
        log.exception("chat: could not read the findings of the second look")
        return ""


async def _remember(
    request: Request,
    user: CurrentUser,
    turn: Turn,
    message_id: int,
    answer: str,
    existing: list[Recalled],
) -> dict[str, Any] | None:
    """Extract notes from the exchange (a background task that survives a closed browser) and
    wait a few seconds for the result to show "remembered: …" under the answer."""
    memory = request.app.state.memory
    asked = explicit_request(turn.last_user_text) is not None
    claimed = chat.claims_noted(answer)
    future = memory.extract_in_background(
        user.uuid,
        turn.conversation_id,
        message_id,
        turn.last_user_text,
        answer,
        turn.model,
        existing,
    )
    # a user who asked to note something waits for the result: "Gemerkt: …" or why not (E3-07 D4)
    wait = request.app.state.settings.memory_note_wait_seconds * (4 if asked else 1)
    try:
        result = await asyncio.wait_for(asyncio.shield(future), wait)
    except TimeoutError:
        return _not_noted(message_id, "pending") if asked or claimed else None
    except Exception:
        return _not_noted(message_id, "failed") if asked or claimed else None
    why = result.not_noted
    if why is None and (asked or claimed) and not result.stored_something and not result.retracted:
        why = "secret" if result.blocked else "nothing_found"
    if not (
        result.remembered
        or result.merged
        or result.corrected
        or result.retracted
        or result.blocked
        or result.questions
        or result.proposed
        or why
    ):
        return None
    return {
        "message_id": message_id,
        "remembered": result.remembered,
        "confirmed": result.merged,
        "corrected": result.corrected,
        "retracted": result.retracted,
        "blocked": result.blocked,
        "questions": result.questions,
        "proposed": result.proposed,
        **({"not_noted": why} if why else {}),
    }


async def _where(
    request: Request, user: CurrentUser, turn: Turn, route: Any, context_length: int
) -> str:
    """The "where do you run" line of this answer (AGT-04): the node the route puts first, its
    hardware in one phrase for an admin (who sees the machines anyway), and whether the program may
    propose changes. Never raises: an answer does not wait for a nice-to-have. A context of less
    than 2048 tokens gets no line at all, one below the compact limit the shortest."""
    if context_length < 2048:
        return ""
    try:
        node = route.candidates[0].name if route.candidates else None
        hardware = await _hardware_line(request, node) if node and user.is_admin else ""
        return chat.where_block(
            turn.application,
            turn.model,
            node,
            hardware,
            can_change=bool(turn.capabilities.tools),
            compact=context_length < chat.COMPACT_CONTEXT_TOKENS,
        )
    except Exception:
        log.exception("chat: could not build the line about where the answer runs")
        return ""


async def _hardware_line(request: Request, node: str) -> str:
    """ "AMD Radeon RX 9070 XT 16 GB, 32 GB RAM" for a node, from its inventory; "" if unknown."""
    async with request.app.state.sessionmaker() as db:
        row = (
            await db.execute(
                select(Node, NodeInventory)
                .outerjoin(NodeInventory, NodeInventory.node_id == Node.id)
                .where(Node.name == node)
            )
        ).first()
    if row is None:
        return ""
    view = node_summary(row[0], row[1], True)
    parts = []
    for g in view["gpus"][:2]:
        name, vram = g.get("name"), g.get("vram_total_mb")
        if name:
            parts.append(f"{name} {round(vram / 1024)} GB" if vram else str(name))
    if view["ram_total_mb"]:
        parts.append(f"{round(view['ram_total_mb'] / 1024)} GB RAM")
    return ", ".join(parts)


async def _prepared_changes(
    sessionmaker: Any, hub_timezone: str, user_id: uuid.UUID, task_id: uuid.UUID
) -> tuple[list[Any], list[Any]]:
    """The single changes the check prepared (for the card), and everything that still waits for
    the person's click: those and the change sets of the same task (AGT-01). A claim of a change
    is held against the second list; counting only single changes made the hub deny a change set
    that was on record."""
    singles = await ChangeStore(sessionmaker).for_task(user_id, task_id)
    sets = await ChangeSetStore(sessionmaker, hub_timezone).for_task(user_id, task_id)
    return singles, [row for row in (*singles, *sets) if row.status == "pending"]


async def _task_proposals(
    state: Any, user_id: uuid.UUID, task_id: uuid.UUID
) -> list[dict[str, Any]]:
    """The proposals the check of this answer made (file changes of the workshop, D99); never
    more than a list, a failure to read them leaves the answer as it is."""
    service = getattr(state, "proposals", None)
    if service is None:
        return []
    try:
        return list(await service.for_task(user_id, task_id))
    except Exception:
        log.exception("chat: could not read the proposals of the check")
        return []


def _change_facts(
    prepared: list[Any], task: Any
) -> tuple[int, list[dict[str, Any]], tuple[bool, str] | None]:
    """What the hub has on record for an answer after a check: how many changes wait for the
    person's click and which calls were refused, so a claim of a change in the answer can be
    held against it (AGT-01). Prepared change sets and pending proposals of file changes count (both
    have their buttons under the report card); the advice on the card (``advice_only``) has no
    button and nothing to apply."""
    waiting = len(prepared)
    failed = list(((task.diagnosis or {}).get("failed_calls") if task is not None else None) or [])
    sets = [p for p in prepared if isinstance(p, ProjectChangeSet)]
    texts = (
        (
            any(project_changesets.changes_text(s) for s in sets),
            "; ".join(project_changesets.summary_of(s) for s in sets),
        )
        if sets
        else None
    )
    return waiting, failed, texts


def _scope_guard(turn: Turn, task: Any) -> tuple[str, list[str], list[str]] | None:
    """The language, the nodes that were asked and the ones that were not, for an answer after a
    check that did not ask every node it could have asked; ``None`` when it did."""
    coverage = ((task.diagnosis or {}).get("coverage") if task is not None else None) or {}
    not_asked = list(coverage.get("not_asked") or [])
    if not not_asked:
        return None
    language = chat.hub_language(turn.language, turn.last_user_text)
    return language, list(coverage.get("asked") or []), not_asked


def _not_noted(message_id: int, reason: str) -> dict[str, Any]:
    """The ``memory`` event of an exchange in which nothing was noted although the user asked for it
    (or the answer claimed it): the reason the GUI states as "Nicht gemerkt, weil …"."""
    return {
        "message_id": message_id,
        "remembered": [],
        "confirmed": [],
        "corrected": [],
        "retracted": [],
        "blocked": 0,
        "questions": 0,
        "proposed": 0,
        "not_noted": reason,
    }


async def _store_stopped(
    request: Request, user: CurrentUser, turn: Turn, content: str, meta: dict[str, Any]
) -> None:
    try:
        await _store_answer(request, user, turn, content, **meta)
    except Exception:
        log.exception("could not store the stopped answer")


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
    turn = await _prepare(
        request, user, conversation_id, content, continue_last=continue_last, images=images
    )
    # The answer is made by a task of the hub from here on, the preparation included (E3-07): a
    # reader that goes away — or presses stop while the model still reads the prompt — changes
    # nothing about it. Before the first byte a failure is still an HTTP error, as it always was.
    outcome: asyncio.Future[Response | None] = asyncio.get_running_loop().create_future()
    run = await _launch(request, user, turn, _pipeline(request, user, turn, tools, outcome))
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
        turn = await _prepare(request, user, conversation_id, None, hidden=HUB_NOTICE)
        turn.from_hub = True
        outcome: asyncio.Future[Response | None] = asyncio.get_running_loop().create_future()
        run = await _launch(request, user, turn, _pipeline(request, user, turn, tools, outcome))
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


# A thorough research as a job of its own (research_chat.py): the chat starts it and goes on with
# the person; a watch writes the progress into the message; the end brings the chat's summary.
WATCH_SECONDS = 3.0
WATCH_MAX_SECONDS = 4 * 3600.0
_WATCHERS: set[asyncio.Future[Any]] = set()  # held, so that the garbage collector keeps a watch


def _is_thorough(turn: Turn) -> bool:
    return research_profile.CHAT_DEPTH.get(quality.level_of(turn.quality), "default") == "thorough"


def research_mode(application: str | None, level: str | None, from_hub: bool) -> str | None:
    """What a check in the Research application becomes (the maintainer, 2026-11-13): at
    "thorough" a research of its own at once ("job"), at "balanced" the offer to start one
    ("offer": a plain check of many web searches filled the model's context and found nothing,
    live 2026-11-12), at "fast" a short check as before (None). Elsewhere always None."""
    if application != research_profile.APPLICATION or from_hub:
        return None
    lvl = quality.level_of(level)
    if lvl == "thorough":
        return "job"
    if lvl == "balanced":
        return "offer"
    return None


_RESEARCH_OFFER = {
    "de": (
        "Dafür lohnt sich eine richtige Recherche: Ich suche im Web und in deinen Notizen, prüfe "
        "meinen Entwurf und lege einen Bericht in die Ablage. Das dauert einige Minuten; den "
        "Fortschritt siehst du hier. Für eine schnelle Antwort ohne Recherche stelle die Qualität "
        "auf „Schnell“."
    ),
    "en": (
        "This deserves a proper research: I search the web and your notes, check my draft and "
        "file a report in the shelf. It takes a few minutes; you see the progress here. For a "
        'quick answer without a research, set the quality to "fast".'
    ),
}


WRITE_OUTPUT_TOKENS_ONE_CALL = 6144


async def _write_target(
    request: Request, user: CurrentUser, turn: Turn
) -> tuple[Any, list[Any], list[str]] | None:
    """The project and the tasks a "rewrite these tasks" request is about, or None (then the agent
    loop takes it): the message must ask to write, name a project of the person (here or earlier
    in the conversation) and name tasks by number (here, or for "überarbeite das nochmal" in the
    conversation before)."""
    if not project_write.wants_writing(turn.last_user_text):
        return None
    texts = [turn.last_user_text, *(c for _, c in reversed(turn.task_history[:-1]))][:8]
    store = ProjectStore(request.app.state.sessionmaker)
    project = project_write.pick_project(await store.list_projects(user.uuid), texts)
    if project is None:
        return None
    wanted: list[str] = []
    for text in texts:  # the newest text that names tasks decides
        wanted = project_write.numbers_in(text)
        if wanted:
            break
    if not wanted:
        return None
    nodes = await store.tree(user.uuid, project.id)
    known = {str(n.number) for n in nodes if n.kind == "task"}
    wanted = [w for w in wanted if w in known]
    if not wanted:
        return None
    return project, nodes, wanted


def _issue_request(turn: Turn) -> tuple[str, bool] | None:
    """``(text, file_now)`` when the message files an issue (its prefix, or a first sentence
    that asks for one in words) or, in Projekte, reads
    like a complaint about the system without naming a task (a card offers to file it)."""
    if turn.continues is not None or turn.new_user_message is None:
        return None
    text = turn.last_user_text
    filed = project_issues.filed_text(text)
    if filed is None:
        filed = project_issues.asked_text(text)
    if filed is not None:
        return filed, True
    if (
        turn.application == "project"
        and project_issues.sounds_like_problem(text)
        and not project_write.numbers_in(text)
    ):
        return text.strip(), False
    return None


async def _issue_events(
    request: Request, user: CurrentUser, turn: Turn, text: str, file_now: bool
) -> AsyncIterator[str]:
    """File the issue (or offer it) and answer with the hub's own sentence and a card."""
    lang = hub_language(turn.language, turn.last_user_text)
    yield _meta_event(turn, "hub", None, None, 0, 0, 0, [])
    store = ProjectStore(
        request.app.state.sessionmaker, PERSON, request.app.state.settings.hub_timezone
    )
    project_title = await _issue_project(request)
    card_data: dict[str, Any]
    if file_now:
        try:
            task, alike = await project_issues.file_issue(
                store,
                user.uuid,
                text,
                project_title=project_title,
                application=turn.application,
                conversation=str(turn.conversation_id),
                where="Chat" if not turn.application else f"Chat {turn.application}",
            )
        except ProjectError as exc:
            answer = (
                f"Ich konnte das Issue nicht anlegen: {exc}"
                if lang == "de"
                else f"I could not file the issue: {exc}"
            )
            message = await _store_answer(
                request, user, turn, answer, status="complete", error=None
            )
            yield chat.sse("delta", {"text": answer})
            yield chat.sse("done", {"message": message})
            return
        answer = project_issues.filed_answer(lang, task, project_title, alike)
        card_data = {
            "filed": True,
            "task_id": str(task.id),
            "project_id": str(task.project_id),
            "number": task.number,
            "title": task.title,
            "similar": [{"id": str(n.id), "number": n.number, "title": n.title} for n in alike],
        }
    else:
        answer = (
            "Das klingt nach einem Problem im Betrieb. Soll ich es als Issue anlegen? Ein Klick "
            "auf "
            "der Karte legt es mit dem Kontext (Build, Fenster, Knoten, Hub-Log) an."
            if lang == "de"
            else "That sounds like a problem with the system. Shall I file it as an issue? One "
            "click on the card files it with the context (build, window, nodes, hub log)."
        )
        card_data = {
            "filed": False,
            "text": text[: project_issues.MAX_TEXT],
            "application": turn.application,
            "conversation": str(turn.conversation_id),
        }
    fake = SimpleNamespace(
        id=uuid.uuid4(),
        diagnosis={"summary": answer.split(". ")[0] + "."},
        status="done",
        stop_reason=None,
        tool_calls=0,
    )
    card = report.build(fake, [])
    if card is not None:
        card = {**card, "issue": card_data}
        row = await _new_message(
            request, user, turn, report.text(card), kind="report", report=card, status="complete"
        )
        yield chat.sse("report", {"message": message_view(row)})
    message = await _store_answer(request, user, turn, answer, status="complete", error=None)
    yield chat.sse("delta", {"text": answer})
    yield chat.sse("done", {"message": message})


async def _issue_project(request: Request) -> str:
    """The project issues are filed in (live setting ``issue_project``, default "Topcoat")."""
    value = getattr(request.app.state.settings, "issue_project", "") or ""
    return value.strip() or project_issues.DEFAULT_PROJECT


async def _iteration_target(
    request: Request, user: CurrentUser, turn: Turn
) -> tuple[Any, Any, Any | None] | None:
    """The project, task and version a "make the next version" request is about, or None: the
    message asks for a version or iteration, names a project of the person (here or earlier) and
    a number of a task that has a result, or of such a result."""
    text = turn.last_user_text
    if not project_iterations.wants_iteration(text):
        return None
    texts = [text, *(c for _, c in reversed(turn.task_history[:-1]))][:8]
    store = ProjectStore(request.app.state.sessionmaker)
    project = project_write.pick_project(await store.list_projects(user.uuid), texts)
    if project is None:
        return None
    nodes = await store.tree(user.uuid, project.id)
    for candidate in texts:  # the newest text that names a fitting number decides
        found = project_iterations.iteration_target(nodes, project_write.numbers_in(candidate))
        if found is not None:
            task, base = found
            return project, task, base
    return None


async def _iteration_events(
    request: Request, user: CurrentUser, turn: Turn, project: Any, task: Any, base: Any | None
) -> AsyncIterator[str]:
    """The answer to "make the next version": a card that starts it (the person's click, D15),
    with the message as feedback to trim; no model call, no change to the task."""
    lang = hub_language(turn.language, turn.last_user_text)
    nodes = await ProjectStore(request.app.state.sessionmaker).tree(user.uuid, project.id)
    number = project_iterations.next_number(project_iterations.results_of(nodes, task.id))
    text = project_iterations.offer_text(lang, task, base, number, task.requirements or [])
    fake = SimpleNamespace(
        id=uuid.uuid4(),
        diagnosis={"summary": text.split("\n\n")[0]},
        status="done",
        stop_reason=None,
        tool_calls=0,
    )
    card = report.build(fake, [])
    if card is not None:
        card = {
            **card,
            "iterate": {
                "task_id": str(task.id),
                "task_number": task.number,
                "task_title": task.title,
                "iteration": number,
                "based_on": str(base.id) if base is not None else None,
                "based_on_number": base.number if base is not None else None,
                "feedback": turn.last_user_text[: project_iterations.MAX_FEEDBACK_CHARS],
                "requirements": [r.get("text") for r in task.requirements or []],
            },
        }
        row = await _new_message(
            request, user, turn, report.text(card), kind="report", report=card, status="complete"
        )
        yield chat.sse("report", {"message": message_view(row)})
    message = await _store_answer(request, user, turn, text, status="complete", error=None)
    yield chat.sse("delta", {"text": text})
    yield chat.sse("done", {"message": message})


async def _write_events(
    request: Request,
    user: CurrentUser,
    turn: Turn,
    project: Any,
    nodes: list[Any],
    wanted: list[str],
) -> AsyncIterator[str]:
    """The answer of a write: the confirmation, a progress line, one model call that writes, the
    change set with its card, and the hub's own sentence of what waits for the click."""
    state = request.app.state
    lang = hub_language(turn.language, turn.last_user_text)
    yield chat.sse("bubble", {"message": await _confirm(request, user, turn)})
    line = (
        f"Schreibe die Aufgaben {', '.join(wanted)} …"
        if lang == "de"
        else f"Writing the tasks {', '.join(wanted)} …"
    )
    progress = await _new_message(request, user, turn, line, kind="progress", status="running")
    yield chat.sse("progress", {"message_id": progress.id, "text": line})
    by_number = {str(n.number): n for n in nodes}
    tasks = [by_number[w] for w in wanted]
    others = [n for n in nodes if n.kind == "task" and str(n.number) not in wanted][:40]
    messages = project_write.writer_messages(
        chat_tools.conversation_for_task(turn.task_history),
        tasks,
        turn.last_user_text,
        others,
    )
    payload = {
        "model": turn.requested,
        "messages": messages,
        "stream": False,
        "temperature": 0.4,
        "max_tokens": WRITE_OUTPUT_TOKENS_ONE_CALL,
    }
    draft_id = uuid.uuid4()  # the change set's task id: the card reads the set by it
    plan = project_write.Plan()
    error = ""
    try:
        data = await state.gateway.call(
            turn.requested, "/v1/chat/completions", payload, label="chat"
        )
        written = memory_answer_text(data)[0]
        plan = project_write.build(project_write.parse_sections(written), nodes, wanted)
        if plan.operations:
            await ChangeSetStore(state.sessionmaker, state.settings.hub_timezone).propose(
                user.uuid,
                project.id,
                plan.operations,
                title=f"Aufgaben neu geschrieben: {', '.join(plan.touched)}"[:200],
                summary=turn.last_user_text[:500],
                task_id=draft_id,
                agent_plan=True,
            )
    except GatewayError as exc:
        error = exc.message
    except SetError as exc:
        error = str(exc)
        plan.operations = []
    await _drop_message(request, user, turn, progress.id)
    yield chat.sse("progress_done", {"message_id": progress.id})
    if error:
        text = (
            f"Ich konnte die Aufgaben nicht neu schreiben: {error}"
            if lang == "de"
            else f"I could not rewrite the tasks: {error}"
        )
    else:
        text = project_write.answer_text(lang, plan, project.title)
    if plan.operations and not error:
        fake = SimpleNamespace(
            id=draft_id,
            diagnosis={"summary": text.split("\n\n")[0], "notes": plan.notes},
            status="done",
            stop_reason=None,
            tool_calls=0,
        )
        card = report.build(fake, [])
        if card is not None:
            card = {**card, "changes_for": str(draft_id)}
            row = await _new_message(
                request,
                user,
                turn,
                report.text(card),
                kind="report",
                report=card,
                status="complete",
            )
            yield chat.sse("report", {"message": message_view(row)})
    message = await _store_answer(request, user, turn, text, status="complete", error=None)
    yield chat.sse("delta", {"text": text})
    yield chat.sse("done", {"message": message})


async def _handover_answer_events(
    request: Request, user: CurrentUser, turn: Turn, owner: str
) -> AsyncIterator[str]:
    """The answer of a turn that belongs to another program: the hand-over is prepared (nothing is
    sent, D15) and the answer says where it waits, as when the model calls ``hand_over``."""
    args = json.dumps({"to": owner, "request": turn.last_user_text})
    text, proposed = await _propose_handover(request, user, turn, args)
    message = await _store_answer(request, user, turn, text, status="complete", error=None)
    yield chat.sse("delta", {"text": text})
    if proposed is not None:
        yield chat.sse("handover", proposed)
    yield chat.sse("done", {"message": message})


async def _research_offer_events(
    request: Request, user: CurrentUser, turn: Turn
) -> AsyncIterator[str]:
    """The chat offers a research of its own; a click starts it in this conversation
    (``start_research_in_chat``). Nothing runs before the click."""
    language = hub_language(turn.language, turn.last_user_text)
    text = _RESEARCH_OFFER[language]
    message = await _store_answer(
        request,
        user,
        turn,
        text,
        status="complete",
        error=None,
        report={"research_offer": {"question": turn.last_user_text[:2000]}},
    )
    yield chat.sse("delta", {"text": text})
    yield chat.sse("done", {"message": message})


async def _write_research_message(
    app: Any,
    user: CurrentUser,
    conversation_id: uuid.UUID,
    message_id: int,
    content: str,
    status: str,
) -> None:
    async with app.state.sessionmaker() as db:
        await ChatStore(db, user.uuid).update_message(
            conversation_id, message_id, content=content, status=status
        )
        await db.commit()
    app.state.bus.publish(
        "chat_message",
        {
            "conversation_id": str(conversation_id),
            "message_id": message_id,
            "_user": str(user.uuid),
        },
    )


async def _research_start_events(
    request: Request, user: CurrentUser, turn: Turn
) -> AsyncIterator[str]:
    """The chat starts the research as a job, says so in the message, and the person goes on."""
    state = request.app.state
    language = hub_language(turn.language, turn.last_user_text)
    try:
        run = state.blocks.start(
            user, blocks_builtin.PROGRAMS[research_chat.PROGRAM], {"question": turn.last_user_text}
        )
    except BlockError as exc:
        text = research_chat.failed_line(exc.message, language)
        message = await _store_answer(request, user, turn, text, status="complete", error=None)
        yield chat.sse("delta", {"text": text})
        yield chat.sse("done", {"message": message})
        return
    turn.research_job = True
    text = _RESEARCH_LINE[language]
    message = await _store_answer(request, user, turn, text, status="running", error=None)
    watch = asyncio.ensure_future(
        _research_watch(
            request.app,
            user,
            turn.conversation_id,
            message["id"],
            run.run_id,
            language,
            turn.last_user_text,
        )
    )
    _WATCHERS.add(watch)
    watch.add_done_callback(_WATCHERS.discard)
    yield chat.sse("delta", {"text": text})
    yield chat.sse("done", {"message": message})


async def _research_watch(
    app: Any,
    user: CurrentUser,
    conversation_id: uuid.UUID,
    message_id: int,
    run_id: uuid.UUID,
    language: str,
    question: str,
) -> None:
    """Writes the progress of the job into its message until it ends; then the message names the
    report, and the chat writes its summary from that report."""
    state = app.state
    started = time.monotonic()
    last = ""
    try:
        while True:
            await asyncio.sleep(WATCH_SECONDS)
            run = state.blocks.get(user.uuid, run_id)
            if run is None:  # gone: the hub was restarted or the run expired
                await _write_research_message(
                    app,
                    user,
                    conversation_id,
                    message_id,
                    research_chat.gone_line(language),
                    "complete",
                )
                return
            if run.finished:
                break
            if time.monotonic() - started > WATCH_MAX_SECONDS:
                await _write_research_message(
                    app,
                    user,
                    conversation_id,
                    message_id,
                    research_chat.failed_line(research_chat.TOO_LONG[language], language),
                    "complete",
                )
                return
            line = research_chat.progress_line(run, language)
            if line != last:
                last = line
                await _write_research_message(
                    app, user, conversation_id, message_id, line, "running"
                )
        result = run.result
        if result.status == "ok" and result.filed:
            title = question.strip()[:120]
            await _write_research_message(
                app,
                user,
                conversation_id,
                message_id,
                research_chat.done_line(title, str(result.filed[-1]), language),
                "complete",
            )
        else:
            why = result.error or result.reason or research_chat.NO_REPORT[language]
            await _write_research_message(
                app,
                user,
                conversation_id,
                message_id,
                research_chat.failed_line(why, language),
                "complete",
            )
            return
    except asyncio.CancelledError:
        raise
    except Exception:
        log.exception("research watch of run %s failed", run_id)
        await _write_research_message(
            app,
            user,
            conversation_id,
            message_id,
            research_chat.failed_line(research_chat.BROKE[language], language),
            "complete",
        )
        return
    # the summary is its own turn: a failure there leaves the report's message as it is
    try:
        body = str(run.result.vars.get("draft") or "")
        await _research_summary(app, user, conversation_id, question.strip()[:120], body, language)
    except Exception:
        log.exception("research summary of run %s failed", run_id)


async def _research_summary(
    app: Any,
    user: CurrentUser,
    conversation_id: uuid.UUID,
    title: str,
    body: str,
    language: str,
) -> None:
    """The chat answers the conversation's questions from the report: a turn of its own, the hub
    speaks up (like a second look), without tools and without a new research."""
    request: Any = SimpleNamespace(app=app)
    # no answer may be in writing in the chat meanwhile (the start line is one): wait until quiet
    for _ in range(120):
        if app.state.runs.running(conversation_id) is None:
            break
        await asyncio.sleep(WATCH_SECONDS)
    turn = await _prepare(
        request,
        user,
        conversation_id,
        None,
        hidden=research_chat.summary_note(title, body, language),
    )
    turn.from_hub = True
    outcome: asyncio.Future[Response | None] = asyncio.get_running_loop().create_future()
    run = await _launch(request, user, turn, _pipeline(request, user, turn, "off", outcome))
    if await asyncio.shield(outcome) is not None:
        log.info("research summary of %s was not started", conversation_id)
        return
    app.state.bus.publish(
        "chat_message",
        {
            "conversation_id": str(conversation_id),
            "message_id": run.message_id,
            "_user": str(user.uuid),
        },
    )


async def _pipeline(
    request: Request,
    user: CurrentUser,
    turn: Turn,
    tools: str,
    outcome: "asyncio.Future[Response | None]",
) -> AsyncIterator[str]:
    """The answer's events. Until the model has started to answer, a failure is reported through
    ``outcome`` (an HTTP error response) and nothing is yielded; ``outcome`` is ``None`` once the
    answer starts."""
    try:
        # (aclosing: a stop or a shutdown reaches the generators inside, which store the text)
        async with contextlib.aclosing(
            _pipeline_events(request, user, turn, tools, outcome)
        ) as events:
            async for chunk in events:
                yield chunk
    except asyncio.CancelledError:
        if not outcome.done():  # stopped before the model answered: the reader gets the end
            outcome.set_result(None)
        raise
    finally:
        if not outcome.done():  # a crash before the model answered: the request hears about it
            outcome.set_result(_error(500, "internal_error", "the answer could not be started"))


async def _reserve(state: Any, turn: Turn) -> Any:
    """The place for this answer. A model or node the person picked wins, but a pick that cannot
    be served now (its machine is off, away or kept free) does not end in an error: the row of the
    program in the table of applications answers instead, and the answer says whom it replaces
    (the same label as any other hand-over to the next node)."""
    try:
        return await state.gateway.reserve(turn.requested)
    except GatewayError as exc:
        if not turn.picked or not (exc.status == 503 or exc.code == "node_not_found"):
            raise
        choice = await state.gateway.resolver.resolve(
            "answer", application=turn.application or "chat", if_none="first"
        )
        if choice.node is None or choice.requested == turn.requested:
            raise
        try:
            placement = await state.gateway.reserve(choice.requested)
        except GatewayError:
            raise exc from None
        away = turn.requested.partition("@")[2]
        turn.chain_from, turn.chain_state = away or turn.requested, "offline"
        turn.requested, turn.model = choice.requested, choice.model
        return placement


async def _pipeline_events(
    request: Request,
    user: CurrentUser,
    turn: Turn,
    tools: str,
    outcome: "asyncio.Future[Response | None]",
) -> AsyncIterator[str]:
    state = request.app.state
    emoji = await _reaction_for(request, turn)
    if emoji is not None:
        outcome.set_result(None)
        async with contextlib.aclosing(_reaction_events(request, user, turn, emoji)) as events:
            async for chunk in events:
                yield chunk
        return
    issue = _issue_request(turn)
    if issue is not None:
        # "Problem: …" files an issue (the person's own command, no model); a complaint in
        # Projekte gets a card that files it on a click (project_issues.py)
        outcome.set_result(None)
        async with contextlib.aclosing(_issue_events(request, user, turn, *issue)) as events:
            async for chunk in events:
                yield chunk
        return
    set_job_context(
        user_id=user.uuid,
        conversation_id=turn.conversation_id,
        application=turn.application or "chat",
    )  # the record of the model requests of this answer (SCH-03 step 3); the row of the table
    try:
        turn.placement = await _reserve(state, turn)
        route = turn.placement.route
        context_length = turn.placement.context_length or state.settings.chat_default_context_tokens
        budget = chat.prompt_budget(context_length)
        # Notes about the user that fit this message (E2-02); never for a continuation.
        recalled: list[Recalled] = []
        if turn.memory_enabled and turn.policy.recall and turn.continues is None:
            recalled = await state.memory.recall(
                user.uuid,
                turn.last_user_text,
                min(RECALL_MAX_TOKENS, budget // 5),
                also=turn.last_refs if is_followup(turn.last_user_text) else (),
                ask_allowed=not turn.care_asked,
                application=turn.application,
            )
        # The secretary decides who does the task, by the contracts (D112, issue #36).
        decision = await _secretary_route(request, user, turn, tools, bool(route.candidates))
        if decision is not None:
            outcome.set_result(None)
            async with contextlib.aclosing(
                _secretary_events(request, user, turn, decision, recalled, context_length)
            ) as routed_events:
                async for chunk in routed_events:
                    yield chunk
            return
        # A question about the user's servers is answered after a check (E3-01d).
        checking, refusal = await _plan_check(request, user, turn, tools, bool(route.candidates))
        if refusal is not None:
            outcome.set_result(refusal)
            return
        handed = None if checking else await _start_handoff(request, user, turn, tools, route)
        if handed is not None:
            outcome.set_result(None)
            async with contextlib.aclosing(
                _handoff_events(
                    request, user, turn, route, handed[0], handed[1], recalled, context_length
                )
            ) as handoff_events:
                async for chunk in handoff_events:
                    yield chunk
            return
        mode = research_mode(turn.application, turn.quality, turn.from_hub) if checking else None
        if mode is not None:
            # in Research a check is a research of its own (at "thorough") or the offer of one
            # (at "balanced"): the chat starts it or offers it, says so and goes on
            outcome.set_result(None)
            events = (
                _research_start_events(request, user, turn)
                if mode == "job"
                else _research_offer_events(request, user, turn)
            )
            async with contextlib.aclosing(events) as started:
                async for chunk in started:
                    yield chunk
            return
        if checking and turn.application == "project":
            found = await _iteration_target(request, user, turn)
            if found is not None:
                # "make the next version of 1.8.2": a card that starts the next iteration with
                # the message as feedback (project_iterations.py); the task text stays
                outcome.set_result(None)
                async with contextlib.aclosing(
                    _iteration_events(request, user, turn, *found)
                ) as offering:
                    async for chunk in offering:
                        yield chunk
                return
        if checking and turn.application == "project":
            target = await _write_target(request, user, turn)
            if target is not None:
                # "rewrite these tasks": the hub reads, the chat model writes, the hub builds the
                # set (project_write.py); no agent loop
                outcome.set_result(None)
                async with contextlib.aclosing(
                    _write_events(request, user, turn, *target)
                ) as writing:
                    async for chunk in writing:
                        yield chunk
                return
        if checking and not turn.handover_targets:
            turn.handover_targets = await _handover_targets(request, user, turn)
        if checking and (owner := project_owner(turn)) is not None:
            # the check the hub would start has no project tools: the hand-over instead
            outcome.set_result(None)
            async with contextlib.aclosing(
                _handover_answer_events(request, user, turn, owner)
            ) as handing:
                async for chunk in handing:
                    yield chunk
            return
        if checking:
            outcome.set_result(None)
            async with contextlib.aclosing(
                _checked_answer(
                    request,
                    user,
                    turn,
                    route,
                    checking,
                    notes_on_topic(recalled, turn.last_user_text),
                    context_length,
                    budget,
                    order=_order_of(turn, "{}")[0],  # a check the hub starts: one machine, or none
                )
            ) as checked:
                async for chunk in checked:
                    yield chunk
            return
        # What changes with the question (the recalled notes) goes last, into the final user
        # turn, so the model can reuse its prompt cache for everything before it (D27).
        notes = chat.notes_context([n.prompt_line() for n in recalled])
        notes = await _brief_block(request, user, turn) + notes
        if turn.segment_block:  # the hand-over comes first, capped by what the context allows
            cap = max(budget // 4, 0) * chat.CHARS_PER_TOKEN
            notes = turn.segment_block[:cap] + notes
        notes = await _second_look_block(request, user, turn) + notes
        notes += turn.accepted_hint  # last, next to the person's "ja": what it agreed to
        earlier = await chat_tools.earlier_check(
            request.app.state.sessionmaker, user.uuid, turn.conversation_id
        )
        if earlier is not None:  # a follow-up without a check still sees what was read (1.3)
            room = min(max(budget // 3, 0) * chat.CHARS_PER_TOKEN, chat_tools.EARLIER_CHARS)
            notes = chat_tools.earlier_block(earlier, room) + notes
        core_text = await _core_text(request, user, turn, context_length)
        upstream: Upstream | None = None
        for attempt in (0, 1):
            # Small contexts get the short system prompt: the fixed rules must not eat the window.
            system = chat.system_prompt(
                turn.user_prompt,
                turn.address,
                turn.capabilities,
                compact=context_length < chat.COMPACT_CONTEXT_TOKENS,
                core=core_text,
                where=await _where(request, user, turn, route, context_length),
            )
            sent: list[dict[str, Any]] | None = None
            if turn.continues is None:
                prompt = chat.build_prompt(system, turn.history, budget, notes)
                if turn.vision_history is not None:
                    # D125: the same prompt with the images where they stand, for a model that sees
                    seen = chat.build_prompt(system, turn.vision_history, budget, notes)
                    sent, _ = chat_images.expand(seen.messages, turn.image_data)
                    sent[0] = {
                        **sent[0],
                        "content": f"{sent[0]['content']}\n\n{chat_images.DATA_RULE}",
                    }
            else:
                continued = chat.build_continue_prompt(system, turn.history, budget, notes)
                if continued is None:
                    outcome.set_result(
                        _error(
                            409,
                            "context_too_small",
                            f"The context of {turn.model} ({context_length} tokens) is too small "
                            "to continue this answer. Raise the context length of the model on "
                            "its node (runner.model_context_lengths) or write a new message.",
                        )
                    )
                    return
                prompt = continued
            payload: dict[str, Any] = {
                "model": turn.requested,
                "messages": sent if sent is not None else prompt.messages,
                "stream": True,
                "stream_options": {"include_usage": True},
                # llama-server keeps the KV cache of the last prompt of a slot and only processes
                # what differs from it; the prompt is ordered for that (D27)
                "cache_prompt": True,
            }
            offered = []
            if turn.decide:
                offered.append(
                    chat_tools.decider_tool(
                        turn.decide_groups,
                        turn.decide_targets,
                        turn.decide_keys,
                        web=turn.capabilities.web_search,
                    )
                )
                offered.extend(local_tools.definitions(set(turn.decide_keys)))
            if turn.handover_targets:
                offered.append(chat_tools.handover_tool(dict(turn.handover_targets)))
            if offered:
                payload["tools"] = offered
                payload["tool_choice"] = "auto"
            if turn.temperature is not None:
                payload["temperature"] = turn.temperature
            payload.update(chat.sampling_params(turn.model, state.settings.chat_sampling))
            payload.update(chat.effort_params(turn.model, turn.quality, state.settings.chat_effort))
            upstream = await state.gateway.forward(
                turn.requested,
                "POST",
                "/v1/chat/completions",
                payload,
                accept="text/event-stream",
                placement=turn.placement,
            )
            if turn.chain_from and upstream.fallback_from is None:
                upstream.fallback_from, upstream.fallback_state = turn.chain_from, turn.chain_state
            if offered and upstream.status in (400, 422) and attempt == 0:
                # a server without --jinja refuses ``tools``: this model decides by the heuristic
                # from now on, and this message is asked again without the function
                upstream.close()
                _no_decider(request).add(turn.requested)
                turn.decide = False
                turn.handover_targets = ()
                turn.capabilities = replace(turn.capabilities, decider=False)
                continue
            break
        if upstream is None:  # unreachable: the loop always forwards once
            outcome.set_result(_error(502, "model_error", "the model could not be asked"))
            return
    except GatewayError as exc:
        outcome.set_result(_error(exc.status, exc.code, exc.message, exc.retry_after))
        return
    if upstream.status != 200:
        detail = b"".join([c async for c in upstream.body])[:2000].decode("utf-8", "replace")
        upstream.close()
        log.warning("chat: model answered %s: %s", upstream.status, detail)
        if is_context_full(detail):
            outcome.set_result(
                _error(
                    503,
                    "context_full",
                    CONTEXT_FULL_MESSAGE,
                    state.settings.gateway_retry_after_seconds,
                )
            )
            return
        outcome.set_result(
            _error(502, "model_error", f"the model refused the request ({upstream.status})")
        )
        return
    on_tool = None
    if turn.decide:

        def on_tool(name: str, arguments: str) -> AsyncIterator[str]:
            if name in local_tools.LOCAL:  # read-only functions of the hub itself (T3.6, T3.8)
                return _local_answer(
                    request, user, turn, route, name, arguments, context_length, budget, recalled
                )
            # the model decided to look: in Research that is a research (or its offer), too
            mode = research_mode(turn.application, turn.quality, turn.from_hub)
            if mode == "job":
                return _research_start_events(request, user, turn)
            if mode == "offer":
                return _research_offer_events(request, user, turn)
            order, focus = _order_of(turn, arguments)
            return _checked_answer(
                request,
                user,
                turn,
                route,
                "decided",
                notes_on_topic(recalled, turn.last_user_text),
                context_length,
                budget,
                focus,
                order,
            )

    after_done = None
    # a revision is asked with what the model was shown, the images included
    shown_prompt = replace(prompt, messages=sent) if sent is not None else prompt
    held = _held_for(turn, shown_prompt, on_tool, request.app.state.settings)
    if held is None and turn.second_look and turn.verify_model:
        # "thorough": a second look at every answer; without a check there is no evidence, the
        # reviewer marks what looks wrong or cannot be known without looking (quality.py)

        def after_done(message: dict[str, Any]) -> AsyncIterator[str]:
            return _verify_answer(request, user, turn, message, "")

    outcome.set_result(None)  # the model answers: the request becomes a reader of the run
    async with contextlib.aclosing(
        _answer(
            request,
            user,
            turn,
            upstream,
            prompt,
            context_length,
            recalled,
            on_tool=on_tool,
            after_done=after_done,
            held=held,
        )
    ) as answer:
        async for chunk in answer:
            if held is not None and held.hides(chunk):
                continue  # the text waits for its check; the activity around it is shown
            yield chunk


SSE_HEADERS = {"Cache-Control": "no-cache", "X-Accel-Buffering": "no"}


def _sse_response(body: AsyncIterator[str]) -> StreamingResponse:
    return StreamingResponse(body, media_type="text/event-stream", headers=SSE_HEADERS)


async def _launch(
    request: Request, user: CurrentUser, turn: Turn, body: AsyncIterator[str]
) -> AnswerRun:
    """Start the answer as a task of the hub (E3-07): it goes on whatever happens to the
    connection that asked. The message it is written into exists from now on (status ``running``,
    the text so far saved every second), so a reload or another tab finds it and attaches."""
    state = request.app.state
    async with state.sessionmaker() as db:
        store = ChatStore(db, user.uuid)
        if turn.continues is not None:
            await store.update_message(turn.conversation_id, turn.continues.id, status="running")
            turn.message_id = turn.continues.id
        else:
            placeholder = await store.add_message(
                turn.conversation_id, "assistant", "", status="running"
            )
            turn.message_id = placeholder.id
        await db.commit()
    assert turn.message_id is not None  # noqa: S101
    turn.run_key = turn.message_id
    run = AnswerRun(user.uuid, turn.conversation_id, turn.message_id, turn)
    state.runs.start(
        run,
        body,
        save=lambda r, text: _save_partial(request, user, turn, text),
        finalize=lambda r: _settle(request, user, turn, r),
    )
    return run


async def _save_partial(request: Request, user: CurrentUser, turn: Turn, text: str) -> None:
    """The text so far goes into the database (a reload, another tab, a restart see it). Only a
    message that is still ``running`` is touched: an answer stored as finished before its text was
    sent (the research offer, live 2026-11-20) stays finished instead of turning back to
    ``running`` and then to ``error`` when the run is settled."""
    if turn.message_id is None:
        return
    prefix = turn.continues.content if turn.continues is not None else ""
    async with request.app.state.sessionmaker() as db:
        await db.execute(
            update(Message)
            .where(
                Message.id == turn.message_id,
                Message.user_id == user.uuid,
                Message.conversation_id == turn.conversation_id,
                Message.status == "running",
            )
            .values(content=prefix + text)
        )
        await db.commit()


async def _settle(
    request: Request, user: CurrentUser, turn: Turn, run: AnswerRun
) -> dict[str, Any] | None:
    """After the run: a message that is still ``running`` (nothing stored it — a stop before the
    first word, a crash) gets its final state; an empty one disappears. Returns the message."""
    if turn.message_id is None:
        return None
    async with request.app.state.sessionmaker() as db:
        store = ChatStore(db, user.uuid)
        row = await db.scalar(
            select(Message).where(Message.id == turn.message_id, Message.user_id == user.uuid)
        )
        if row is None:
            return None
        if row.status == "running" and not row.content and turn.continues is None:
            await store.delete_message(turn.conversation_id, row.id)
            await db.commit()
            return None
        final = settled_state(turn, row.status, row.content, run.stop_requested)
        if final is not None:
            await store.update_message(
                turn.conversation_id,
                row.id,
                status=final,
                error=None if final != "error" else "the answer could not be completed",
            )
            await db.commit()
            await db.refresh(row)
        return message_view(row)


def settled_state(turn: Any, row_status: str, content: str, stop_requested: bool) -> str | None:
    """The state a message gets when its run ends, or ``None`` when it keeps the state it has.
    Only a ``running`` message is settled; a research job keeps ``running`` because its watcher
    writes the progress and the end. A continued answer that gained nothing stays continuable."""
    if row_status != "running" or turn.research_job:
        return None
    if turn.continues is not None and content == turn.continues.content:
        # nothing was added (refused before the model answered, stopped at once): the answer
        # stays what it was -- cut off and continuable
        return "truncated"
    if turn.interrupted:
        return "interrupted"
    return "stopped" if stop_requested else "error"


def _no_decider(request: Request) -> set[str]:
    """The models whose server refused the ``tools`` of the decision function."""
    state = request.app.state
    if not hasattr(state, "chat_no_decider"):
        state.chat_no_decider = set()
    found: set[str] = state.chat_no_decider
    return found


PROJECT_GROUP = "project"
_ABOUT_PROJECTS = re.compile(r"\b(?:projekt\w*|aufgabe\w*|project\w*|tasks?)\b", re.I)


def project_owner(turn: Turn) -> str | None:
    """The program a project question belongs to when this conversation cannot read projects: the
    hand-over target whose tools include the project group, if the question is about projects or
    tasks and the conversation's scope has no project tools. ``None`` otherwise."""
    groups = (turn.scope or {}).get("groups")
    if groups is None or PROJECT_GROUP in groups:
        return None  # every group, or the project tools are here: the check can answer
    if not _ABOUT_PROJECTS.search(turn.last_user_text):
        return None
    label = next(iter(tool_groups.labels([PROJECT_GROUP])), PROJECT_GROUP)
    for key, what in turn.handover_targets:
        if label in what:
            return key
    return None


# what a program is called in the answer that proposes a hand-over to it
PROGRAM_NAMES = {lang: applications.names(lang) for lang in ("de", "en")}


async def _handover_targets(
    request: Request, user: CurrentUser, turn: Turn
) -> tuple[tuple[str, str], ...]:
    """The programs this conversation may hand a request to: the person's other programs that have
    a conversation and tools of their own, each with what it can do (for the function's text)."""
    if getattr(request.app.state, "handovers", None) is None:
        return ()
    out: list[tuple[str, str]] = []
    async with request.app.state.sessionmaker() as db:
        access = await resolve_access(db, user.uuid, user.role)
        # what this conversation can do itself is no reason to hand a request over
        mine = await app_scope.ceiling(db, user, turn.application or "chat")
        my_groups = set(
            mine["groups"] if mine.get("groups") is not None else tool_groups.SELECTABLE
        )
        my_web = app_scope.internet_allowed(mine)
        for template in await TemplateStore(db).all():
            # the programs keep their conversations in Chat: a program is a target by its tools
            if template.key == (turn.application or "chat"):
                continue
            if not access.workspace_visible(template):
                continue
            limit = await app_scope.ceiling(db, user, template.key)
            groups = limit.get("groups")
            wanted = [
                g
                for g in (groups if groups is not None else tool_groups.SELECTABLE)
                if g not in my_groups
            ]
            what = list(tool_groups.labels(wanted))
            if app_scope.internet_allowed(limit) and not my_web:
                what.append("the web")
            if template.key == research_profile.APPLICATION:
                # Research has no tools Chat lacks, but a kind of work of its own: a thorough
                # research with sources and a report (2026-11-13: a hand-over "to Research" went
                # to Projekte, because Research was never offered)
                what.insert(0, "a thorough research with sources, filed as a report")
            if what:
                out.append((template.key, ", ".join(what)))
    return tuple(out)


async def _propose_handover(
    request: Request, user: CurrentUser, turn: Turn, arguments: str
) -> tuple[str, dict[str, Any] | None]:
    """The model called ``hand_over``: prepare the dossier (status ``proposed``) and say so. The
    person reads it in the inbox and sends it; nothing leaves this conversation before (D15)."""
    from topcoat_hub.handover import HandoverError
    from topcoat_hub.handover import view as handover_view

    lang = hub_language(turn.language, turn.last_user_text)  # the language of the question
    to, wanted = chat_tools.handover_from(arguments)
    targets = dict(turn.handover_targets)
    name = PROGRAM_NAMES[lang].get(to, to)
    if to not in targets:
        return (
            "Das kann ich hier nicht beantworten, und es gibt kein Programm, an das ich es "
            "übergeben könnte."
            if lang == "de"
            else "I cannot answer this here, and there is no program I could hand it to."
        ), None
    try:
        proposed = await request.app.state.handovers.prepare(
            user, to, conversation_id=turn.conversation_id, note=wanted or None
        )
    except HandoverError as exc:
        log.info("chat: the hand-over to %s could not be prepared: %s", to, exc.code)
        return (
            f"Dafür ist {name} zuständig, aber die Übergabe ließ sich nicht vorbereiten."
            if lang == "de"
            else f"{name} is the program for this, but the hand-over could not be prepared."
        ), None
    # where it is, as a link that stays in the conversation (2026-11-13: the text sent the person
    # to the inbox, where hand-overs are not, and the talk at a window showed no button)
    link = f"#/handovers/{proposed.id}"
    text = (
        f"Dafür ist **{name}** zuständig. Ich habe eine Übergabe vorbereitet, sie ist noch nicht "
        f"abgeschickt: [Übergabe ansehen und abschicken]({link}) (in Start unter „Übergaben“). "
        "Erst wenn du sie abschickst, geht sie raus; die Antwort kommt dann in deinen Posteingang."
        if lang == "de"
        else f"That is what **{name}** is for. I prepared a hand-over; it is not sent yet: "
        f'[See and send the hand-over]({link}) (in Start under "Hand-overs"). Only when you send '
        "it does it go out; the answer then comes to your inbox."
    )
    return text, handover_view(proposed)


async def _can_decide(request: Request, turn: Turn) -> bool:
    """May the model be offered ``look_at_my_systems`` for this turn? Only a model that makes
    native tool calls: the agent's tool mode for it is not ``json`` and its server did not refuse
    ``tools`` before."""
    if turn.requested in _no_decider(request):
        return False
    model = request.app.state.agents.loop.model
    mode_of = getattr(model, "tool_mode", None)
    if mode_of is None:
        return False
    try:
        return bool(await mode_of(turn.requested) != "json")
    except Exception:
        return False


async def _waiting_sets(request: Request, user: CurrentUser, turn: Turn) -> list[tuple[str, str]]:
    """The change sets shown on this conversation's cards that wait for the person's click:
    (title, what it changes). A card names its set by the check's task (``agent_task_id``) or, for
    a set the hub wrote without a run, by ``changes_for``."""
    if turn.conversation_id is None:
        return []
    async with request.app.state.sessionmaker() as db:
        cards = (
            await db.execute(
                select(Message.agent_task_id, Message.report).where(
                    Message.user_id == user.uuid,
                    Message.conversation_id == turn.conversation_id,
                    Message.kind == "report",
                )
            )
        ).all()
        ids: set[uuid.UUID] = set()
        for task_id, card in cards:
            if task_id is not None:
                ids.add(task_id)
            ref = card.get("changes_for") if isinstance(card, dict) else None
            if ref:
                with contextlib.suppress(ValueError):
                    ids.add(uuid.UUID(str(ref)))
        if not ids:
            return []
        rows = (
            await db.execute(
                select(ProjectChangeSet)
                .where(
                    ProjectChangeSet.user_id == user.uuid,
                    ProjectChangeSet.status == "pending",
                    ProjectChangeSet.task_id.in_(ids),
                )
                .order_by(ProjectChangeSet.created_at)
            )
        ).scalars()
        return [(s.title, project_changesets.summary_of(s)) for s in rows]


async def _plan_check(
    request: Request, user: CurrentUser, turn: Turn, tools: str, routable: bool
) -> tuple[str | None, JSONResponse | None]:
    """Does this answer start with a server check? ``(reason, refusal)``: the reason is
    ``"forced"`` (the user switched it on) or ``"detected"`` (the question clearly names their
    servers), ``None`` for a plain answer; a refusal is the error for a forced check that cannot
    run.

    Otherwise (``auto``) the model decides: it is offered ``look_at_my_systems`` in the answer's own
    stream (``turn.decide``) and the answer turns into a check if it calls it (E3-04). A model that
    cannot call functions gets the old behaviour: when in doubt the answer comes first and the
    browser offers the check (:func:`chat_tools.check_need`).

    Also tells the assistant what it could do: every group of tools the user may use, so it does
    not claim it has no access to something that is offered (honest capabilities)."""
    state = request.app.state
    if turn.continues is not None or not state.settings.agent_enabled:
        return None, None
    # the tools of the conversation's own application count too (Projekte: its project tools)
    everything = await state.agents.tools.available(
        replace(Caller.of(user, "agent"), application=turn.application)
    )
    available = tool_groups.apply_scope(everything, turn.scope)
    # the scope the fixed order is resolved against, also for a check the hub starts itself
    turn.decide_available = tuple(available)
    turn.internet_node = tool_groups.pick_web_node(available)
    # the user may search the web but switched the internet off here: say so, so the assistant
    # does not claim it cannot, and can point to the switch
    turn.capabilities = replace(
        turn.capabilities,
        internet_off=not turn.internet and tool_groups.pick_web_node(everything) is not None,
    )
    if tools != "off" and turn.in_program and await _can_decide(request, turn):
        turn.handover_targets = await _handover_targets(request, user, turn)
    if not available:
        if tools == "on":
            return None, _error(
                409,
                "no_capabilities",
                "There is no tool you may use on a connected tool node (is a tool node "
                "connected, and did an admin grant you its capabilities?).",
            )
        return None, None
    keys = tool_groups.present(available)
    turn.capabilities = replace(
        turn.capabilities,
        tools=tool_groups.labels(keys),
        knowledge="notes" in keys,
        web_search="web" in keys,
    )
    if tools == "off":
        # the user switched the checks off for this message: no claim that the tools are there
        turn.capabilities = replace(turn.capabilities, tools=(), knowledge=False, web_search=False)
        return None, None
    if not routable:
        return None, None  # no model can answer at all: the normal path says why
    if tools == "on":
        return "forced", None
    # the contract in the language of the question (else the person's own, D118 / issue #6)
    own = agent_contract.long(
        turn.application, chat.hub_language(turn.language, turn.last_user_text)
    )
    if own and chat_tools.asks_about_abilities(turn.last_user_text):
        # "what can you do": the contract answers it, in words, without a check (1.9)
        turn.accepted_hint = chat_tools.abilities_hint(own)
        return None, None
    if turn.application == "project" and chat_tools.asks_to_apply(turn.last_user_text):
        waiting = await _waiting_sets(request, user, turn)
        if waiting:
            # "wende die Änderungen an" while a set waits: its card's button applies it, a new run
            # would only replace it (live 2026-11-18)
            language = chat.hub_language(turn.language, turn.last_user_text)
            turn.accepted_hint = chat_tools.apply_hint(language, waiting)
            return None, None
    if turn.application == "project" and len(turn.last_user_text.strip()) >= 15:
        # the only tools of Projekte are its own data: a real message there looks at it first
        return "detected", None
    if turn.application == "research" and len(turn.last_user_text.strip()) >= 15 and turn.internet:
        # Recherche is the program that reads the web: a real question there is researched, not
        # answered from memory and not handed to another program (the check has the web tools)
        return "detected", None
    if turn.context and chat_tools.refers_to_place(turn.last_user_text):
        # Clipper at a window: "this page" is open there, it is read at once (2026-11-12: the
        # model only offered to read the wiki page it had been attached to)
        return "detected", None
    if (
        turn.internet
        and tool_groups.WEB in keys
        and turn.application in (None, "chat")
        and chat_tools.fact_question(turn.last_user_text)
    ):
        # a fact of the world is looked up at once (the maintainer, 2026-11-12)
        return "detected", None
    accepted = chat_tools.accepted_offer(turn.history)
    if accepted is not None and accepted.kind == "explain":
        # "ja bitte" to "soll ich dir erklären, wie …?": answered, not checked again (live
        # 2026-11-17: the same diagnosis ran again and ended with the same offer)
        turn.accepted_hint = accepted.hint
        return None, None
    if accepted is not None:
        # "ja bitte" to "soll ich das prüfen?": the offered check runs, the offer first (live
        # 2026-11-10: the chat offered the same check again instead of running it)
        turn.accepted_question = accepted.task
        return "detected", None
    # every node Topcoat knows (a model node too): "Wie ist der Stand von node-nvidia?" is a state
    # question its tools answer (live 2026-11-11: the model only offered to look)
    machines = {t["node"] for t in available}
    async with state.sessionmaker() as db:
        machines |= {n for (n,) in (await db.execute(select(Node.name))).all()}
    need = chat_tools.check_need(
        turn.last_user_text,
        machines,
        self_tool="topcoat" in keys,
        github_tool="github" in keys,
        tool_words=chat_tools.topic_words(t["capability"] for t in available),
    )
    if need == "check":
        return "detected", None
    if await _can_decide(request, turn):
        turn.decide, turn.decide_groups = True, tool_groups.labels(keys)
        turn.decide_keys = tuple(k for k in keys if k != tool_groups.WEB)
        turn.decide_targets = tuple(
            sorted({t["node"] for t in available if t["node"] not in tool_groups.HUB_VIEWS})
        )
        turn.decide_available = tuple(available)
        turn.capabilities = replace(turn.capabilities, decider=True)
        return None, None
    turn.check_offer = need == "offer"
    return None, None


# The most of the check's results put in front of the answer (about 1500 tokens).
CHECK_BLOCK_CHARS = 6000


CONFIRMATIONS = {"de": "Schau ich mir an.", "en": "On it — I'll take a look."}
# applications whose checks write text into a proposal, and the room one call gets for it
WRITING_APPLICATIONS = frozenset({"project", "dev"})
WRITE_OUTPUT_TOKENS = 4096
hub_language = chat.hub_language


async def _confirm(request: Request, user: CurrentUser, turn: Turn) -> dict[str, Any]:
    """The first bubble of a request that needs a finding (E3-07 D3): the hub says it is looking,
    in a sentence of its own — the model writes neither this nor the report. It is written into
    the message the run was started with; the closing bubble gets a message of its own later."""
    text = CONFIRMATIONS[hub_language(turn.language, turn.last_user_text)]
    async with request.app.state.sessionmaker() as db:
        row = await ChatStore(db, user.uuid).update_message(
            turn.conversation_id, turn.message_id or 0, content=text, status="complete"
        )
        await db.commit()
    turn.message_id = None  # the next message of the run is the report, then the closing bubble
    return message_view(row) if row is not None else {}


async def _reaction_for(request: Request, turn: Turn) -> str | None:
    """The emoji a plain acknowledgement is answered with (``None``: it gets a real answer)."""
    if turn.continues is not None or not turn.policy.wants_reactions(
        request.app.state.settings.reactions_enabled
    ):
        return None
    previous = [t for r, t in turn.history[:-1] if r == "assistant"]
    if not previous:
        return None  # nothing to acknowledge yet
    return reaction.emoji_for(turn.last_user_text, previous[-1])


async def _reaction_events(
    request: Request, user: CurrentUser, turn: Turn, emoji: str
) -> AsyncIterator[str]:
    """No model is asked and nothing is generated: the answer is the emoji, shown at the message."""
    yield _meta_event(turn, "hub", None, None, 0, 0, 0, [])
    async with request.app.state.sessionmaker() as db:
        row = await ChatStore(db, user.uuid).update_message(
            turn.conversation_id,
            turn.message_id or 0,
            content=emoji,
            status="complete",
            kind="reaction",
        )
        await db.commit()
    turn.message_id = None
    yield chat.sse("done", {"message": message_view(row) if row is not None else {}})


# the last turns the routing decision sees ("do that", "and the other one"), each cut
SECRETARY_TURNS = 4
SECRETARY_TURN_CHARS = 300


async def _secretary_route(
    request: Request, user: CurrentUser, turn: Turn, tools: str, routable: bool
) -> secretary.Route | None:
    """The secretary's decision for this message (D112): a task handed on or a question about the
    tasks handed on; ``None`` when the answer is written here as before. Only in the assistant's
    own conversation (Chat), for a new message the person wrote, with tools and agents on; a
    decision that cannot be made (no model answer, no contract) leaves the old path."""
    state = request.app.state
    if (
        not state.settings.secretary_routing
        or not state.settings.agent_enabled
        or turn.application != "chat"
        or turn.continues is not None
        or turn.from_hub
        or turn.accepted_question
        or tools == "off"
        or not routable
        or not secretary.targets()
        or not turn.last_user_text.strip()
    ):
        return None
    earlier = [
        f"{role}: {' '.join(text.split())[:SECRETARY_TURN_CHARS]}"
        for role, text in turn.history[:-1][-SECRETARY_TURNS:]
    ]
    decision = await secretary.route(
        state.gateway, turn.requested, turn.last_user_text, "\n".join(earlier)
    )
    if decision is None or not (decision.hands_on or decision.asks_status):
        return None  # the secretary answers it itself, as before
    return decision


SECRETARY_SAY = {
    "en": {
        "handed": "I handed this to {who}: “{topic}”.",
        "report": " I'll tell you here when it is done.",
        "ask": " Ask me “how far is it?” any time.",
        "held": " It is not in the queue yet ({reason}).",
        "failed": "I could not hand this on ({reason}). Please ask {who} directly.",
    },
    "de": {
        "handed": "Ich habe das an {who} weitergegeben: „{topic}“.",
        "report": " Ich sage dir hier Bescheid, wenn es fertig ist.",
        "ask": " Frag mich jederzeit „wie weit ist es?“.",
        "held": " Es ist noch nicht in der Warteschlange ({reason}).",
        "failed": "Ich konnte das nicht weitergeben ({reason}). Frag bitte {who} direkt.",
    },
}


async def _secretary_events(
    request: Request,
    user: CurrentUser,
    turn: Turn,
    decision: secretary.Route,
    recalled: list[Recalled],
    context_length: int,
) -> AsyncIterator[str]:
    """The secretary's answer in the hub's own words, no second model call: the state of the
    tasks handed on, or that the task was handed on (an ``assistant.request`` in the person's
    Secretary project, queued at once: it only reads, so no click, D112)."""
    from topcoat_hub.message_service import MessageService

    yield _meta_event(turn, "hub", None, None, context_length, 0, 0, recalled)
    state = request.app.state
    lang = hub_language(turn.language, turn.last_user_text)
    say = SECRETARY_SAY[lang]
    if decision.asks_status:
        text = await secretary.status_lines(
            state.sessionmaker, user.uuid, turn.conversation_id, lang
        )
    else:
        contract = agent_contract.get(decision.to)
        who = contract.title(lang) if contract is not None else decision.to
        service = MessageService(
            ProjectStore(state.sessionmaker, PERSON, state.settings.hub_timezone),
            state.agents,
            getattr(state, "proposals", None),
        )
        try:
            handed = await secretary.hand_on(
                service, user.uuid, user.is_admin, turn.conversation_id, decision
            )
        except (ValueError, ProjectError) as exc:
            text = say["failed"].format(reason=str(exc)[:200], who=who)
        else:
            text = say["handed"].format(who=who, topic=decision.topic)
            text += say["report"] if decision.report_when_done else say["ask"]
            if handed.reason:
                text += say["held"].format(reason=handed.reason)
    async with state.sessionmaker() as db:
        row = await ChatStore(db, user.uuid).update_message(
            turn.conversation_id, turn.message_id or 0, content=text, status="complete"
        )
        await db.commit()
    turn.message_id = None
    yield chat.sse("done", {"message": message_view(row) if row is not None else {}})


async def _start_handoff(
    request: Request, user: CurrentUser, turn: Turn, tools: str, route: Any
) -> tuple[AgentTask, str] | None:
    """A request that says it may take long becomes a task (hand-over to the queue); ``None``
    when it is answered here: not asked for, urgent, tools off, or no task can be started."""
    state = request.app.state
    if (
        turn.continues is not None
        or tools == "off"
        or not state.settings.handoff_enabled
        or not state.settings.agent_enabled
        or not route.candidates
    ):
        return None
    why = handoff.reason(turn.quality, turn.last_user_text)
    if why is None:
        return None
    try:
        task = await state.agents.start(
            user,
            chat_tools.task_prompt(turn.last_user_text, turn.task_history or turn.history),
            None,
            scope=turn.scope,
            conversation_id=turn.conversation_id,
        )
        return task, why
    except AgentError:
        return None  # no tool node, too many tasks: the chat answers as usual


async def _handoff_events(
    request: Request,
    user: CurrentUser,
    turn: Turn,
    route: Any,
    task: AgentTask,
    why: str,
    recalled: list[Recalled],
    context_length: int,
) -> AsyncIterator[str]:
    """The answer to a handed-over request: one sentence of the hub's own, no model asked."""
    yield _meta_event(turn, "hub", None, None, context_length, 0, 0, recalled)
    lang = hub_language(turn.language, turn.last_user_text)
    text = handoff.ACKNOWLEDGEMENTS[lang]
    info = {"task_id": str(task.id), "deadline_at": None}
    async with request.app.state.sessionmaker() as db:
        row = await ChatStore(db, user.uuid).update_message(
            turn.conversation_id,
            turn.message_id or 0,
            content=text,
            status="complete",
            kind="handoff",
            agent_task_id=task.id,
            report=info,
        )
        await db.commit()
    turn.message_id = None
    yield chat.sse("done", {"message": message_view(row) if row is not None else {}})


async def _new_message(
    request: Request, user: CurrentUser, turn: Turn, content: str = "", **meta: Any
) -> Message:
    async with request.app.state.sessionmaker() as db:
        msg = await ChatStore(db, user.uuid).add_message(
            turn.conversation_id, "assistant", content, **meta
        )
        await db.commit()
        await db.refresh(msg)
        return msg


async def _drop_message(request: Request, user: CurrentUser, turn: Turn, message_id: int) -> None:
    try:
        async with request.app.state.sessionmaker() as db:
            await ChatStore(db, user.uuid).delete_message(turn.conversation_id, message_id)
            await db.commit()
    except Exception:
        log.exception("could not remove the progress line")


def notes_on_topic(recalled: list[Recalled], question: str) -> list[Recalled]:
    """After a server check the answer rests on the check, so a note stays only when it shares a
    whole word with the question (the keyword search also finds "Testeingaben" for "test") or the
    question is about the user themself."""
    if is_personal(question):
        return recalled
    asked = set(keywords(question))
    return [n for n in recalled if asked & set(keywords(n.text))]


async def _closing_fallback(
    request: Request, user: CurrentUser, turn: Turn, card: dict[str, Any]
) -> AsyncIterator[str]:
    """The model wrote nothing under a report card (or could not be asked): the hub writes one
    plain sentence from the card, so a check never ends in a folded card and silence."""
    text = report.closing_fallback(card, hub_language(turn.language, turn.last_user_text))
    turn.message_id = None
    message = await _store_answer(request, user, turn, text, status="complete", error=None)
    yield chat.sse("delta", {"text": text})
    yield chat.sse("done", {"message": message})


# the one line a research run shows while it works (its steps are not shown in the chat)
_RESEARCH_LINE = {
    "de": "Recherche gestartet, sie läuft jetzt. Das dauert meist einige Minuten.",
    "en": "Research started and running. It usually takes a few minutes.",
}
# the chat's pointer to the report a research run filed in the Ablage (the document is the answer)
_RESEARCH_POINTER = {
    "de": "Der Bericht [„{title}“](#/library/report%3A{id}) liegt in der Ablage.",
    "en": 'The report ["{title}"](#/library/report%3A{id}) is in the library.',
}


class _SilentLog:
    """An activity log that keeps nothing: a research answer shows neither its steps nor its
    thinking in the chat (the one line "in progress" and the report stand for the work)."""

    def add(self, *_args: Any, **_kwargs: Any) -> tuple[int, str]:
        return 0, ""

    def update(self, *_args: Any, **_kwargs: Any) -> str:
        return ""

    def finish(self, *_args: Any, **_kwargs: Any) -> str:
        return ""


async def _filed_pointer(
    sessionmaker: Any, user_id: uuid.UUID, task_id: uuid.UUID, language: str
) -> str | None:
    """The sentence that points to the report of a finished research, or ``None`` when it filed none
    (a quick look, or a run that failed to file)."""
    async with sessionmaker() as db:
        found = await ReportStore(db, user_id).by_task(task_id)
    if found is None:
        return None
    title = found.title.replace("[", "(").replace("]", ")")  # a link text keeps no brackets
    return _RESEARCH_POINTER[language].format(title=title, id=found.id)


CLOSING_RETRY_MESSAGE = {
    "de": {"role": "user", "content": "Antworte jetzt direkt auf meine Frage, ohne Vorrede."},
    "en": {"role": "user", "content": "Answer my question now, directly and without preamble."},
}


def _order_of(turn: Turn, arguments: str) -> tuple[check_order.Order | None, str]:
    """The resolved order of a ``look_at_my_systems`` call and the model's note (``what``). Without
    the fixed fields (an old call, or a model that sends only ``what``) there is no order and the
    check runs as before. A machine outside the chat's scope gives a note for the answer instead."""
    focus = chat_tools.focus_from(arguments)
    fields = chat_tools.order_from(arguments)
    if not turn.decide_available:
        return None, focus
    # the model often leaves the machine out: the hub names it from the question or the scope
    filled = check_order.fill_target(fields, turn.decide_available, turn.last_user_text)
    if filled is None:
        return None, focus
    fields = filled
    resolved = check_order.resolve(fields, list(turn.decide_available), turn.last_user_text)
    if isinstance(resolved, check_order.OrderProblem):
        note = (
            f"{resolved.reason}. Say so to the person; do not claim that this machine was checked."
        )
        return None, note
    return resolved, focus


async def _local_result(
    state: Any, user: CurrentUser, turn: Turn, name: str, args: dict[str, Any]
) -> str:
    """What one of the hub's own reads says (T3.6, T3.8): read only, no agent loop. A refusal or a
    missing machine is a sentence, so the answer can say what could not be read."""
    if name == local_tools.ABOUT:  # the application's contract, in words (1.9)
        language = hub_language(turn.language, turn.last_user_text)
        return agent_contract.long(turn.application, language) or "this application has no contract"
    if name == local_tools.RECORD:
        try:
            tid = uuid.UUID(str(args.get("task_id") or ""))
        except ValueError:
            return "no record: the task id is not valid"
        async with state.sessionmaker() as db:
            store = AgentStore(db, user.uuid)  # only this person's tasks are found here
            task = await store.get(tid)
            evid = await store.evidence(tid) if task is not None else []
        card = report.build(task, evid) if task is not None else None
        return local_tools.record_text(
            card, str(args.get("ref") or ""), str(args.get("section") or "")
        )
    caller = replace(Caller.of(user, "agent"), application=turn.application, scope=turn.scope)
    try:
        if name == local_tools.NODE_STATE:
            data = await state.agents.tools.invoke(
                caller,
                TOPCOAT_NODE_ID,
                local_tools.NODE_STATE_CAPABILITY,
                local_tools.node_state_input(args),
            )
            return local_tools.state_text(data)
        node = next(
            (t for t in turn.decide_available if t["capability"] == local_tools.MONITOR_CAPABILITY),
            None,
        )
        if node is None:
            return "no monitor server is in this chat's scope"
        data = await state.agents.tools.invoke(
            caller,
            uuid.UUID(node["node_id"]),
            local_tools.MONITOR_CAPABILITY,
            local_tools.monitor_input(args),
        )
        return local_tools.monitor_text(data)
    except ToolError as exc:
        return f"not retrieved: {exc.message}"


async def _local_answer(
    request: Request,
    user: CurrentUser,
    turn: Turn,
    route: Any,
    name: str,
    arguments: str,
    context_length: int,
    budget: int,
    recalled: list[Recalled],
) -> AsyncIterator[str]:
    """SSE of an answer after one of the hub's own reads: the read runs here and its result is the
    evidence ``[E1]`` of one answer of the model. No agent loop, no new check (T3.6, T3.8)."""
    state = request.app.state
    turn.checked = True
    body = await _local_result(state, user, turn, name, local_tools.args_of(arguments))
    yield _meta_event(
        turn, route.candidates[0].name, None, None, context_length, 0, 0, recalled, "decided"
    )
    _, chunk = turn.activity.add("tool", status="ok", title=name, detail=local_tools.cut(body, 300))
    if chunk:
        yield chunk
    block = f"[E1] {name} (live)\n{body}"
    system = chat.system_prompt(
        turn.user_prompt,
        turn.address,
        turn.capabilities,
        compact=context_length < chat.COMPACT_CONTEXT_TOKENS,
        check=True,
        core=await _core_text(request, user, turn, context_length),
        report_shown=False,
        where=await _where(request, user, turn, route, context_length),
    )
    notes = await _brief_block(request, user, turn) + chat.notes_context(
        [n.prompt_line() for n in recalled]
    )
    prompt = chat.build_prompt(system, turn.history, budget, notes + chat.check_context(block))
    payload: dict[str, Any] = {
        "model": turn.requested,
        "messages": prompt.messages,
        "stream": True,
        "stream_options": {"include_usage": True},
        "cache_prompt": True,
    }
    if turn.temperature is not None:
        payload["temperature"] = turn.temperature
    payload.update(chat.sampling_params(turn.model, state.settings.chat_sampling))
    payload.update(chat.effort_params(turn.model, turn.quality, state.settings.chat_effort))
    closing = await _new_message(request, user, turn, "", status="running")
    turn.message_id = closing.id
    state.runs.alias(turn.run_key, closing.id)
    try:
        upstream = await state.gateway.forward(
            turn.requested, "POST", "/v1/chat/completions", payload, accept="text/event-stream"
        )
    except GatewayError as exc:
        yield _sse_error(exc.code, exc.message, exc.retry_after)
        return
    if upstream.status != 200:
        upstream.close()
        yield _sse_error("model_error", f"the model refused the request ({upstream.status})")
        return
    async with contextlib.aclosing(
        _answer(request, user, turn, upstream, prompt, context_length, recalled, send_meta=False)
    ) as answer:
        async for ev in answer:
            yield ev


async def _checked_answer(
    request: Request,
    user: CurrentUser,
    turn: Turn,
    route: Any,
    checking: str,
    recalled: list[Recalled],
    context_length: int,
    budget: int,
    focus: str = "",
    order: check_order.Order | None = None,
) -> AsyncIterator[str]:
    """SSE of an answer after a server check: ``meta`` (with who will answer and, if the
    preferred machine is not available, that a slower one does), the check's steps
    (``tools_start``, ``tool_step``…, ``tools_done``), then the streamed answer like any other."""
    state = request.app.state
    turn.checked = True
    candidate = route.candidates[0]
    fallback = bool(route.preferred and route.preferred != candidate.name)
    workshop = tool_groups.may_use_workshop(turn.last_user_text, turn.scope)
    step_model = (
        state.settings.step_check_model
        if state.settings.step_check_enabled and turn.quality == "thorough"
        else ""
    )
    limits = quality.check_limits(turn.quality, fallback, workshop, step_check_model=step_model)
    if not fallback and not workshop:  # a slow stand-in or the workshop keep their own room
        limits = turn.policy.apply_limits(limits)
    if order is not None and "host" in order.tools:
        # a Windows read check needs a few calls more than the default (T3.2)
        limits = replace(limits, max_tool_calls=max(limits.max_tool_calls, 12))
    if turn.application in WRITING_APPLICATIONS:
        # a run that writes (task texts, a change set) needs room for the text in one call (live
        # 2026-11-14: three task texts were cut at 2048 tokens, the set kept only the titles); the
        # loop still caps it at an eighth of the slot (agent_loop)
        limits = replace(
            limits,
            max_output_tokens=max(limits.max_output_tokens, WRITE_OUTPUT_TOKENS),
            native_output_tokens=max(limits.native_output_tokens, WRITE_OUTPUT_TOKENS + 2048),
        )
    for_task = turn.task_history or turn.history
    if order is not None:
        task_text = chat_tools.order_prompt(
            order,
            turn.last_user_text,
            for_task,
            turn.application,
            hub_language(turn.language, turn.last_user_text),
        )
    else:
        task_text = chat_tools.task_prompt(turn.last_user_text, for_task, focus)
    if turn.segment_block:
        # the summary of the closed part of the chat: a check that follows up on it needs it too
        task_text += "\n\n(" + turn.segment_block[: chat_tools.TASK_ANSWER_CHARS].strip() + ")"
    # what the second model found in the last answers: the check corrects it instead of repeating
    # it (live 2026-11-14: the hub's notice "the problem described above" reached the check without
    # the problem, which then read the same nodes again and stopped without progress)
    review = await _second_look_block(request, user, turn)
    if review:  # in brackets like every note the hub appends (report_service.question_of)
        task_text += "\n\n(" + review.strip() + ")"
    task_text += chat_tools.earlier_for_task(
        await chat_tools.earlier_check(state.sessionmaker, user.uuid, turn.conversation_id)
    )
    if turn.application == "project":
        # what the person did by hand in Projekte: the run builds on it instead of redoing it
        # (the maintainer, 2026-11-17: tasks made by hand were unknown to the next run)
        try:
            changes = await application_brief.person_changes(state, user.uuid)
        except Exception:
            log.exception("chat: could not read the person's recent project changes")
            changes = ""
        if changes:
            task_text += "\n\n(" + changes + ")"
    if turn.context:
        # the window Clipper is attached to: the check reads what is open there, not something
        # it finds by searching (live 2026-11-12: it searched the notes and read another document)
        async with state.sessionmaker() as db:
            place = await conversation_context.block(db, user.uuid, turn.context, for_task=True)
        if place:
            task_text += "\n\n" + place
    research = turn.application == research_profile.APPLICATION
    if research:
        # Research is not a server check: the stage of the chat is the frame of the run, which
        # has no fixed limits (research_profile.py). The frame applies also to a stand-in node:
        # with the small check room a run made two calls and wrote a list of sources (live,
        # 2026-11-06), so the slot it needs (research_min_context) is asked of every node
        depth = research_profile.CHAT_DEPTH.get(quality.level_of(turn.quality), "default")
        limits = research_profile.limits_for(state.settings, depth, limits)
        task_text = research_profile.depth_line(depth) + "\n" + task_text
    yield _meta_event(
        turn,
        candidate.name,
        route.preferred if fallback else None,
        route.preferred_state if fallback else None,
        context_length,
        0,
        0,
        recalled,
        checking,
        limits.max_tool_calls if fallback else None,
    )
    state.memory.activity.begin()
    run_scope = check_order.scope_for(order, turn.scope) if order is not None else turn.scope
    run_app = turn.application if turn.application in ("project", "research") else None
    if (
        run_app is None
        and turn.application in wiki_tools.APPLICATIONS
        and (
            (turn.context or {}).get("module") == "page.wiki"
            or re.search(r"\bwiki\b", turn.last_user_text, re.I) is not None
        )
    ):
        # Clipper at the Wiki window: the wiki tools answer only a run of an application that may
        # read the wiki (live 2026-11-12: the run had no application, so no wiki). Only then: a
        # Chat check without an application keeps every server tool the person may use, which
        # the narrow ceiling of Chat would take away.
        run_app = turn.application
    check = chat_tools.ToolCheck(
        state.agents,
        state.sessionmaker,
        user,
        task_text,
        turn.requested,
        limits=limits,
        scope=run_scope,
        application=run_app,
    )
    # E3-07 D3: a request that needs a finding is a confirmation bubble, a progress line that
    # lives while the tools run, the report card the hub builds from the check, and a closing
    # bubble with the answer from the model.
    yield chat.sse("bubble", {"message": await _confirm(request, user, turn)})
    progress_id: int | None = None
    trace = CheckTrace(_SilentLog() if research else turn.activity)  # the steps: activity (E7-01)
    live = state.runs.live(turn.run_key) if turn.run_key is not None else None
    # what earlier checks of this application usually took: the browser's step count (2026-11-13)
    typical = (
        await check_steps.typical_calls(
            state.sessionmaker, user.uuid, run_app, limits.max_tool_calls
        )
        if order is not None
        else None
    )
    try:
        try:
            async for event, data in check.events():
                if event == "tools_start" and order is not None:
                    # the order goes to the browser's card and into the activity record (T4.1)
                    data = {
                        **data,
                        "order": check_order.card(order, limits.max_tool_calls, typical),
                    }
                yield chat.sse(event, data)
                # a research run shows one line that it is in progress, not every step of its
                # work (the steps stay in the activity record)
                for chunk in trace.event(event, data):
                    if not research:
                        yield chunk
                if live is not None and event == "tools_start":
                    live.agent_task_id = check.task_id  # the person can steer it while it runs
                elif live is not None and event == "tools_done":
                    live.agent_task_id = None  # it is over: nothing left to steer
                if event == "tools_start":
                    line = _RESEARCH_LINE[chat.hub_language(turn.language, turn.last_user_text)]
                    text = line if research else ""
                    progress = await _new_message(
                        request, user, turn, text, kind="progress", status="running"
                    )
                    progress_id = progress.id
                    state.runs.alias(turn.run_key, progress_id)  # a reload sees it "running"
                    yield chat.sse("progress", {"message_id": progress_id, "text": text})
                elif research:
                    pass  # the line stays "in progress" until the report is there
                elif event == "tool_step" and data.get("kind") == "plan" and progress_id:
                    text = chat_tools.progress_text(
                        data.get("capability"), data.get("node"), data.get("text") or ""
                    )
                    async with state.sessionmaker() as db:
                        await ChatStore(db, user.uuid).update_message(
                            turn.conversation_id, progress_id, content=text
                        )
                        await db.commit()
                    yield chat.sse("progress", {"message_id": progress_id, "text": text})
        except (asyncio.CancelledError, GeneratorExit):
            spawn(check.cancel())  # the user pressed stop: the check stops with it
            raise
        if check.task_id is None:  # it never started
            done = check.done_event()
            yield chat.sse("tools_done", done)
            for chunk in trace.event("tools_done", done):
                yield chunk
        if progress_id is not None:  # the result is here: the line goes
            await _drop_message(request, user, turn, progress_id)
            yield chat.sse("progress_done", {"message_id": progress_id})
            progress_id = None
        card = report.build(check.result.task, check.result.evidence, error=check.result.error)
        pointer = None
        if research and check.task_id is not None:
            pointer = await _filed_pointer(
                state.sessionmaker,
                user.uuid,
                check.task_id,
                chat.hub_language(turn.language, turn.last_user_text),
            )
        if pointer is not None:
            # the document in the Ablage is the answer: the chat says where it is, not the card
            card = None
            row = await _new_message(request, user, turn, pointer, kind="say", status="complete")
            yield chat.sse("bubble", {"message": message_view(row)})
        prepared: list[Any] = []
        waiting: list[Any] = []  # what waits for the person's click: single changes and sets
        if card is not None and check.task_id is not None and turn.application == "project":
            prepared, waiting = await _prepared_changes(
                state.sessionmaker, state.settings.hub_timezone, user.uuid, check.task_id
            )
            if prepared:  # the card shows them with their buttons (their state is read live)
                card = {**card, "changes_for": str(check.task_id)}
        offered: list[dict[str, Any]] = []
        if card is not None and check.task_id is not None:
            offered = await _task_proposals(state, user.uuid, check.task_id)
            if offered:  # the card shows them with their buttons (their state is read live)
                card = {**card, "proposals_for": str(check.task_id)}
        if card is not None:
            row = await _new_message(
                request,
                user,
                turn,
                report.text(card),
                kind="report",
                report=card,
                agent_task_id=check.task_id,
                status="complete",
            )
            yield chat.sse("report", {"message": message_view(row)})
        closing = await _new_message(request, user, turn, "", status="running")
        turn.message_id = closing.id
        state.runs.alias(turn.run_key, closing.id)
        # the evidence is what the answer needs, not half the window: a prompt of 8k tokens costs a
        # slow node a minute before the first word (E3-04)
        block = chat_tools.check_block(
            check.result, min(budget * chat.CHARS_PER_TOKEN // 2, CHECK_BLOCK_CHARS)
        )
        pending_sets = [w for w in waiting if isinstance(w, ProjectChangeSet)]
        if pending_sets:
            # what waits for the click, as the hub has it: the answer describes this, not what the
            # run meant to write (live 2026-11-14: "detailed instructions" for three new titles)
            block += (
                "\n\nPrepared and waiting for the person's click (the hub's record): "
                + "; ".join(
                    f"'{s.title}': {project_changesets.summary_of(s)}" for s in pending_sets
                )
                + ". Describe exactly this; a set that changes no body wrote no text."
            )
        elif (
            not waiting and not any(p.get("status") == "pending" for p in offered)
        ) and chat_tools.tried_to_write(check.result, turn.application in WRITING_APPLICATIONS):
            # nothing waits for a click: the answer must not say it does (live 2026-11-15: "der
            # Batch ist vorbereitet" over a run that prepared nothing; 7 of 70 answers in 5 days)
            block += "\n\n" + chat_tools.NOTHING_PREPARED
        system = chat.system_prompt(
            turn.user_prompt,
            turn.address,
            turn.capabilities,
            compact=context_length < chat.COMPACT_CONTEXT_TOKENS,
            check=True,
            core=await _core_text(request, user, turn, context_length),
            report_shown=card is not None or pointer is not None,
            where=await _where(request, user, turn, route, context_length),
        )
        notes = await _brief_block(request, user, turn) + chat.notes_context(
            [n.prompt_line() for n in recalled]
        )
        if turn.segment_block:  # as in an answer without a check (_pipeline)
            cap = max(budget // 4, 0) * chat.CHARS_PER_TOKEN
            notes = turn.segment_block[:cap] + notes
        notes = review + notes
        prompt = chat.build_prompt(system, turn.history, budget, notes + chat.check_context(block))
        payload: dict[str, Any] = {
            "model": turn.requested,
            "messages": prompt.messages,
            "stream": True,
            "stream_options": {"include_usage": True},
            "cache_prompt": True,
        }
        if card is not None or pointer is not None:
            payload["max_tokens"] = chat.CLOSING_MAX_TOKENS
        if turn.temperature is not None:
            payload["temperature"] = turn.temperature
        payload.update(chat.sampling_params(turn.model, state.settings.chat_sampling))
        payload.update(chat.effort_params(turn.model, turn.quality, state.settings.chat_effort))
        try:
            upstream = await state.gateway.forward(
                turn.requested, "POST", "/v1/chat/completions", payload, accept="text/event-stream"
            )
        except GatewayError as exc:
            log.warning("chat: closing bubble: %s on %s: %s", exc.code, turn.requested, exc.message)
            if card is not None:  # the report stands; the hub says what it shows
                turn.message_id = None
                await _drop_message(request, user, turn, closing.id)
                async for ev in _closing_fallback(request, user, turn, card):
                    yield ev
                return
            yield _sse_error(exc.code, exc.message, exc.retry_after)
            return
        if upstream.status != 200:
            detail = b"".join([c async for c in upstream.body])[:2000].decode("utf-8", "replace")
            upstream.close()
            log.warning("chat: model answered %s: %s", upstream.status, detail)
            if card is not None and not is_context_full(detail):
                turn.message_id = None
                await _drop_message(request, user, turn, closing.id)
                async for ev in _closing_fallback(request, user, turn, card):
                    yield ev
                return
            if is_context_full(detail):
                yield _sse_error(
                    "context_full", CONTEXT_FULL_MESSAGE, state.settings.gateway_retry_after_seconds
                )
            else:
                yield _sse_error(
                    "model_error", f"the model refused the request ({upstream.status})"
                )
            return
        after_done = None
        if turn.second_look and turn.verify_model and check.task_id is not None:

            def after_done(message: dict[str, Any]) -> AsyncIterator[str]:
                return _verify_answer(request, user, turn, message, block)

        async with contextlib.aclosing(
            _answer(
                request,
                user,
                turn,
                upstream,
                prompt,
                context_length,
                recalled,
                send_meta=False,
                agent_task_id=check.task_id,
                after_done=after_done,
                closing=card is not None or pointer is not None,
                scope=_scope_guard(turn, check.result.task),
                change_facts=_change_facts(
                    [*waiting, *(p for p in offered if p.get("status") == "pending")],
                    check.result.task,
                ),
                prepared_sets=[p for p in waiting if isinstance(p, ProjectChangeSet)],
                refused=check.result.reason if check.task_id is None else "",
            )
        ) as answer:
            async for ev in answer:
                yield ev
        if turn.empty_answer and card is not None:
            # the model said nothing after the report: once more with a short instruction, and if
            # that stays empty too, the report stands alone (B-A)
            turn.empty_answer = False
            hint = CLOSING_RETRY_MESSAGE[hub_language(turn.language, turn.last_user_text)]
            retry = {**payload, "messages": [*prompt.messages, hint]}
            again = await _new_message(request, user, turn, "", status="running")
            turn.message_id = again.id
            state.runs.alias(turn.run_key, again.id)
            try:
                upstream = await state.gateway.forward(
                    turn.requested,
                    "POST",
                    "/v1/chat/completions",
                    retry,
                    accept="text/event-stream",
                )
            except GatewayError as exc:
                log.warning(
                    "chat: closing retry: %s on %s: %s", exc.code, turn.requested, exc.message
                )
                upstream = None
            if upstream is not None and upstream.status == 200:
                async with contextlib.aclosing(
                    _answer(
                        request,
                        user,
                        turn,
                        upstream,
                        prompt,
                        context_length,
                        recalled,
                        send_meta=False,
                        agent_task_id=check.task_id,
                        closing=True,
                    )
                ) as answer:
                    async for ev in answer:
                        yield ev
            elif upstream is not None:
                upstream.close()
            if turn.empty_answer or upstream is None or upstream.status != 200:
                if turn.message_id is not None:
                    await _drop_message(request, user, turn, turn.message_id)
                    turn.message_id = None
                async for ev in _closing_fallback(request, user, turn, card):
                    yield ev
    finally:
        state.memory.activity.end()
        if progress_id is not None:  # stopped or broken while the line was showing
            await asyncio.shield(_drop_message(request, user, turn, progress_id))


async def _verify_answer(
    request: Request, user: CurrentUser, turn: Turn, message: dict[str, Any], block: str
) -> AsyncIterator[str]:
    """The thorough level's second look (E3-04): the big model reads the answer against the
    evidence once more. The result is stored with the answer and sent as ``verification``;
    """
    state = request.app.state
    # a model other than the answering one looks at the answer where the nodes serve another;
    # otherwise the same model does and the verification says so
    reviewer = turn.verify_model or turn.requested
    same_model = split_model(reviewer)[0] == turn.model
    if same_model:
        try:
            other = chat_review.pick_reviewer(
                await state.gateway.list_models(), turn.model, "", None, message.get("node")
            )
        except Exception:
            other = None
        if other:  # it may be the author's own model on another node: then it says so
            reviewer, same_model = other, split_model(other)[0] == turn.model
    shown = split_model(reviewer)[0]  # the person sees the model; the call is pinned to its node
    item_id, chunk = turn.activity.add("check", shown, data={"second_look": True})
    if chunk:
        yield chunk
    yield chat.sse("verifying", {"model": shown})
    result = await quality.verify(
        state.gateway,
        reviewer,
        turn.last_user_text,
        message["content"],
        block,
        min_context=state.settings.agent_min_context,
    )

    async def keep(**fields: Any) -> None:
        # the answer was stored before the second look: its activity gets the look added
        try:
            async with state.sessionmaker() as db:
                await ChatStore(db, user.uuid).update_message(
                    turn.conversation_id,
                    message["id"],
                    activity=turn.activity.snapshot(),
                    **fields,
                )
                await db.commit()
        except Exception:
            log.exception("could not store the verification")

    if result is None:
        chunk = turn.activity.finish(item_id, "failed", "unavailable")
        if chunk:
            yield chunk
        await keep()
        yield chat.sse("verification", {"message_id": message["id"], "available": False})
        return
    view = {
        **result.view(),
        "model": shown,
        "same_model": same_model,
        "at": datetime.now(UTC).isoformat(),
    }
    chunk = turn.activity.finish(item_id, "ok" if result.ok else "failed")
    if chunk:
        yield chunk
    await keep(verification=view)
    yield chat.sse(
        "verification",
        {
            "message_id": message["id"],
            "available": True,
            **view,
        },
    )


class Held:
    """A Gründlich answer whose text waits for its check (PLAN 2a). While it is not released the
    answer's ``delta`` events are not shown; the activity around them is."""

    def __init__(self, messages: list[dict[str, Any]]) -> None:
        self.messages = messages  # the prompt the answer was made from, for a revision
        self.released = False
        self.text = ""
        self.message: dict[str, Any] | None = None

    def hides(self, chunk: str) -> bool:
        return not self.released and chunk.startswith("event: delta\n")


def _held_for(turn: Turn, prompt: chat.Prompt, on_tool: Any, settings: Any) -> Held | None:
    """An answer is held back when it has a second look and no tool call can turn it into a server
    check (``on_tool``), at a stage with revisions (``effort_stages``, the live setting)."""
    if on_tool is not None or not turn.second_look or not turn.verify_model:
        return None
    if effort_stages.stage_for(turn.quality, settings).second_look_rounds <= 0:
        return None
    return Held(prompt.messages)


async def _revise(
    state: Any, turn: Turn, held: Held, draft: str, findings: list[str]
) -> str | None:
    """The answer again, with the findings of its check, from the same prompt. None when the model
    cannot answer; the draft then stands."""
    payload = {
        "model": turn.model,
        "stream": False,
        "temperature": 0.2,
        "max_tokens": 2048,
        "messages": [
            *held.messages,
            {"role": "assistant", "content": draft},
            {"role": "user", "content": answer_gate.revision_note(findings)},
        ],
    }
    try:
        data = await state.gateway.call(
            # with images (D125) only where the model sees them
            turn.requested if getattr(turn, "vision_history", None) is not None else turn.model,
            "/v1/chat/completions",
            payload,
            min_context=state.settings.agent_min_context,
            label="revise",
        )
    except GatewayError as exc:
        log.info("revision not made: %s", exc.message)
        return None
    try:
        text = data["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError):
        return None
    return text.strip() if isinstance(text, str) and text.strip() else None


async def _pick_checker(state: Any, turn: Turn, node: str | None) -> str:
    """The model that checks: the second look's own model, or another model than the answering one
    when the nodes serve one (the same rule as ``_verify_answer``)."""
    reviewer = turn.verify_model or turn.requested
    if split_model(reviewer)[0] == turn.model:
        try:
            other = chat_review.pick_reviewer(
                await state.gateway.list_models(), turn.model, "", None, node
            )
        except Exception:
            other = None
        if other:
            reviewer = other
    return reviewer


async def _finalize_held(
    request: Request,
    user: CurrentUser,
    turn: Turn,
    held: Held,
    text: str,
    meta_view: dict[str, Any],
) -> AsyncIterator[str]:
    """The check of a held answer, its revisions, then the one text that is shown and stored.
    The verdict is kept with the message, as the second look is kept (``_verify_answer``)."""
    state = request.app.state
    reviewer = await _pick_checker(state, turn, meta_view.get("node"))
    shown = split_model(reviewer)[0]
    item_id, chunk = turn.activity.add("check", shown, data={"second_look": True, "held": True})
    if chunk:
        yield chunk
    yield chat.sse("verifying", {"model": shown})
    draft = {"text": text}

    async def produce(findings: list[str] | None) -> str:
        if findings is None:
            return draft["text"]
        revised = await _revise(state, turn, held, draft["text"], findings)
        if revised is not None:
            draft["text"] = revised
        return draft["text"]

    async def check(answer: str) -> quality.Verification | None:
        return await quality.verify(
            state.gateway,
            reviewer,
            turn.last_user_text,
            answer,
            "",
            min_context=state.settings.agent_min_context,
        )

    rounds = effort_stages.stage_for(turn.quality, state.settings).second_look_rounds
    outcome = await answer_gate.gate(produce, check, rounds)
    if outcome.checked:
        chunk = turn.activity.finish(item_id, "ok" if outcome.ok else "failed")
    else:
        chunk = turn.activity.finish(item_id, "failed", "unavailable")
    if chunk:
        yield chunk
    held.released = True
    held.text = outcome.text
    yield chat.sse("delta", {"text": outcome.text})
    message = await _store_answer(request, user, turn, outcome.text, **meta_view)
    held.message = message
    if not outcome.checked:
        yield chat.sse("verification", {"message_id": message["id"], "available": False})
        return
    view = {
        "ok": outcome.ok,
        "issues": outcome.issues,
        "model": shown,
        "same_model": split_model(reviewer)[0] == turn.model,
        "revisions": outcome.answers - 1,
        "at": datetime.now(UTC).isoformat(),
    }
    try:
        async with state.sessionmaker() as db:
            await ChatStore(db, user.uuid).update_message(
                turn.conversation_id,
                message["id"],
                activity=turn.activity.snapshot(),
                verification=view,
            )
            await db.commit()
    except Exception:
        log.exception("could not store the verification")
    yield chat.sse("verification", {"message_id": message["id"], "available": True, **view})


def _sse_error(code: str, message: str, retry_after: int | None = None) -> str:
    data: dict[str, Any] = {"code": code, "message": message, "retry": True}
    if retry_after:
        data["retry_after"] = retry_after
    return chat.sse("error", data)


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
