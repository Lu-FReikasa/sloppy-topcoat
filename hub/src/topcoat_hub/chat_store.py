"""Data access for conversations, messages and per-user chat settings (D8, D20).

Everything here belongs to one person. A :class:`ChatStore` is created for exactly one user and
**every statement it runs filters by that user's id**; asking for another user's conversation
answers "not found", never the data. The GUI/API layer cannot forget the scoping because it
has no other way to reach these tables (there is no query helper that takes a bare id).
"""

import hashlib
import uuid
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import delete, func, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from topcoat_hub import agent_contract
from topcoat_hub.models import (
    HOUSEHOLD_ID,
    Conversation,
    HubSetting,
    Message,
    Persona,
    PromptSnapshot,
    UserSettings,
)
from topcoat_hub.tool_groups import clean_scope

TITLE_MAX = 200
DEFAULT_TITLE = ""


def is_free_chat(application: str | None) -> bool:
    """The free chat (no application, or "chat") is the only place a server choice is inherited
    from the user's last choice and remembered for the next chat."""
    return not application or application == "chat"


def _like_escape(text: str) -> str:
    return text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def title_from(text: str) -> str:
    """A conversation title from its first message: first line, whitespace collapsed."""
    line = " ".join(text.strip().split("\n", 1)[0].split())
    return line[:60] + ("…" if len(line) > 60 else "")


