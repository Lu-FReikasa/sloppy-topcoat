"""Data access for the reviews of chat answers (D20: every statement filters by ``user_id``)."""

import uuid
from collections.abc import Sequence
from typing import Any

from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from topcoat_hub.models import ChatReview


class ReviewStore:
    def __init__(self, db: AsyncSession, user_id: uuid.UUID) -> None:
        self.db = db
        self.user_id = user_id

    async def add(
        self,
        conversation_id: uuid.UUID,
        message_id: int,
        verdict: str,
        findings: list[dict[str, Any]],
        reviewer: tuple[str | None, str | None],
        author: tuple[str | None, str | None],
    ) -> ChatReview:
        row = ChatReview(
            user_id=self.user_id,
            conversation_id=conversation_id,
            message_id=message_id,
            verdict=verdict,
            findings=findings,
            reviewer_model=reviewer[0],
            reviewer_node=reviewer[1],
            author_model=author[0],
            author_node=author[1],
            seen=verdict == "ok",
        )
        self.db.add(row)
        await self.db.flush()
        return row

    async def remove(self, message_id: int) -> None:
        """Drop the review of an answer (a model's look on request replaces the hub's check)."""
        await self.db.execute(
            delete(ChatReview).where(
                ChatReview.user_id == self.user_id, ChatReview.message_id == message_id
            )
        )

    async def has(self, message_id: int) -> bool:
        return (
            await self.db.scalar(
                select(ChatReview.id).where(
                    ChatReview.user_id == self.user_id, ChatReview.message_id == message_id
                )
            )
        ) is not None

    async def pending_for_agent(self, conversation_id: uuid.UUID) -> Sequence[ChatReview]:
        """Findings of this chat the agent has not been given yet, oldest first."""
        rows = await self.db.scalars(
            select(ChatReview)
            .where(
                ChatReview.user_id == self.user_id,
                ChatReview.conversation_id == conversation_id,
                ChatReview.verdict != "ok",
                ChatReview.told_agent.is_(False),
            )
            .order_by(ChatReview.message_id)
        )
        return rows.all()

    async def mark_told(self, review_ids: Sequence[uuid.UUID]) -> None:
        if review_ids:
            await self.db.execute(
                update(ChatReview)
                .where(ChatReview.user_id == self.user_id, ChatReview.id.in_(review_ids))
                .values(told_agent=True)
            )

    async def verdicts(self, conversation_id: uuid.UUID) -> dict[int, str]:
        """The verdict of every reviewed answer of a conversation, ``ok`` included."""
        rows = await self.db.execute(
            select(ChatReview.message_id, ChatReview.verdict).where(
                ChatReview.user_id == self.user_id, ChatReview.conversation_id == conversation_id
            )
        )
        return {m: v for m, v in rows.all()}

    async def for_conversation(self, conversation_id: uuid.UUID) -> Sequence[ChatReview]:
        rows = await self.db.scalars(
            select(ChatReview)
            .where(
                ChatReview.user_id == self.user_id,
                ChatReview.conversation_id == conversation_id,
                ChatReview.verdict != "ok",
            )
            .order_by(ChatReview.message_id)
        )
        return rows.all()

    async def unseen(self, limit: int = 50) -> Sequence[ChatReview]:
        """Findings the user has not looked at, evidenced errors first."""
        rows = await self.db.scalars(
            select(ChatReview)
            .where(ChatReview.user_id == self.user_id, ChatReview.seen.is_(False))
            .order_by(ChatReview.verdict.asc(), ChatReview.created_at.desc())
            .limit(limit)
        )
        return rows.all()

    async def mark_seen(self, review_id: uuid.UUID) -> bool:
        res = await self.db.execute(
            update(ChatReview)
            .where(ChatReview.id == review_id, ChatReview.user_id == self.user_id)
            .values(seen=True)
        )
        return bool(res.rowcount)
