"""Data access for the digests of finished chats (D20: every statement filters by ``user_id``)."""

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from topcoat_hub.core_rules import RecentChat
from topcoat_hub.models import ChatDigest, Conversation


class DigestStore:
    def __init__(self, db: AsyncSession, user_id: uuid.UUID) -> None:
        self.db = db
        self.user_id = user_id

    async def get(self, conversation_id: uuid.UUID) -> ChatDigest | None:
        return await self.db.scalar(
            select(ChatDigest).where(
                ChatDigest.user_id == self.user_id, ChatDigest.conversation_id == conversation_id
            )
        )

    async def put(
        self,
        conversation_id: uuid.UUID,
        last_message_id: int,
        summary: str,
        author: tuple[str | None, str | None],
        now: datetime,
        refs: list[dict[str, Any]] | None = None,
    ) -> ChatDigest:
        """Pass 1: the author's summary (new, or overwriting the one of an earlier rest) with the
        statements behind it."""
        row = await self.get(conversation_id)
        fields: dict[str, Any] = {
            "last_message_id": last_message_id,
            "summary": summary,
            "summary_refs": refs,
            "passes": 1,
            "status": "summarised",
            "learned": 0,
            "author_model": author[0],
            "author_node": author[1],
            "checker_model": None,
            "checker_node": None,
            "updated_at": now,
        }
        if row is None:
            row = ChatDigest(user_id=self.user_id, conversation_id=conversation_id, **fields)
            self.db.add(row)
        else:
            for key, value in fields.items():
                setattr(row, key, value)
        await self.db.flush()
        return row

    async def recent(
        self,
        application: str,
        exclude: uuid.UUID | None,
        limit: int,
        days: int,
        default_application: str = "chat",
    ) -> list[RecentChat]:
        """The running summaries of the latest chats of one program, newest first, for the core
        block of a new chat. Only this program's chats (a program's memory is its own, D74), not
        the chat that asks, nothing older than ``days``; a digest without a verified statement
        has nothing to say and is skipped."""
        rows = await self.db.execute(
            select(ChatDigest, Conversation.title)
            .join(Conversation, Conversation.id == ChatDigest.conversation_id)
            .where(
                ChatDigest.user_id == self.user_id,
                Conversation.user_id == self.user_id,
                func.coalesce(Conversation.application, default_application) == application,
                ChatDigest.updated_at > datetime.now(UTC) - timedelta(days=days),
                *([ChatDigest.conversation_id != exclude] if exclude is not None else []),
            )
            .order_by(ChatDigest.updated_at.desc())
            .limit(limit * 3)  # some rows will have nothing verified
        )
        out: list[RecentChat] = []
        for digest, title in rows:
            statements = tuple(
                str(r["text"])
                for r in (digest.summary_refs or [])
                if isinstance(r, dict) and r.get("verified") and r.get("text")
            )
            if not statements:
                continue
            out.append(
                RecentChat(
                    id=str(digest.conversation_id),
                    title=title or "",
                    day=digest.updated_at.date().isoformat(),
                    last_message_id=digest.last_message_id,
                    statements=statements,
                )
            )
            if len(out) == limit:
                break
        return out