class ChatStore:
    def __init__(self, db: AsyncSession, user_id: uuid.UUID) -> None:
        self.db = db
        self.user_id = user_id

    # --- conversations -------------------------------------------------------------------

    async def list_conversations(self, q: str | None = None, limit: int = 200) -> Sequence[Any]:
        """The user's conversations, most recently used first, with their message count;
        ``q`` matches the title (case-insensitive substring)."""
        stmt = (
            select(Conversation, func.count(Message.id))
            .outerjoin(
                Message,
                (Message.conversation_id == Conversation.id) & (Message.user_id == self.user_id),
            )
            .where(Conversation.user_id == self.user_id)
            .group_by(Conversation.id)
            .order_by(Conversation.updated_at.desc(), Conversation.id)
            .limit(limit)
        )
        if q and q.strip():
            stmt = stmt.where(Conversation.title.ilike(f"%{_like_escape(q.strip())}%", escape="\\"))
        return (await self.db.execute(stmt)).all()

    async def continue_conversation(self, prev: Conversation) -> Conversation:
        """The next section of a continuous conversation: a chat of its own that keeps the quality,
        servers and persona of the one it continues. It does **not** keep a model or a node that was
        picked in the chat before: a pick belongs to the chat it was made in, and the one who
        decides for a new section is the table of applications (a pick that is carried on from
        section to section outlives the reason it was made for, and ends in an error when its
        machine is gone)."""
        conv = Conversation(
            user_id=self.user_id,
            title=DEFAULT_TITLE,
            quality=prev.quality,
            tool_scope=prev.tool_scope,
            persona_ref=prev.persona_ref,
            application=prev.application,
            continues_id=prev.id,
        )
        self.db.add(conv)
        await self.db.flush()
        await self.db.refresh(conv)
        return conv

    async def create_conversation(
        self,
        title: str = DEFAULT_TITLE,
        model: str | None = None,
        node: str | None = None,
        tool_scope: dict[str, Any] | None = None,
        persona_ref: str | None = None,
        application: str | None = None,
        quality: str | None = None,
        context: dict[str, Any] | None = None,
    ) -> Conversation:
        """``application``: the workspace the conversation is started in (docs/PLATFORM.md).
        ``tool_scope`` None: the user's last choice of servers and groups (E3-04), the start of
        new conversations; the internet switch starts as the user's default. ``persona_ref`` None:
        the persona the user has chosen as their default *now* — pinned to the conversation, so a
        later change of the default leaves it alone (E3-06); without a default of the person's own,
        the persona the application's contract names (D118: the Butler for the secretary)."""
        # Only the free chat inherits the user's last server choice. A fixed application (admin,
        # project, research, dev) starts from its own default, so an old choice never narrows
        # or widens its run (pruefauftrag T2.1).
        if tool_scope is None and is_free_chat(application):
            tool_scope = await self.last_tool_scope()
        if persona_ref is None:
            settings = await self.get_settings()
            persona_ref = str(settings.persona_id) if settings and settings.persona_id else None
        if persona_ref is None:
            persona_ref = await self._contract_persona(application)
        conv = Conversation(
            user_id=self.user_id,
            title=title[:TITLE_MAX],
            model=model or None,
            node=node or None,
            tool_scope=clean_scope(tool_scope),
            persona_ref=persona_ref,
            application=application or None,
            context=context or None,
            quality=quality or None,
        )
        self.db.add(conv)
        await self.db.flush()
        await self.db.refresh(conv)
        return conv

    async def running_conversations(self) -> set[uuid.UUID]:
        """The user's conversations in which an answer is being generated right now."""
        rows = await self.db.scalars(
            select(Message.conversation_id)
            .where(Message.user_id == self.user_id, Message.status == "running")
            .distinct()
        )
        return set(rows)

    async def get_conversation(self, conversation_id: uuid.UUID) -> Conversation | None:
        return await self.db.scalar(
            select(Conversation).where(
                Conversation.id == conversation_id, Conversation.user_id == self.user_id
            )
        )

    async def set_state(
        self, conversation_id: uuid.UUID, pinned: bool | None = None, done: bool | None = None
    ) -> Conversation | None:
        """Pin or unpin, mark done or open again; the conversation's activity time stays (a
        click is no activity). None if the conversation is not the person's."""
        conv = await self.get_conversation(conversation_id)
        if conv is None:
            return None
        now = datetime.now(UTC)
        if pinned is not None:
            conv.pinned_at = now if pinned else None
        if done is not None:
            conv.done_at = now if done else None
            if done:
                conv.pinned_at = None  # done is done: it leaves Start
        await self.db.flush()
        return conv

    async def update_conversation(
        self, conversation_id: uuid.UUID, **fields: Any
    ) -> Conversation | None:
        """Change title / model / node (only these); None if the conversation is not the
        user's."""
        allowed = {
            k: v
            for k, v in fields.items()
            if k in ("title", "model", "node", "quality", "tool_scope", "persona_ref")
        }
        if "tool_scope" in allowed:
            allowed["tool_scope"] = clean_scope(allowed["tool_scope"])
            conv = await self.get_conversation(conversation_id)
            if conv is not None and is_free_chat(conv.application):
                await self.remember_tool_scope(allowed["tool_scope"])
        if "title" in allowed:
            allowed["title"] = str(allowed["title"])[:TITLE_MAX]
        for key in ("model", "node", "quality", "persona_ref"):
            if key in allowed:
                allowed[key] = allowed[key] or None
        if allowed:
            await self.db.execute(
                update(Conversation)
                .where(Conversation.id == conversation_id, Conversation.user_id == self.user_id)
                .values(**allowed, updated_at=func.now())
            )
        conv = await self.get_conversation(conversation_id)
        if conv is not None:
            await self.db.refresh(conv)
        return conv

    async def delete_conversation(self, conversation_id: uuid.UUID) -> bool:
        result = await self.db.execute(
            delete(Conversation)
            .where(Conversation.id == conversation_id, Conversation.user_id == self.user_id)
            .returning(Conversation.id)
        )
        return result.scalar_one_or_none() is not None

    # --- messages ------------------------------------------------------------------------

    async def messages(self, conversation_id: uuid.UUID) -> list[Message]:
        """Messages of one of the user's conversations, oldest first (empty for any other)."""
        rows = await self.db.scalars(
            select(Message)
            .where(Message.conversation_id == conversation_id, Message.user_id == self.user_id)
            .order_by(Message.id)
        )
        return list(rows)

    async def get_message(self, message_id: int) -> Message | None:
        return await self.db.scalar(
            select(Message).where(Message.id == message_id, Message.user_id == self.user_id)
        )

    async def add_message(
        self, conversation_id: uuid.UUID, role: str, content: str, **meta: Any
    ) -> Message:
        """Append a message to one of the user's conversations; LookupError for any other."""
        if await self.get_conversation(conversation_id) is None:
            raise LookupError(conversation_id)
        allowed = {
            k: v
            for k, v in meta.items()
            if k
            in (
                "model",
                "node",
                "fallback_from",
                "status",
                "error",
                "prompt_tokens",
                "completion_tokens",
                "first_token_ms",
                "duration_ms",
                "context_trimmed",
                "memory_refs",
                "agent_task_id",
                "prompt_hash",
                "persona_ref",
                "kind",
                "report",
                "activity",
            )
        }
        msg = Message(
            conversation_id=conversation_id,
            user_id=self.user_id,
            role=role,
            content=content,
            **allowed,
        )
        self.db.add(msg)
        await self.db.flush()
        await self.db.execute(
            update(Conversation)
            .where(Conversation.id == conversation_id, Conversation.user_id == self.user_id)
            .values(updated_at=func.now())
        )
        await self.db.refresh(msg)
        return msg

    async def update_message(
        self, conversation_id: uuid.UUID, message_id: int, **fields: Any
    ) -> Message | None:
        """Change an assistant message of one of the user's conversations (continuing a
        truncated answer); None if it is not the user's."""
        allowed = {
            k: v
            for k, v in fields.items()
            if k
            in (
                "content",
                "status",
                "error",
                "prompt_tokens",
                "completion_tokens",
                "duration_ms",
                "context_trimmed",
                "verification",
                # an answer written into its placeholder (E3-07)
                "model",
                "node",
                "fallback_from",
                "first_token_ms",
                "memory_refs",
                "agent_task_id",
                "prompt_hash",
                "persona_ref",
                "report",
                "kind",
                "activity",
            )
        }
        if allowed:
            await self.db.execute(
                update(Message)
                .where(
                    Message.id == message_id,
                    Message.conversation_id == conversation_id,
                    Message.user_id == self.user_id,
                    Message.role == "assistant",
                )
                .values(**allowed)
            )
            await self.db.execute(
                update(Conversation)
                .where(Conversation.id == conversation_id, Conversation.user_id == self.user_id)
                .values(updated_at=func.now())
            )
        msg = await self.db.scalar(
            select(Message).where(
                Message.id == message_id,
                Message.conversation_id == conversation_id,
                Message.user_id == self.user_id,
            )
        )
        if msg is not None:
            await self.db.refresh(msg)
        return msg

    async def set_feedback(
        self, conversation_id: uuid.UUID, message_id: int, value: int | None
    ) -> Message | None:
        """The user's verdict on an answer (1 helped, -1 did not fit, None takes it back)."""
        await self.db.execute(
            update(Message)
            .where(
                Message.id == message_id,
                Message.conversation_id == conversation_id,
                Message.user_id == self.user_id,
                Message.role == "assistant",
            )
            .values(feedback=value)
        )
        msg = await self.get_message(message_id)
        if msg is None or msg.conversation_id != conversation_id:
            return None
        await self.db.refresh(msg)
        return msg

    # --- prompt snapshots (E3-06, 11a) ----------------------------------------------------------

    async def save_snapshot(
        self, text: str, persona_ref: str | None, core_version: str | None
    ) -> str:
        """Keep the system prompt as it was sent, once per user and SHA-256; returns the hash."""
        digest = hashlib.sha256(text.encode()).hexdigest()
        await self.db.execute(
            insert(PromptSnapshot)
            .values(
                user_id=self.user_id,
                hash=digest,
                text=text,
                persona_ref=persona_ref,
                core_version=core_version,
            )
            .on_conflict_do_nothing(index_elements=["user_id", "hash"])
        )
        return digest

    async def get_snapshot(self, digest: str) -> PromptSnapshot | None:
        return await self.db.scalar(
            select(PromptSnapshot).where(
                PromptSnapshot.user_id == self.user_id, PromptSnapshot.hash == digest
            )
        )

    async def delete_message(self, conversation_id: uuid.UUID, message_id: int) -> bool:
        result = await self.db.execute(
            delete(Message)
            .where(
                Message.id == message_id,
                Message.conversation_id == conversation_id,
                Message.user_id == self.user_id,
            )
            .returning(Message.id)
        )
        return result.scalar_one_or_none() is not None

    # --- settings ------------------------------------------------------------------------

    async def last_tool_scope(self) -> dict[str, Any] | None:
        """The tool scope the user chose last (new conversations start with it)."""
        settings = await self.get_settings()
        return clean_scope(settings.tool_scope) if settings else None

    async def remember_tool_scope(self, scope: dict[str, Any] | None) -> None:
        """The servers and groups the user chose last. The internet switch is per conversation
        (its default is a setting), so it is not part of what new conversations inherit."""
        if scope is not None:
            scope = clean_scope({**scope, "internet": None, "memory": None})
        await self.db.execute(
            insert(UserSettings)
            .values(user_id=self.user_id, tool_scope=scope)
            .on_conflict_do_update(
                index_elements=["user_id"], set_={"tool_scope": scope, "updated_at": func.now()}
            )
        )

    async def _contract_persona(self, application: str | None) -> str | None:
        """The persona the contract of ``application`` names (D118), as the household's active
        template of that key; ``None`` when the application has no contract or the template is
        gone or archived. Only used when the person has chosen no default of their own; a
        conversation a program opens without naming an application gets none."""
        contract = agent_contract.get(application) if application else None
        if contract is None:
            return None
        found = await self.db.scalar(
            select(Persona.id).where(
                Persona.user_id == HOUSEHOLD_ID,
                Persona.scope == "household",
                Persona.key == contract.persona,
                Persona.status == "active",
            )
        )
        return str(found) if found is not None else None

    async def get_settings(self) -> UserSettings | None:
        return await self.db.scalar(
            select(UserSettings).where(UserSettings.user_id == self.user_id)
        )

    async def set_settings(
        self,
        system_prompt: str | None,
        temperature: float | None,
        address: str | None = None,
        memory_enabled: bool = True,
        chat_quality: str | None = None,
        internet: bool | None = None,
    ) -> UserSettings:
        """Upsert the user's settings. ``chat_quality`` and ``internet`` None keep the stored
        value."""
        current = await self.get_settings()
        if internet is None:
            internet = current.internet if current else True
        if chat_quality is None:
            chat_quality = current.chat_quality if current else "balanced"
        values = {
            "system_prompt": system_prompt or None,
            "temperature": temperature,
            "address": address or None,
            "memory_enabled": memory_enabled,
            "chat_quality": chat_quality,
            "internet": internet,
        }
        await self.db.execute(
            insert(UserSettings)
            .values(user_id=self.user_id, **values)
            .on_conflict_do_update(
                index_elements=["user_id"], set_={**values, "updated_at": func.now()}
            )
        )
        settings = await self.get_settings()
        assert settings is not None  # noqa: S101 - just written
        await self.db.refresh(settings)
        return settings


# --- installation-wide settings (admin, not personal) ---------------------------------------


async def get_hub_setting(db: AsyncSession, key: str) -> Any | None:
    row = await db.get(HubSetting, key)
    return row.value if row else None


async def set_hub_setting(db: AsyncSession, key: str, value: Any | None) -> None:
    if value is None:
        await db.execute(delete(HubSetting).where(HubSetting.key == key))
        return
    await db.execute(
        insert(HubSetting)
        .values(key=key, value=value)
        .on_conflict_do_update(
            index_elements=["key"], set_={"value": value, "updated_at": func.now()}
        )
    )
