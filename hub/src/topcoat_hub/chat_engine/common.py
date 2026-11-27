"""Helpers the chat routes and the chat engine share (issue #205): the error responses, the view
of a message, the conversation as the prompt and a check read it, the chat defaults."""

import logging
import re
import uuid
from datetime import UTC
from typing import Any

from fastapi import HTTPException, Request
from fastapi.responses import JSONResponse
from sqlalchemy.ext.asyncio import AsyncSession

from topcoat_hub import (
    chat,
    chat_tools,
    quality,
)
from topcoat_hub.chat_store import (
    ChatStore,
)
from topcoat_hub.models import (
    Conversation,
    Message,
)

log = logging.getLogger(__name__)


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


CONFIRMATIONS = {"de": "Schau ich mir an.", "en": "On it — I'll take a look."}
hub_language = chat.hub_language


def _sse_error(code: str, message: str, retry_after: int | None = None) -> str:
    data: dict[str, Any] = {"code": code, "message": message, "retry": True}
    if retry_after:
        data["retry_after"] = retry_after
    return chat.sse("error", data)
