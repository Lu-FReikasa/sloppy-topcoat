"""ORM models. Keep in sync with the Alembic migrations in ``hub/migrations``.

Rule (CLAUDE.md): every row that belongs to a person carries a ``user_id``.
Nodes are infrastructure owned by the installation, not by a person.
"""

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    LargeBinary,
    SmallInteger,
    String,
    Text,
    UniqueConstraint,
    cast,
    false,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from sqlalchemy.types import UserDefinedType

DEV_ADMIN_ID = uuid.UUID("00000000-0000-4000-8000-000000000001")
# E3-06: what belongs to nobody in particular is owned by two fixed pseudo users, so every query
# scoped by user_id stays exactly as strict as before (no real user's query can see their notes)
HOUSEHOLD_ID = uuid.UUID("00000000-0000-4000-8000-000000000002")
SYSTEM_ID = uuid.UUID("00000000-0000-4000-8000-000000000003")
PSEUDO_USER_IDS = (HOUSEHOLD_ID, SYSTEM_ID)


class Base(DeclarativeBase):
    pass


class Vector(UserDefinedType[list[float]]):
    """pgvector ``vector`` without a fixed dimension (different embedders have different
    sizes; a user's memory holds hundreds of items, so an exact scan needs no index).
    Values go in as ``'[0.1,0.2]'`` text and are cast on the database side; distances are
    computed in SQL (see :mod:`topcoat_hub.memory_store`), never read back."""

    cache_ok = True

    def get_col_spec(self, **_kw: Any) -> str:
        return "vector"

    def bind_processor(self, dialect: Any) -> Any:
        def process(value: list[float] | None) -> str | None:
            if value is None:
                return None
            return "[" + ",".join(repr(float(x)) for x in value) + "]"

        return process

    def bind_expression(self, bindvalue: Any) -> Any:
        return cast(bindvalue, _VectorCast())


class _VectorCast(UserDefinedType[Any]):
    cache_ok = True

    def get_col_spec(self, **_kw: Any) -> str:
        return "vector"


class User(Base):
    __tablename__ = "users"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(String(100), unique=True)
    role: Mapped[str] = mapped_column(String(20))  # admin | user
    # GUI language (en | de); None = follow the browser.
    language: Mapped[str | None] = mapped_column(String(8))
    # IANA time zone of the person (brief PPH-00-implementation-prep, D4-4); None = the
    # hub's
    timezone: Mapped[str | None] = mapped_column(String(64))
    # PPH-04, 04h: quiet hours (minutes after midnight, local time, may cross midnight; both or
    # neither) and the local hour from which the daily overdue digest may go out (None = no digest)
    project_quiet_start: Mapped[int | None] = mapped_column(SmallInteger)
    project_quiet_end: Mapped[int | None] = mapped_column(SmallInteger)
    project_digest_hour: Mapped[int | None] = mapped_column(SmallInteger)
    # L1 (D60): the profile that decides which workspaces and tool groups the person is offered;
    # NULL = the default profile. The admin role ignores profiles.
    profile_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("ui_profiles.id", ondelete="SET NULL")
    )
    # D70: the person's interface choices, the hue and the desktop (which programs are open);
    # the hub clamps every value (``topcoat_hub.ui.prefs``).
    ui_prefs: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict, server_default="{}")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Node(Base):
    __tablename__ = "nodes"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(String(63), unique=True)
    os: Mapped[str] = mapped_column(String(20))
    arch: Mapped[str] = mapped_column(String(20))
    public_key: Mapped[str] = mapped_column(Text)
    state: Mapped[str] = mapped_column(String(20), default="offline")
    agent_version: Mapped[str | None] = mapped_column(String(50))
    last_seen: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    config: Mapped[dict[str, Any]] = mapped_column(JSONB)
    config_hash: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class NodeInventory(Base):
    """Latest inventory and latest heartbeat per node (overwritten, not history)."""

    __tablename__ = "node_inventory"

    node_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("nodes.id", ondelete="CASCADE"), primary_key=True
    )
    inventory: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    status: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class NodeAlias(Base):
    """A name a node had before it was renamed (node_rename.py). The history (chats, reports,
    tool calls) keeps the old name; a request that still names it reaches the node by this row."""

    __tablename__ = "node_aliases"

    name: Mapped[str] = mapped_column(String(63), primary_key=True)
    node_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("nodes.id", ondelete="CASCADE"), index=True
    )
    renamed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class NodeEvent(Base):
    """Append-only event log. ``ts`` is the node's timestamp, ``received_at`` the hub's."""

    __tablename__ = "node_events"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    node_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("nodes.id", ondelete="CASCADE"), index=True
    )
    message_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), unique=True)
    kind: Mapped[str] = mapped_column(String(40))
    data: Mapped[dict[str, Any]] = mapped_column(JSONB)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    received_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class NodeBenchmark(Base):
    """One llama-bench measurement (pp512 / tg128) reported by a node's ``benchmark_result``."""

    __tablename__ = "node_benchmarks"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    node_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("nodes.id", ondelete="CASCADE"), index=True
    )
    benchmark_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True))
    model: Mapped[str] = mapped_column(Text)
    backend: Mapped[str] = mapped_column(String(20))
    threads: Mapped[int] = mapped_column(Integer)
    n_gpu_layers: Mapped[int | None] = mapped_column(Integer)
    pp_tps: Mapped[float] = mapped_column(Float)
    tg_tps: Mapped[float] = mapped_column(Float)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class ModelRating(Base):
    """The result of one bench suite for one model on one node (PRG-00 R6)."""

    __tablename__ = "model_ratings"
    # named by d107
    __table_args__ = (Index("ix_model_ratings_lookup", "model", "node", "suite", "variant"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=True
    )
    run_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), index=True)
    model: Mapped[str] = mapped_column(Text)
    node: Mapped[str] = mapped_column(String(63))
    suite: Mapped[str] = mapped_column(String(40))
    function_kind: Mapped[str] = mapped_column(String(40))
    suite_version: Mapped[int] = mapped_column(Integer)
    # the preset of bench/variants.py the suite ran with; "" = the model as it is asked every day
    variant: Mapped[str] = mapped_column(String(40), default="", server_default="")
    score: Mapped[float] = mapped_column(Float)  # share of the cases that passed, 0 to 1
    passed: Mapped[int] = mapped_column(Integer)
    total: Mapped[int] = mapped_column(Integer)
    tps: Mapped[float | None] = mapped_column(Float)  # tokens per second over the whole answer
    failed: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, default=list, server_default="[]")


class PairingCode(Base):
    __tablename__ = "pairing_codes"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    code_hash: Mapped[str] = mapped_column(String(64), unique=True)
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id"), index=True
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    node_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("nodes.id", ondelete="SET NULL")
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class ApiKey(Base):
    """Bearer keys for the OpenAI-compatible gateway (/v1, E1-05). Only a SHA-256 of the key
    is stored; the key itself is shown once when it is created."""

    __tablename__ = "api_keys"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    name: Mapped[str] = mapped_column(String(100))
    # First characters of the key, to recognise it in the GUI ("tc_ab12cd…").
    prefix: Mapped[str] = mapped_column(String(16))
    key_hash: Mapped[str] = mapped_column(String(64), unique=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class Conversation(Base):
    """A chat conversation. Personal data (D20): only ever read or written through
    :class:`topcoat_hub.chat_store.ChatStore`, which scopes every query by ``user_id``."""

    __tablename__ = "conversations"
    __table_args__ = (
        Index("conversations_continues_idx", "continues_id"),  # named by 0050
        Index("ix_conversations_user_application", "user_id", "application"),  # 0058
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    title: Mapped[str] = mapped_column(String(200), default="")
    # Picker: model name (None = the user's / hub default) and a pinned node (None = automatic).
    model: Mapped[str | None] = mapped_column(String(255))
    node: Mapped[str | None] = mapped_column(String(63))
    # Quality level of this conversation: fast | balanced | thorough (None = the user's default)
    quality: Mapped[str | None] = mapped_column(String(12))
    # E3-04: the nodes and tool groups the assistant may use here (None = all the user may use)
    tool_scope: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    # the moments (ISO timestamps) the user cut the conversation into a new segment ("new section")
    segment_breaks: Mapped[list[str] | None] = mapped_column(JSONB)
    # the chat this one continues (a section of the continuous conversation); NULL = a first chat
    continues_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("conversations.id", ondelete="SET NULL")
    )
    # E3-06: the core block this conversation's prompt was built from and what it was built for
    # (stamp): rebuilt only when a core entry changed, so the prompt prefix stays byte-identical
    core_stamp: Mapped[str | None] = mapped_column(String(64))
    core_text: Mapped[str | None] = mapped_column(Text)
    # E3-06 (11b): the persona this conversation speaks with (a ``personas.id``); frozen from the
    # user's default when the conversation is created, changed only by the user's own choice
    persona_ref: Mapped[str | None] = mapped_column(String(40))
    # PLATFORM.md: the application (a workspace key) this conversation was started in
    application: Mapped[str | None] = mapped_column(String(40))
    # where it was started: a desktop window's view, ids and title (conversation_context.py)
    context: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    # Start gathers the open conversations (2026-11-13): pinned stays open, done is the person's
    # click (new activity opens it again), a day without activity counts as done by itself
    pinned_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    done_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Message(Base):
    """One chat message; carries ``user_id`` itself so it can be scoped without a join."""

    __tablename__ = "messages"
    __table_args__ = (Index("ix_messages_conversation_id", "conversation_id", "id"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    conversation_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("conversations.id", ondelete="CASCADE")
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    role: Mapped[str] = mapped_column(String(16))  # user | assistant
    content: Mapped[str] = mapped_column(Text, default="")
    # Assistant messages: what answered and how it went.
    model: Mapped[str | None] = mapped_column(String(255))
    node: Mapped[str | None] = mapped_column(String(63))
    fallback_from: Mapped[str | None] = mapped_column(String(63))
    status: Mapped[str] = mapped_column(
        String(16), default="complete"
    )  # complete|stopped|error|truncated|running|interrupted (E3-07)
    error: Mapped[str | None] = mapped_column(Text)
    prompt_tokens: Mapped[int | None] = mapped_column(Integer)
    completion_tokens: Mapped[int | None] = mapped_column(Integer)
    first_token_ms: Mapped[int | None] = mapped_column(Integer)
    duration_ms: Mapped[int | None] = mapped_column(Integer)
    context_trimmed: Mapped[int] = mapped_column(Integer, default=0)
    # Memory notes the model saw for this answer: [{"id", "text", "evidence", "status"}] (E2-02).
    memory_refs: Mapped[list[dict[str, Any]] | None] = mapped_column(JSONB)
    # The server check (agent task) this answer is based on, if the chat ran one (E3-01d).
    agent_task_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("agent_tasks.id", ondelete="SET NULL")
    )
    # E3-04: the second look at a checked answer by the big model: {ok, issues, model, at}
    verification: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    # E3-06 (11a): the SHA-256 of the system prompt this answer was sent with (a prompt snapshot of
    # the user), the persona it came from ("<id>@<version>") and the user's verdict on it
    # (1 helped, -1 did not fit)
    prompt_hash: Mapped[str | None] = mapped_column(String(64))
    persona_ref: Mapped[str | None] = mapped_column(String(80))
    feedback: Mapped[int | None] = mapped_column(SmallInteger)
    # E3-07 D3: what the message is — ``say`` (a chat bubble), ``progress`` (the line that shows
    # which tool runs; it exists while a check runs and is deleted at the result) or ``report``
    # (the card the hub builds from a check; ``report`` holds its structure, ``content`` its text)
    kind: Mapped[str] = mapped_column(String(16), default="say", server_default="say")
    report: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    # E7-01 (D56): what the hub did to produce this answer, in order — thoughts, plan lines, tool
    # calls with their result, checks, retries, notes from the person (``activity.py``)
    activity: Mapped[list[dict[str, Any]] | None] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class UserSettings(Base):
    """Per-user chat settings (personal, D20)."""

    __tablename__ = "user_settings"

    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )
    system_prompt: Mapped[str | None] = mapped_column(Text)
    temperature: Mapped[float | None] = mapped_column(Float)
    default_model: Mapped[str | None] = mapped_column(String(255))
    address: Mapped[str | None] = mapped_column(String(8))  # du|sie, null = du
    memory_enabled: Mapped[bool] = mapped_column(Boolean, default=True, server_default="true")
    # E3-04: the default quality level of the user's conversations (fast|balanced|thorough)
    chat_quality: Mapped[str] = mapped_column(
        String(12), default="balanced", server_default="balanced"
    )
    # E3-04: the last tool scope the user chose; new conversations start with it
    tool_scope: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    # E3-04: the default of the per-conversation "Internet" switch (web search and page reading)
    internet: Mapped[bool] = mapped_column(Boolean, default=True, server_default="true")
    # E3-06 (11): the persona new conversations start with, and where the first-start wizard is
    # (null = not offered yet | later | skipped | done)
    persona_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("personas.id", ondelete="SET NULL")
    )
    persona_wizard: Mapped[str | None] = mapped_column(String(10))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class HubSetting(Base):
    """Installation-wide settings edited by an admin (not personal data)."""

    __tablename__ = "hub_settings"

    key: Mapped[str] = mapped_column(String(100), primary_key=True)
    value: Mapped[Any] = mapped_column(JSONB)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class SettingChange(Base):
    """E7-03 (D58): one change of a live parameter — who, when, from what to what. A reset is a
    change whose new value is null. Rows are never edited, so the history can be trusted."""

    __tablename__ = "setting_changes"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    key: Mapped[str] = mapped_column(String(100), index=True)
    old_value: Mapped[Any] = mapped_column(JSONB, nullable=True)
    new_value: Mapped[Any] = mapped_column(JSONB, nullable=True)
    user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )
    at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class MemoryItem(Base):
    """One note in a user's memory (D19, D20): only ever read or written through
    :class:`topcoat_hub.memory_store.MemoryStore`, which scopes every query by ``user_id``.
    The row is the current state; every change also writes a :class:`MemoryVersion`."""

    __tablename__ = "memory_items"
    __table_args__ = (
        Index("ix_memory_items_core", "user_id", "core"),
        Index("ix_memory_items_user_application", "user_id", "application"),  # 0060
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    text: Mapped[str] = mapped_column(Text)
    embedding: Mapped[list[float] | None] = mapped_column(Vector(), deferred=True)
    embedder: Mapped[str | None] = mapped_column(String(100))  # which embedder made it
    status: Mapped[str] = mapped_column(
        String(16), default="active"
    )  # active|superseded|disputed|deleted
    confidence: Mapped[float] = mapped_column(Float, default=0.7)
    evidence: Mapped[str] = mapped_column(String(16), default="doc")  # live|doc|assumption
    source_type: Mapped[str] = mapped_column(String(16), default="chat")  # chat|tool|web|doc
    source_ref: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    version: Mapped[int] = mapped_column(Integer, default=1)
    disputed_with: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    superseded_by: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    # Last time the idle consolidation looked at it (None = not yet).
    checked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # The user said it again: a counter and a time, no new version (nothing changed).
    confirmed_count: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    last_confirmed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # E3-05 memory care ------------------------------------------------------------------
    # person (stable) | preference | operational (about the user's infrastructure)
    kind: Mapped[str] = mapped_column(String(12), default="person", server_default="person")
    # A live check showed it still holds (a count and a time; a check counts double a statement)
    last_verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    verified_count: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    # Only a volatile note ("service X is down") expires; None = it does not
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # D74: the program whose memory the note is in; null = the person's core (who the person is
    # and what they like), which every program reads. Its tags are rows of memory_item_tags.
    application: Mapped[str | None] = mapped_column(String(40))
    # What can check it: {"capability": "omada.devices", "input": {…}} (a read-only capability)
    verify_with: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    # Lowered when a check was not possible, back to 1 by a confirmation or a check
    trust: Mapped[float] = mapped_column(Float, default=1.0, server_default="1.0")
    # Usefulness (apart from reliability): put in a prompt / used in the answer / the user's word
    recalled_count: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    used_count: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    feedback_positive: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    feedback_negative: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    # Last time a care run looked at it (checked or found unverifiable)
    care_checked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # E3-06: pinned into the core (the assistant's first block), where it belongs (user | household
    # | system), the example of a style note, and a style that is only adjectives (a weak hint)
    core: Mapped[bool] = mapped_column(Boolean, default=False, server_default=false())
    scope: Mapped[str] = mapped_column(String(10), default="user", server_default="user")
    example: Mapped[str | None] = mapped_column(Text)
    weak: Mapped[bool] = mapped_column(Boolean, default=False, server_default=false())


class MemoryTag(Base):
    """One tag of a person's tag tree (D74): what a note is about. Names are unique per person, so
    a tag is found by its name; the tree gives it a place. ``responsible`` (the program that
    decides when notes about this topic disagree, or ``person``), ``sensitive`` and ``released``
    are inherited from the parent when null. Personal (D20): only through ``TagTree``."""

    __tablename__ = "memory_tags"
    __table_args__ = (UniqueConstraint("user_id", "name", name="uq_memory_tags_user_name"),)

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    parent_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("memory_tags.id", ondelete="CASCADE")
    )
    name: Mapped[str] = mapped_column(String(30))
    description: Mapped[str] = mapped_column(Text, default="", server_default="")
    responsible: Mapped[str | None] = mapped_column(String(40))
    sensitive: Mapped[bool | None] = mapped_column(Boolean)
    # the programs that may still see the notes of a sensitive subtree
    released: Mapped[list[str] | None] = mapped_column(JSONB)
    created_by: Mapped[str] = mapped_column(String(16), default="user")  # seed|user|agent|migration
    # a tag an assistant made waits for the person's look (keep, rename, merge)
    reviewed: Mapped[bool] = mapped_column(Boolean, default=True, server_default="true")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class MemoryItemTag(Base):
    """A note carries a tag (D74)."""

    __tablename__ = "memory_item_tags"

    item_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("memory_items.id", ondelete="CASCADE"), primary_key=True
    )
    tag_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("memory_tags.id", ondelete="CASCADE"),
        primary_key=True,
        index=True,
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), index=True
    )


class MemoryReport(Base):
    """What the cross-check found between two memories (D74), addressed to the program that is
    responsible for the topic — not to the owner of a note — or a notice that program sent to
    another one about a note it changed there. Personal (D20): only through ``CrossCheck``."""

    __tablename__ = "memory_reports"
    __table_args__ = (
        Index("ix_memory_reports_user_recipient", "user_id", "recipient", "status"),
        Index("ix_memory_reports_signature", "user_id", "signature"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE")
    )
    kind: Mapped[str] = mapped_column(String(12))  # discrepancy | agreement | notice
    recipient: Mapped[str] = mapped_column(String(40))  # a program key, or "person"
    sender: Mapped[str | None] = mapped_column(String(40))  # the program that wrote a notice
    item_a: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("memory_items.id", ondelete="CASCADE")
    )
    item_b: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("memory_items.id", ondelete="CASCADE")
    )
    topic: Mapped[str] = mapped_column(String(30), default="", server_default="")
    # open | decided | asked | seen (a notice the person dismissed) | agreed
    status: Mapped[str] = mapped_column(String(10), default="open")
    # the pair and its versions: the same pair in the same state is not reported again
    signature: Mapped[str] = mapped_column(String(64), default="", server_default="")
    reason: Mapped[str] = mapped_column(Text, default="", server_default="")
    message: Mapped[str] = mapped_column(Text, default="", server_default="")
    decision: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class MemoryVersion(Base):
    """History of a memory item: the state after each change, with the reason."""

    __tablename__ = "memory_versions"
    __table_args__ = (Index("ix_memory_versions_item", "item_id", "version", unique=True),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    item_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("memory_items.id", ondelete="CASCADE")
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    version: Mapped[int] = mapped_column(Integer)
    text: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(16))
    evidence: Mapped[str] = mapped_column(String(16))
    confidence: Mapped[float] = mapped_column(Float)
    reason: Mapped[str] = mapped_column(Text, default="")
    actor: Mapped[str] = mapped_column(String(16))  # chat|consolidation|user
    # Where the change came from: conversation / message, question, evidence.
    source: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class MemoryBlock(Base):
    """A candidate the secret filter refused to store. Never contains the value."""

    __tablename__ = "memory_blocks"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    reason: Mapped[str] = mapped_column(String(50))  # which rule fired, e.g. "password"
    source_type: Mapped[str] = mapped_column(String(16))
    source_ref: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class MemoryRun(Base):
    """One idle consolidation run for one user."""

    __tablename__ = "memory_runs"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    trigger: Mapped[str] = mapped_column(String(16))  # scheduled|manual
    # E3-05: consolidation (merge, conflicts) | care (check, correct, expire, archive)
    kind: Mapped[str] = mapped_column(
        String(16), default="consolidation", server_default="consolidation"
    )
    status: Mapped[str] = mapped_column(String(20), default="running")
    # running|ok|stopped_budget|no_model|error (a care run also: paused)
    stats: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class MemoryQuestion(Base):
    """A question the memory could not answer itself (D19): which of these notes is true?
    Personal (D20): only ever read or written through ``MemoryStore``."""

    __tablename__ = "memory_questions"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    item_ids: Mapped[list[str]] = mapped_column(JSONB)  # the notes in doubt
    topic: Mapped[str] = mapped_column(String(50), default="")  # e.g. "residence"
    origin: Mapped[str] = mapped_column(String(16))  # chat|consolidation
    status: Mapped[str] = mapped_column(String(12), default="open")  # open|answered|dismissed
    # Snapshot for the GUI: {"candidates": [{id, text, at, confirmed_count, last_confirmed_at}]}
    context: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    answer: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    resolution: Mapped[str | None] = mapped_column(Text)  # how it ended, with the evidence
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    answered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class MemoryProposal(Base):
    """A change to a rule, a style or the core that the assistant (or a care run) *proposes*; it
    takes effect only when the user accepts it (one click), and then it is a version like any
    other. Personal (D20): only ever read or written through ``MemoryStore``."""

    __tablename__ = "memory_proposals"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
        server_default=func.gen_random_uuid(),
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE")
    )
    # add | change | remove | pin | unpin | kb_edit (a wish for a note file: copy the Markdown)
    action: Mapped[str] = mapped_column(String(12))
    scope: Mapped[str] = mapped_column(String(10), default="user", server_default="user")
    target_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    kind: Mapped[str] = mapped_column(String(12))
    text: Mapped[str] = mapped_column(Text, default="", server_default="")
    example: Mapped[str | None] = mapped_column(Text)
    core: Mapped[bool] = mapped_column(Boolean, default=False, server_default=false())
    weak: Mapped[bool] = mapped_column(Boolean, default=False, server_default=false())
    reason: Mapped[str] = mapped_column(Text, default="", server_default="")
    evidence: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)  # quote, conversation …
    diff: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)  # {before, after}
    previous: Mapped[dict[str, Any] | None] = mapped_column(JSONB)  # the note before (a snapshot)
    origin: Mapped[str] = mapped_column(String(16))  # chat | care | kb
    status: Mapped[str] = mapped_column(String(10), default="proposed", server_default="proposed")
    fingerprint: Mapped[str] = mapped_column(
        String(40)
    )  # of action + target + text: no re-proposal
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    decision: Mapped[dict[str, Any] | None] = mapped_column(JSONB)

    __table_args__ = (
        Index("ix_memory_proposals_user", "user_id", "status"),
        Index("ix_memory_proposals_fingerprint", "user_id", "fingerprint"),
    )


class CapabilityGrant(Base):
    """A user may run a read-only capability of a tool node (E2-04, D20). ``capability`` is a
    capability name or ``*`` for every read-only capability the node enables. Admins need no
    grant; an API key never inherits its admin's rights, so it needs grants like any user."""

    __tablename__ = "capability_grants"

    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )
    node_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("nodes.id", ondelete="CASCADE"), primary_key=True
    )
    capability: Mapped[str] = mapped_column(String(100), primary_key=True)
    granted_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class ToolCall(Base):
    """Audit log of every capability call, allowed or refused (D15, E2-04). Belongs to ``user_id``
    (D20): a user reads their own rows, an admin all of them. The node name is kept so the row
    outlives the node."""

    __tablename__ = "tool_calls"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    node_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("nodes.id", ondelete="SET NULL"), index=True
    )
    node_name: Mapped[str] = mapped_column(String(63))
    capability: Mapped[str] = mapped_column(String(100))
    input: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    via: Mapped[str] = mapped_column(String(10))  # gui | api | agent
    task_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))  # agent task (stage 3)
    ok: Mapped[bool] = mapped_column(Boolean)
    code: Mapped[str | None] = mapped_column(String(40))  # error code when not ok
    error: Mapped[str | None] = mapped_column(Text)
    output_bytes: Mapped[int] = mapped_column(Integer, default=0)
    output_excerpt: Mapped[str | None] = mapped_column(Text)  # cut to tool_audit_output_bytes
    duration_ms: Mapped[int | None] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), index=True
    )


class AgentTask(Base):
    """A background task of one user's agent (E3-01, D20): a question, the steps the agent took
    and the diagnosis it wrote. Only ever read or written through ``AgentStore``."""

    __tablename__ = "agent_tasks"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    prompt: Mapped[str] = mapped_column(Text)
    title: Mapped[str] = mapped_column(String(200))
    model: Mapped[str | None] = mapped_column(String(255))  # requested model, None = the default
    # gui | api: where the task was started. A task started with an API key never runs with the
    # rights of an admin (the same rule as for capability calls, D29).
    via: Mapped[str] = mapped_column(String(10), default="gui")
    # tasks | chat: a check that a chat answer started (E3-01d) is not listed on the Tasks page
    origin: Mapped[str] = mapped_column(String(10), default="tasks", server_default="tasks")
    # E3-04: {"nodes": [...]|null, "groups": [...]|null} the task may use, on top of the grants
    scope: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    # the application the run belongs to (project: the project agent's tools and memory)
    application: Mapped[str | None] = mapped_column(String(10))
    # the only capabilities this task may use (a reviewer run, PPH-05); None: all it is offered
    capabilities: Mapped[list[str] | None] = mapped_column(JSONB)
    # single | coordinator (E3-08: splits the question, starts one read-only check per machine and
    # merges the reports; depth 1 — its children are ``single``). A child has ``parent_id`` and
    # origin "child" (not listed on the Tasks page).
    kind: Mapped[str] = mapped_column(String(12), default="single", server_default="single")
    parent_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("agent_tasks.id", ondelete="CASCADE"), index=True
    )
    # the chat this task was handed over from (None: started on the Tasks page)
    conversation_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("conversations.id", ondelete="SET NULL"), index=True
    )
    # a queued task waits for a fast node until then; afterwards any node may take it
    deadline_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    status: Mapped[str] = mapped_column(String(12), default="queued")
    # queued | running | done | failed | canceled
    stop_reason: Mapped[str | None] = mapped_column(String(30))  # why it ended early, if it did
    error: Mapped[str | None] = mapped_column(Text)
    summary: Mapped[str | None] = mapped_column(Text)
    diagnosis: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    step_count: Mapped[int] = mapped_column(Integer, default=0)
    tool_calls: Mapped[int] = mapped_column(Integer, default=0)
    runs: Mapped[int] = mapped_column(Integer, default=0)  # how often it was (re)started
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class ChatReview(Base):
    """A second model's look at one answer of a recent chat. ``verdict``: ``ok`` (nothing found),
    ``hint`` (something looks wrong but nothing proves it), ``error`` (a finding with evidence: a
    note of the user's memory contradicts the claim). Only read through ``ReviewStore``."""

    __tablename__ = "chat_reviews"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    conversation_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("conversations.id", ondelete="CASCADE"), index=True
    )
    message_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("messages.id", ondelete="CASCADE"), unique=True
    )
    verdict: Mapped[str] = mapped_column(String(8))
    # [{"claim", "reason", "evidence": {"note_id", "text"} | None}]
    findings: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, default=list)
    reviewer_model: Mapped[str | None] = mapped_column(String(255))
    reviewer_node: Mapped[str | None] = mapped_column(String(63))
    author_model: Mapped[str | None] = mapped_column(String(255))
    author_node: Mapped[str | None] = mapped_column(String(63))
    seen: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")
    # the chat's agent has been given the findings (they stand in the context of one answer)
    told_agent: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class ChatDigest(Base):
    """What the two looks at a finished chat produced: the author's summary (pass 1), then another
    model's check of chat and summary (pass 2) with the notes it learned. One row per
    conversation, overwritten when the chat goes on. ``status``: ``summarised`` (pass 1 only),
    ``checked`` (both passes), ``no_checker`` (no model other than the author's was there)."""

    __tablename__ = "chat_digests"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    conversation_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("conversations.id", ondelete="CASCADE"), unique=True
    )
    last_message_id: Mapped[int] = mapped_column(BigInteger)
    summary: Mapped[str] = mapped_column(Text)
    passes: Mapped[int] = mapped_column(SmallInteger, default=1, server_default="1")
    status: Mapped[str] = mapped_column(String(16), default="summarised")
    learned: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    author_model: Mapped[str | None] = mapped_column(String(255))
    author_node: Mapped[str | None] = mapped_column(String(63))
    checker_model: Mapped[str | None] = mapped_column(String(255))
    checker_node: Mapped[str | None] = mapped_column(String(63))
    # E5-01: the statements behind ``summary`` with their message and quote, verified or not
    summary_refs: Mapped[list[dict[str, Any]] | None] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class ChatSegment(Base):
    """Summary of a closed segment of a continuous conversation (the run of messages between two
    rests, see :mod:`topcoat_hub.segments`). Written once, when the segment is closed."""

    __tablename__ = "chat_segments"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    conversation_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("conversations.id", ondelete="CASCADE")
    )
    first_message_id: Mapped[int] = mapped_column(BigInteger)
    last_message_id: Mapped[int] = mapped_column(BigInteger)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    ended_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    summary: Mapped[str] = mapped_column(Text)
    author_model: Mapped[str | None] = mapped_column(String(255))
    author_node: Mapped[str | None] = mapped_column(String(63))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    __table_args__ = (
        UniqueConstraint("conversation_id", "first_message_id", name="chat_segments_start_uq"),
    )


class AgentStep(Base):
    """One entry of a task's transcript, in order: what the model said (``assistant``), what it
    was shown in return (``observation``). Enough to rebuild the conversation after a restart."""

    __tablename__ = "agent_steps"
    __table_args__ = (UniqueConstraint("task_id", "seq", name="uq_agent_steps_task_seq"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    task_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("agent_tasks.id", ondelete="CASCADE"), index=True
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    seq: Mapped[int] = mapped_column(Integer)
    kind: Mapped[str] = mapped_column(String(16))  # assistant | observation | note
    data: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class AgentEvidence(Base):
    """What a tool showed the agent, kept with its source: ``ref`` (E1, E2 …) is what findings of
    the diagnosis point at. ``tag`` is ``live`` (seen on the machine just now) or ``doc`` (from the
    knowledge base, E3-03)."""

    __tablename__ = "agent_evidence"
    __table_args__ = (UniqueConstraint("task_id", "ref", name="uq_agent_evidence_task_ref"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    task_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("agent_tasks.id", ondelete="CASCADE"), index=True
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    ref: Mapped[str] = mapped_column(String(8))
    tag: Mapped[str] = mapped_column(String(8))
    source: Mapped[dict[str, Any]] = mapped_column(JSONB)  # node, capability, input, at, call_id
    excerpt: Mapped[str] = mapped_column(Text, default="")
    bytes: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class AgentProposal(Base):
    """A change the agent proposes after its diagnosis (E3-01b, D15, D43): the exact action (node,
    capability, input), why, what it does, who notices, the way back. Immutable — ``content_hash``
    covers all of it — and it runs only after the click of the signed-in owner on this one row.
    ``status``: pending → running → executed | failed; or refused / expired. Only ever read or
    written through ``ProposalService``, which filters every statement by ``user_id`` (D20)."""

    __tablename__ = "agent_proposals"
    __table_args__ = (Index("ix_agent_proposals_task", "task_id", "created_at"),)

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    # NULL for a manual proposal: one the person asked for themselves in the inventory
    task_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("agent_tasks.id", ondelete="CASCADE")
    )
    # who made it: "agent" (after a diagnosis) or "manual" (the person, in the inventory)
    origin: Mapped[str] = mapped_column(String(10), default="agent", server_default="agent")
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    node_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("nodes.id", ondelete="SET NULL")
    )
    node_name: Mapped[str] = mapped_column(String(63))
    capability: Mapped[str] = mapped_column(String(100))
    input: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    reason: Mapped[str] = mapped_column(Text)
    effect: Mapped[str] = mapped_column(Text)
    who_notices: Mapped[str] = mapped_column(Text)
    way_back: Mapped[str] = mapped_column(Text)
    # shown with the proposal and covered by its hash (a push: the diff that will be committed)
    preview: Mapped[str | None] = mapped_column(Text)
    evidence: Mapped[list[str]] = mapped_column(JSONB, default=list)  # refs of the task's evidence
    content_hash: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(10), default="pending")
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    decided_by: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))  # who clicked
    result: Mapped[dict[str, Any] | None] = mapped_column(JSONB)  # what came back (or the error)
    tool_call_id: Mapped[int | None] = mapped_column(BigInteger)  # the audit row of the run
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class InventoryNode(Base):
    """What the hub knows about the last look at one node's inventory (E3-10, D45): when it was
    tried, when a section last worked and why a section did not. Infrastructure, not a person's."""

    __tablename__ = "inventory_nodes"

    node_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("nodes.id", ondelete="CASCADE"), primary_key=True
    )
    attempted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    collected_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # ok | partial (a section failed) | error (nothing could be read) | unavailable (not offered)
    status: Mapped[str] = mapped_column(String(12), default="unavailable")
    error: Mapped[str | None] = mapped_column(Text)
    duration_ms: Mapped[int | None] = mapped_column(Integer)
    # per section (host, containers, services): ok, code, error, read_at (the last time it worked)
    sections: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    host: Mapped[dict[str, Any] | None] = mapped_column(JSONB)


class InventoryItem(Base):
    """A container or a systemd service seen on a node. ``gone_at`` is set when a look that
    succeeded for that section did not list it any more; a look that failed changes nothing."""

    __tablename__ = "inventory_items"
    __table_args__ = (
        UniqueConstraint("node_id", "kind", "name", name="uq_inventory_item"),
        Index("ix_inventory_items_node_kind", "node_id", "kind"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    node_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("nodes.id", ondelete="CASCADE")
    )
    kind: Mapped[str] = mapped_column(String(12))  # container | service
    name: Mapped[str] = mapped_column(String(200))
    state: Mapped[str] = mapped_column(String(32))
    state_since: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    detail: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    first_seen: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    last_seen: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    gone_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class KnowledgeSource(Base):
    """A folder of Markdown/text files the knowledge base indexes through a tool node (E2-03,
    D20, D34). ``visibility`` ``shared`` (every user searches it) or ``private`` (only the owner);
    only an admin creates sources. Reached only through ``KnowledgeStore``."""

    __tablename__ = "knowledge_sources"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    name: Mapped[str] = mapped_column(String(100))
    visibility: Mapped[str] = mapped_column(String(8), default="shared")
    # node = a folder read through a tool node; upload = files the admin put in by hand (no node)
    kind: Mapped[str] = mapped_column(String(8), default="node", server_default="node")
    node_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("nodes.id", ondelete="SET NULL")
    )
    node_name: Mapped[str] = mapped_column(String(63))
    root: Mapped[str] = mapped_column(String(512))
    status: Mapped[str] = mapped_column(String(10), default="new")  # new|indexing|ready|error
    error: Mapped[str | None] = mapped_column(Text)
    file_count: Mapped[int] = mapped_column(Integer, default=0)
    chunk_count: Mapped[int] = mapped_column(Integer, default=0)
    excluded_count: Mapped[int] = mapped_column(Integer, default=0)  # entries the list kept out
    blocked_count: Mapped[int] = mapped_column(Integer, default=0)  # chunks the secret filter held
    indexed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class KnowledgeFile(Base):
    """One indexed file of a source: what the next run compares to find changes."""

    __tablename__ = "knowledge_files"
    __table_args__ = (UniqueConstraint("source_id", "path", name="uq_knowledge_files_path"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    source_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("knowledge_sources.id", ondelete="CASCADE"), index=True
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    path: Mapped[str] = mapped_column(String(1024))
    size: Mapped[int] = mapped_column(BigInteger, default=0)
    mtime: Mapped[int] = mapped_column(BigInteger, default=0)
    content_hash: Mapped[str] = mapped_column(String(64), default="")
    chunk_count: Mapped[int] = mapped_column(Integer, default=0)
    blocked: Mapped[list[str]] = mapped_column(JSONB, default=list)  # rule names, never values


class KnowledgeChunk(Base):
    """A searchable piece of a file. ``embedding`` is NULL while no embedding model answered;
    search then falls back to keywords for it."""

    __tablename__ = "knowledge_chunks"
    # the full-text index (GIN on to_tsvector(heading || text)) is made by migration 0018 only:
    # Alembic cannot compare an expression index, see MIGRATION_ONLY_INDEXES in migrations/env.py

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    source_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("knowledge_sources.id", ondelete="CASCADE"), index=True
    )
    file_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("knowledge_files.id", ondelete="CASCADE"), index=True
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    path: Mapped[str] = mapped_column(String(1024))
    heading: Mapped[str] = mapped_column(Text, default="")
    ord: Mapped[int] = mapped_column(Integer, default=0)
    text: Mapped[str] = mapped_column(Text)
    mtime: Mapped[int] = mapped_column(BigInteger, default=0)
    embedding: Mapped[list[float] | None] = mapped_column(Vector)
    embedder: Mapped[str | None] = mapped_column(String(100))


class Persona(Base):
    """A tone of voice (E3-06, 11c): built from blocks, never free-generated. A template belongs to
    the household pseudo user (everybody reads it, an admin changes it), a personal one to its user
    (D20). A change is a new ``version``; the text it produced stays in the prompt snapshots."""

    __tablename__ = "personas"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE")
    )
    scope: Mapped[str] = mapped_column(String(10), default="user", server_default="user")
    key: Mapped[str | None] = mapped_column(String(40))  # template slug, or "legacy"
    name: Mapped[str] = mapped_column(String(80))
    version: Mapped[int] = mapped_column(Integer, default=1, server_default="1")
    components: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    example: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)  # {"de": ..., "en": ...}
    defaults: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    status: Mapped[str] = mapped_column(String(10), default="active", server_default="active")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    __table_args__ = (
        Index("ix_personas_user", "user_id", "status"),
        Index("ix_personas_key", "user_id", "key"),
        CheckConstraint("scope IN ('household', 'user')", name="personas_scope_check"),
        CheckConstraint("status IN ('active', 'archived')", name="personas_status_check"),
    )


class PromptSnapshot(Base):
    """The system prompt as it was sent (E3-06, 11a): once per user and SHA-256 (the hash that keeps
    the prompt cache stable, D27). Personal (D20): one row per user, so one user never sees
    another's, not even for the same text."""

    __tablename__ = "prompt_snapshots"

    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )
    hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    text: Mapped[str] = mapped_column(Text)
    persona_ref: Mapped[str | None] = mapped_column(String(80))
    core_version: Mapped[str | None] = mapped_column(String(64))  # the core stamp
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Artifact(Base):
    """A report or document the agent made (kept apart from chats). Personal (D20): only ever read
    or written through :class:`topcoat_hub.artifact_store.ArtifactStore`."""

    __tablename__ = "artifacts"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE")
    )
    kind: Mapped[str] = mapped_column(String(12), default="report", server_default="report")
    title: Mapped[str] = mapped_column(String(200))
    body: Mapped[str] = mapped_column(Text, default="", server_default="")
    # the task that made it (one artifact per task: a continued task updates it) and the chat the
    # task was handed over from (only a note, nothing reads the chat)
    task_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("agent_tasks.id", ondelete="SET NULL")
    )
    conversation_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("conversations.id", ondelete="SET NULL")
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    read_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        Index("ix_artifacts_user_id", "user_id", "created_at"),
        Index("ix_artifacts_task_id", "task_id"),
    )


class NodeSpeed(Base):
    """What the hub measured on a node for a model: generation speed (tokens per second, from real
    answers) and load speed (MB per second, from real loads), each a moving average. The node
    choice (``gateway.py``) uses them before the benchmark numbers."""

    __tablename__ = "node_speeds"
    __table_args__ = (UniqueConstraint("node_id", "model", name="uq_node_speeds_node_model"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    node_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("nodes.id", ondelete="CASCADE")
    )
    model: Mapped[str] = mapped_column(Text)
    tg_tps: Mapped[float | None] = mapped_column(Float)
    tg_samples: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    load_mb_s: Mapped[float | None] = mapped_column(Float)
    load_samples: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class JobRecord(Base):
    """One model request the gateway served or refused (SCH-03 step 3, D86): who asked for what,
    where it ran and how long it waited. Append-only, never content. ``user_id`` is empty only for
    hub work without a person behind it (and for requests made before a caller set the context);
    reads are scoped by it like every personal row."""

    __tablename__ = "job_records"
    __table_args__ = (
        Index("ix_job_records_created_at", "created_at"),
        Index("ix_job_records_conversation_id", "conversation_id"),
        Index("ix_job_records_task_id", "task_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))  # the request arrived
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    conversation_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    message_id: Mapped[int | None] = mapped_column(BigInteger)
    task_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    application: Mapped[str | None] = mapped_column(String(40))
    work: Mapped[str] = mapped_column(String(40), default="")  # chat, digest_check, care, ...
    caller: Mapped[str] = mapped_column(String(200), default="")  # caller_chain()
    requested: Mapped[str] = mapped_column(String(255), default="")  # model or model@node
    chosen_model: Mapped[str] = mapped_column(String(255), default="")
    chosen_node: Mapped[str] = mapped_column(String(63), default="")
    place: Mapped[int] = mapped_column(SmallInteger, default=0)  # 0 chat, 1 answer, 2 idle
    # picked | pinned: whether the person named the node (more sources come with the table, SCH-04)
    source: Mapped[str] = mapped_column(String(24), default="picked")
    reason: Mapped[str] = mapped_column(String(80), default="")  # a code, e.g. fallback
    outcome: Mapped[str] = mapped_column(String(12), default="done")  # done|failed|cancelled
    error_code: Mapped[str | None] = mapped_column(String(40))
    waited_ms: Mapped[int] = mapped_column(Integer, default=0)
    model_loaded: Mapped[bool] = mapped_column(Boolean, default=True)  # it was already loaded
    duration_ms: Mapped[int] = mapped_column(Integer, default=0)
    # what the model reported at the end of the answer (llama.cpp ``usage`` and ``timings``): the
    # generated tokens, the prompt and how much of it came from the cache, the time the node spent
    # reading the prompt and writing the answer, and why it stopped. Nothing is measured for this:
    # the numbers are read from the last bytes of a stream that passes through anyway.
    tokens: Mapped[int | None] = mapped_column(Integer)
    prompt_tokens: Mapped[int | None] = mapped_column(Integer)
    cached_tokens: Mapped[int | None] = mapped_column(Integer)
    prompt_ms: Mapped[int | None] = mapped_column(Integer)
    predicted_ms: Mapped[int | None] = mapped_column(Integer)
    finish_reason: Mapped[str | None] = mapped_column(String(16))


class ModelTransfer(Base):
    """A model file moving directly from one node to another (E2-07, D51). Personal (D20): only
    ever read or written through :class:`topcoat_hub.model_transfers.ModelTransferStore`. The
    one-time token of the transfer is never stored: it lives in the hub's memory only."""

    __tablename__ = "model_transfers"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE")
    )
    source_node_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("nodes.id", ondelete="CASCADE")
    )
    target_node_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("nodes.id", ondelete="CASCADE")
    )
    model: Mapped[str] = mapped_column(String(255))
    target_name: Mapped[str] = mapped_column(String(255))
    size: Mapped[int | None] = mapped_column(BigInteger)
    sha256: Mapped[str | None] = mapped_column(String(64))
    move: Mapped[bool] = mapped_column(Boolean, default=False, server_default=false())
    # pending | preparing | ready | transferring | done | failed | canceled
    status: Mapped[str] = mapped_column(String(16), default="pending", server_default="pending")
    error_code: Mapped[str | None] = mapped_column(String(32))
    error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    __table_args__ = (Index("ix_model_transfers_user_id", "user_id", "created_at"),)


class DevRepo(Base):
    """A repository the assistant may look at (D52, E6-01 S0). The token is stored encrypted
    (``secretbox``) and never leaves the hub. ``last_state`` is what the last refresh saw."""

    __tablename__ = "dev_repos"
    __table_args__ = (UniqueConstraint("user_id", "name", name="uq_dev_repos_user_name"),)

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    # what the person calls it; the GitHub path is ``full_name`` (owner/name)
    name: Mapped[str] = mapped_column(String(100))
    full_name: Mapped[str] = mapped_column(String(201))
    default_branch: Mapped[str] = mapped_column(String(200), default="main")
    # read | test-branch (what the workshop may do later; S0 only reads)
    mode: Mapped[str] = mapped_column(String(16), default="read")
    test_template: Mapped[str] = mapped_column(String(16), default="none")
    token_enc: Mapped[str] = mapped_column(Text)
    last_state: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    last_checked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    # D76: ``external`` lives elsewhere (GitHub) and is only read; ``local`` lives in the workshop
    # of a dev node (``node_id``), where the assistant edits and tests it. A local repository may
    # be a clone of an external one (``upstream_id``): that is where it is published to.
    kind: Mapped[str] = mapped_column(String(10), default="external", server_default="external")
    node_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("nodes.id", ondelete="SET NULL")
    )
    upstream_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("dev_repos.id", ondelete="SET NULL")
    )


class UiTheme(Base):
    """A custom theme of the desktop (docs/THEMES.md): data and a style sheet, never code. The
    built-in themes live in the web build. Chosen per person through ``users.ui_prefs.look``."""

    __tablename__ = "ui_themes"

    key: Mapped[str] = mapped_column(String(40), primary_key=True)
    name: Mapped[str] = mapped_column(String(60))
    base: Mapped[str | None] = mapped_column(String(20))
    # layout, bar, chrome, motion (proto/ui/theme.schema.json)
    manifest: Mapped[dict[str, Any]] = mapped_column(JSONB)
    css: Mapped[str] = mapped_column(Text, default="", server_default="")
    rev: Mapped[int] = mapped_column(Integer, default=1, server_default="1")
    updated_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class UiProfile(Base):
    """L1 (D60): a named set of grants (family, guest, ...). A person has one
    (``users.profile_id``);
    exactly one is the default for people without one."""

    __tablename__ = "ui_profiles"
    __table_args__ = (  # at most one default profile (0057)
        Index(
            "uq_ui_profiles_one_default",
            "is_default",
            unique=True,
            postgresql_where=text("is_default"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(String(60), unique=True)
    description: Mapped[str] = mapped_column(Text, default="")
    is_default: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")
    builtin: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class UiGrant(Base):
    """One line of a profile: a workspace (``kind`` ``workspace``, off unless allowed) or a tool
    group
    (``kind`` ``tool``, on unless denied) is allowed or denied. Deny wins."""

    __tablename__ = "ui_grants"
    __table_args__ = (
        CheckConstraint("kind IN ('workspace', 'tool')", name="ui_grants_kind_check"),
        CheckConstraint("effect IN ('allow', 'deny')", name="ui_grants_effect_check"),
    )

    profile_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("ui_profiles.id", ondelete="CASCADE"), primary_key=True
    )
    kind: Mapped[str] = mapped_column(String(10), primary_key=True)
    key: Mapped[str] = mapped_column(String(40), primary_key=True)
    effect: Mapped[str] = mapped_column(String(5))


class UiTemplate(Base):
    """L1 (D60): a workspace the admin publishes (or the hub ships): which modules it holds, what
    the
    agent starts with in it, and optionally a default arrangement per shell. Owned by the
    installation (SYSTEM_ID), not by a person. ``rev`` counts the changes; a person's layout
    remembers the revision it was cut from."""

    __tablename__ = "ui_templates"
    __table_args__ = (
        CheckConstraint("requires_role IS NULL OR requires_role = 'admin'", name="ui_tpl_role"),
    )

    key: Mapped[str] = mapped_column(String(40), primary_key=True)
    owner_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), default=SYSTEM_ID
    )
    rev: Mapped[int] = mapped_column(Integer, default=1)
    title: Mapped[str] = mapped_column(String(80))
    title_key: Mapped[str | None] = mapped_column(String(80))
    icon: Mapped[str] = mapped_column(String(8), default="")
    position: Mapped[int] = mapped_column(Integer, default=100)
    modules: Mapped[list[str]] = mapped_column(JSONB)
    # the modules a fresh arrangement opens (a subset of ``modules``); None = all of them
    open: Mapped[list[str] | None] = mapped_column(JSONB)
    agent: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    requires_role: Mapped[str | None] = mapped_column(String(20))
    builtin: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")
    updated_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Plugin(Base):
    """D105 step 2: one installed data plug-in (a manifest, ``proto/plugin/manifest.schema.json``).
    Owned by the installation, not by a person. ``version`` names the :class:`PluginVersion` that
    is in force; a rollback points it at an earlier one. Built-in programs are registered by the
    hub at start (``builtin``) and cannot be removed."""

    __tablename__ = "plugins"

    key: Mapped[str] = mapped_column(String(40), primary_key=True)
    version: Mapped[str] = mapped_column(String(20))
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, server_default="true")
    builtin: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class PluginVersion(Base):
    """One version of a plug-in: the manifest as it was installed and its hash. An admin's version
    is never edited (a change is a new version); only the version ``1.0.0`` of a built-in program
    belongs to the code and is re-written at start when the code changed the program."""

    __tablename__ = "plugin_versions"
    __table_args__ = (UniqueConstraint("plugin_key", "version", name="uq_plugin_versions"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    plugin_key: Mapped[str] = mapped_column(
        String(40), ForeignKey("plugins.key", ondelete="CASCADE"), index=True
    )
    version: Mapped[str] = mapped_column(String(20))
    manifest: Mapped[dict[str, Any]] = mapped_column(JSONB)
    sha256: Mapped[str] = mapped_column(String(64))
    installed_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )
    installed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class PluginChange(Base):
    """The audit of the registry: every install, update, rollback, disable, enable and removal,
    who did it and when. Rows are never edited and outlive the plug-in they name."""

    __tablename__ = "plugin_changes"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    plugin_key: Mapped[str] = mapped_column(String(40), index=True)
    action: Mapped[str] = mapped_column(String(20))
    from_version: Mapped[str | None] = mapped_column(String(20))
    to_version: Mapped[str | None] = mapped_column(String(20))
    sha256: Mapped[str | None] = mapped_column(String(64))
    user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )
    at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class InboxItem(Base):
    """PLATFORM.md 3.7: something that happened and wants to be read. Personal (D20): only ever
    read or written through :class:`topcoat_hub.inbox.InboxStore`. ``urgent`` items stay in the
    inbox until they are resolved; a read item is filed in the corner of its application unless it
    is pinned (then it stays on the board). ``source_*`` ties an item to the thing it is about, so
    the same event never makes two items."""

    __tablename__ = "inbox_items"
    __table_args__ = (
        UniqueConstraint("user_id", "kind", "source_type", "source_ref", name="uq_inbox_source"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    application: Mapped[str] = mapped_column(String(40), default="chat")
    kind: Mapped[str] = mapped_column(String(30))
    source_type: Mapped[str | None] = mapped_column(String(30))
    source_ref: Mapped[str | None] = mapped_column(String(80))
    title: Mapped[str] = mapped_column(String(200))
    # what the person may read in the inbox; the details stay in the application the item is about
    summary: Mapped[str] = mapped_column(Text, default="")
    ref: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    urgent: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    read_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class Handover(Base):
    """PLATFORM.md 3.8: one application hands a request to another. The **dossier** is what the
    person saw before the click and what the receiving side got; ``dossier_hash`` makes it
    unchangeable once it was sent (the way a proposal is, D43). Personal (D20): only ever read or
    written through :class:`topcoat_hub.handover.HandoverService`."""

    __tablename__ = "handovers"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    # an application key, or "incognito" (no conversation: nothing of the context is kept)
    from_application: Mapped[str] = mapped_column(String(40))
    to_application: Mapped[str] = mapped_column(String(40))
    conversation_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("conversations.id", ondelete="SET NULL")
    )
    # the conversation made in the receiving application, where the work and the answer are
    target_conversation_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("conversations.id", ondelete="SET NULL")
    )
    task_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    dossier: Mapped[dict[str, Any]] = mapped_column(JSONB)
    dossier_hash: Mapped[str] = mapped_column(String(64))
    # proposed | sent | done | failed | cancelled
    status: Mapped[str] = mapped_column(String(12), default="proposed")
    result_summary: Mapped[str | None] = mapped_column(Text)
    error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # the Akte (handover_file.py): data and text, and the file of the finished one in the
    # person's knowledge source "Akten"
    file: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    file_markdown: Mapped[str | None] = mapped_column(Text)
    knowledge_path: Mapped[str | None] = mapped_column(String(200))


class ProjectChange(Base):
    """A change the Projekte chat prepared and the person has not decided yet
    (``project_changes.py``): the exact call, immutable (``content_hash``), applied or refused by
    one click of the owner. ``status``: pending | running | applied | refused | failed."""

    __tablename__ = "project_changes"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    task_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), index=True)
    capability: Mapped[str] = mapped_column(String(60))
    input: Mapped[dict[str, Any]] = mapped_column(JSONB)
    preview: Mapped[dict[str, Any]] = mapped_column(JSONB)
    content_hash: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(10), default="pending")
    result: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class Project(Base):
    """A project of the Projekte application. Personal (``user_id``) and scoped to the
    application (``application`` = ``project``); only ``topcoat_hub.project_store`` reads or
    writes it. ``title`` is unique per person. ``scope`` says what the project agent may do in
    this project (see ``topcoat_hub.project_scope``). ``revision`` is the optimistic lock."""

    __tablename__ = "projects"
    __table_args__ = (
        UniqueConstraint("user_id", "application", "title", name="uq_projects_title"),
        Index(
            "uq_projects_identifier",
            "user_id",
            "identifier",
            unique=True,
            postgresql_where=text("identifier IS NOT NULL"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        index=True,
        nullable=True,
    )
    application: Mapped[str] = mapped_column(String(40), default="project")
    title: Mapped[str] = mapped_column(String(200))
    description: Mapped[str] = mapped_column(Text, default="")
    # active | paused | done | dropped
    status: Mapped[str] = mapped_column(String(12), default="active")
    scope: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict, server_default="{}")
    # the counter for the top-level numbers of this project (1, 2, ...)
    next_child: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    # a project can sit below another one; the identifier (at most 10 characters) prefixes its
    # numbers
    parent_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("projects.id", ondelete="SET NULL")
    )
    favorite: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")
    color: Mapped[str | None] = mapped_column(String(20))
    identifier: Mapped[str | None] = mapped_column(String(10))
    # in the trash: hidden, reversible (a project is deleted for good only by hand)
    trashed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    revision: Mapped[int] = mapped_column(Integer, default=1)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=True
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=True
    )


class ProjectNode(Base):
    """A node of a project's numbered tree: a topic (a folder), a task, or a report. ``number``
    is its identity inside the project (``1``, ``1.2``, ``1.2.1``) and is never changed or reused;
    a move changes ``parent_id`` only. ``parent_id`` points only inside the same
    project; what blocks a node is a relation (``ProjectRelation``), which may cross projects.
    The tree rules are checked by ``topcoat_hub.project_tree``."""

    __tablename__ = "project_nodes"
    __table_args__ = (
        UniqueConstraint("project_id", "number", name="uq_project_nodes_number"),
        CheckConstraint("kind IN ('topic', 'task', 'report')", name="ck_project_nodes_kind"),
        CheckConstraint("priority BETWEEN 0 AND 5", name="ck_project_nodes_priority"),
        CheckConstraint("percent_done BETWEEN 0 AND 100", name="ck_project_nodes_percent"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    project_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("projects.id", ondelete="CASCADE"),
        index=True,
        nullable=True,
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        index=True,
        nullable=True,
    )
    application: Mapped[str] = mapped_column(String(40), default="project")
    number: Mapped[str] = mapped_column(String(40))
    parent_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("project_nodes.id", ondelete="CASCADE")
    )
    # topic | task | report
    kind: Mapped[str] = mapped_column(String(8), default="task")
    # for a report: note | question | decision | document
    report_kind: Mapped[str | None] = mapped_column(String(10))
    title: Mapped[str] = mapped_column(String(200))
    body: Mapped[str] = mapped_column(Text, default="")
    # tasks only: todo | in_progress | blocked | done | dropped
    status: Mapped[str | None] = mapped_column(String(12))
    # task fields (PPH-04, 04b); percent_done is derived from the children for a node that has some
    start_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    due_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    end_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    priority: Mapped[int] = mapped_column(SmallInteger, default=0, server_default="0")
    percent_done: Mapped[int] = mapped_column(SmallInteger, default=0, server_default="0")
    done_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    color: Mapped[str | None] = mapped_column(String(20))
    estimate_minutes: Mapped[int | None] = mapped_column(Integer)
    # {every, unit, mode, tz?}, see project_repeat.py
    repeat_rule: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    # a milestone is a task with a flag (it needs a due or an end date)
    is_milestone: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")
    favorite: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")
    trashed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # a report: the date its content describes (not the date it was written), who it came from,
    # whether
    # it still holds, and which report it replaced
    as_of: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    origin_app: Mapped[str | None] = mapped_column(String(40))
    current: Mapped[bool] = mapped_column(Boolean, default=True, server_default="true")
    supersedes_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("project_nodes.id", ondelete="SET NULL")
    )
    # the counter for the numbers of this node's children
    next_child: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    # a question of the person is answered here
    answer: Mapped[str | None] = mapped_column(Text)
    answered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # a message between applications (PPH-05, messages.py): its type, the sending and receiving
    # application, the chain it belongs to, the round, its state, the payload the registry checked,
    # how far to trust it, and what the code made of it (statistics, the verification)
    message_type: Mapped[str | None] = mapped_column(String(40), index=True)
    source_app: Mapped[str | None] = mapped_column(String(10))
    target_app: Mapped[str | None] = mapped_column(String(10))
    chain_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("project_chains.id", ondelete="SET NULL"), index=True
    )
    in_reply_to: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("project_nodes.id", ondelete="SET NULL")
    )
    round: Mapped[int] = mapped_column(Integer, default=1, server_default="1")
    msg_state: Mapped[str | None] = mapped_column(String(20))
    # the agent task that works on the message (a request, an order, a review)
    task_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("agent_tasks.id", ondelete="SET NULL"), index=True
    )
    payload: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    schema_version: Mapped[int | None] = mapped_column(SmallInteger)
    trust: Mapped[str | None] = mapped_column(String(10))
    stats: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    verification: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    # iterations of a task (project_iterations.py, 2026-11-17): on a result its number, the result
    # it continued and the feedback it answered; on a task what every iteration must keep and what
    # the running one was started with
    iteration: Mapped[int | None] = mapped_column(Integer)
    based_on_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("project_nodes.id", ondelete="SET NULL")
    )
    feedback: Mapped[str | None] = mapped_column(Text)
    requirements: Mapped[list[dict[str, Any]] | None] = mapped_column(JSONB)
    next_iteration: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    revision: Mapped[int] = mapped_column(Integer, default=1)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=True
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=True
    )

    @property
    def depends_on(self) -> list[str]:
        """The ids of the nodes this one is blocked by: an open ``blocked_by`` relation each (set by
        the store when it loads a node; the relations are the truth, see ``ProjectRelation``)."""
        return list(getattr(self, "_depends_on", []))

    @depends_on.setter
    def depends_on(self, value: list[str]) -> None:
        self._depends_on = [str(v) for v in value]


class ProjectBatch(Base):
    """One approval of several task nodes, run one after the other (PPH-04, batch of 2026-11-08):
    the next task starts when the last result fits its form; the batch stops at the first that does
    not. ``node_ids`` is the order, ``max_runs`` the limit the person approved. Personal (D20)."""

    __tablename__ = "project_batches"
    __table_args__ = (
        CheckConstraint(
            "status IN ('active', 'done', 'stopped')", name="ck_project_batches_status"
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    project_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("projects.id", ondelete="CASCADE"), index=True
    )
    node_ids: Mapped[list[str]] = mapped_column(JSONB)
    max_runs: Mapped[int] = mapped_column(Integer)
    runs: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    status: Mapped[str] = mapped_column(String(10), default="active", server_default="active")
    stop_reason: Mapped[str | None] = mapped_column(String(60))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    stopped_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class ProjectChain(Base):
    """A conversation between the applications about one project (PPH-05): the first request or
    order starts it and everything that follows from it, also what Development asks of others,
    carries its id. The hub resolves a chain to its project; the applications only see the opaque
    id. ``flagged_at`` marks a chain that has run long or through many rounds (a hint, never a
    limit); ``stopped_at`` is the person's stop. Personal (D20): only through ``message_store``."""

    __tablename__ = "project_chains"
    __table_args__ = (
        CheckConstraint("status IN ('active', 'done', 'stopped')", name="ck_project_chains_status"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    application: Mapped[str] = mapped_column(String(40), default="project")
    project_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("projects.id", ondelete="CASCADE"), index=True
    )
    title: Mapped[str] = mapped_column(String(200), default="")
    status: Mapped[str] = mapped_column(String(10), default="active", server_default="active")
    rounds: Mapped[int] = mapped_column(Integer, default=1, server_default="1")
    flagged_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    flag_reason: Mapped[str | None] = mapped_column(String(40))
    stopped_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=True
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=True
    )


class ProjectMandate(Base):
    """The person's written release of one branch (PPH-05, 05d): orders that match it exactly are
    sent without a further click, and the hub may push that branch. Created only by the click on a
    card that shows this scope; ``branch`` is an exact name below ``topcoat/``. Personal (D20): only
    through ``project_mandates``."""

    __tablename__ = "project_mandates"
    __table_args__ = (
        CheckConstraint("branch LIKE 'topcoat/%'", name="ck_project_mandates_branch"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    application: Mapped[str] = mapped_column(String(40), default="project")
    project_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("projects.id", ondelete="CASCADE"), index=True
    )
    chain_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    target: Mapped[str] = mapped_column(String(10), default="dev", server_default="dev")
    actions: Mapped[list[str]] = mapped_column(JSONB)
    repo: Mapped[str] = mapped_column(String(200))
    branch: Mapped[str] = mapped_column(String(200))
    push: Mapped[bool] = mapped_column(Boolean, default=True, server_default="true")
    text: Mapped[str] = mapped_column(Text, default="", server_default="")
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    uses: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class ProjectEvent(Base):
    """One line of a project's Akte (``project_events.py``): append-only, hash-chained per project.
    ``project_id`` and ``node_id`` are plain ids without a foreign key, and the titles are copies,
    so the line survives a purge of its node and stays readable. Personal (D20): only through
    :class:`topcoat_hub.project_events.EventLog`."""

    __tablename__ = "project_events"
    __table_args__ = (
        UniqueConstraint("project_id", "seq", name="uq_project_events_seq"),
        Index("ix_project_events_node", "project_id", "node_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    application: Mapped[str] = mapped_column(String(40), default="project")
    seq: Mapped[int] = mapped_column(BigInteger)
    at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    project_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True))
    node_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    project_title: Mapped[str] = mapped_column(String(200))
    node_ref: Mapped[str | None] = mapped_column(String(260))
    node_title: Mapped[str | None] = mapped_column(String(200))
    # person | agent | app | system
    actor_kind: Mapped[str] = mapped_column(String(10))
    actor_app: Mapped[str | None] = mapped_column(String(40))
    agent_task_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    chain_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    change_set_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    # the lines of one action that appear in several projects
    group_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    kind: Mapped[str] = mapped_column(String(40))
    payload: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    reason: Mapped[str] = mapped_column(Text, default="", server_default="")
    payload_hash: Mapped[str] = mapped_column(String(64))
    prev_hash: Mapped[str] = mapped_column(String(64))
    hash: Mapped[str] = mapped_column(String(64))


class ProjectNodeVersion(Base):
    """A version of a node's title and body (``project_versions.py``): a snapshot or a line diff
    against the version before it. ``rev`` is the node's revision after the change."""

    __tablename__ = "project_node_versions"

    node_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("project_nodes.id", ondelete="CASCADE"), primary_key=True
    )
    rev: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    application: Mapped[str] = mapped_column(String(40), default="project")
    # snapshot | diff
    kind: Mapped[str] = mapped_column(String(8))
    data: Mapped[dict[str, Any]] = mapped_column(JSONB)
    event_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))


class ProjectLabel(Base):
    """A label of a person's projects (``title`` is unique per person). Personal (D20)."""

    __tablename__ = "project_labels"
    __table_args__ = (
        UniqueConstraint("user_id", "application", "title", name="uq_project_labels_title"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    application: Mapped[str] = mapped_column(String(40), default="project")
    title: Mapped[str] = mapped_column(String(60))
    color: Mapped[str | None] = mapped_column(String(20))
    description: Mapped[str] = mapped_column(String(200), default="", server_default="")


class ProjectNodeLabel(Base):
    """A label on a node."""

    __tablename__ = "project_node_labels"

    node_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("project_nodes.id", ondelete="CASCADE"), primary_key=True
    )
    label_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("project_labels.id", ondelete="CASCADE"), primary_key=True
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), index=True
    )


class ProjectRelation(Base):
    """A relation between two nodes of one person, across projects if need be (PPH-04, 04b). One row
    per pair and kind; it is never deleted while its nodes exist: ending it sets ``valid_to``, so
    "5.2 was blocked by 1.4 from … to …" can be told. ``blocked_by`` reads "``node`` is blocked by
    ``other``", ``precedes`` "``node`` comes before ``other``", ``duplicate_of`` and ``copied_from``
    "``node`` is a duplicate/copy of ``other``", ``supersedes`` "``node`` replaces ``other``";
    ``related``
    is symmetric and stored once."""

    __tablename__ = "project_relations"
    __table_args__ = (
        CheckConstraint(
            "kind IN ('blocked_by', 'related', 'duplicate_of', 'precedes', 'copied_from',"
            " 'supersedes')",
            name="ck_project_relations_kind",
        ),
        CheckConstraint("node_id <> other_id", name="ck_project_relations_self"),
        Index(
            "uq_project_relations_open",
            "node_id",
            "other_id",
            "kind",
            unique=True,
            postgresql_where=text("valid_to IS NULL"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    application: Mapped[str] = mapped_column(String(40), default="project")
    node_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("project_nodes.id", ondelete="CASCADE"), index=True
    )
    other_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("project_nodes.id", ondelete="CASCADE"), index=True
    )
    kind: Mapped[str] = mapped_column(String(14))
    valid_from: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    valid_to: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class ProjectReminder(Base):
    """A reminder of a task: at an absolute time, or relative to its due, start or end date.
    ``fire_at`` is the instant it fires (recomputed in the transaction that changes a date; NULL
    while the date it hangs on is missing); ``fired_at`` marks it as done, so a reminder that came
    due while the hub was down is still delivered once."""

    __tablename__ = "project_reminders"
    __table_args__ = (
        CheckConstraint(
            "(at IS NOT NULL) <> (relative_to IS NOT NULL)", name="ck_project_reminders_kind"
        ),
        CheckConstraint(
            "relative_to IS NULL OR relative_to IN ('due', 'start', 'end')",
            name="ck_project_reminders_base",
        ),
        Index("ix_project_reminders_due", "fire_at", postgresql_where=text("fired_at IS NULL")),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    application: Mapped[str] = mapped_column(String(40), default="project")
    node_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("project_nodes.id", ondelete="CASCADE"), index=True
    )
    at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    relative_to: Mapped[str | None] = mapped_column(String(5))
    offset_seconds: Mapped[int | None] = mapped_column(Integer)
    fire_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    fired_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=True
    )


class ProjectView(Base):
    """A saved view: a filter, a sort rule and a layout under a title (``project_view_defs.py``).
    A view with a ``project_id`` belongs to that project, one without works across all of the
    person's projects. ``builtin`` names a seeded view. Personal (D20)."""

    __tablename__ = "project_views"
    __table_args__ = (
        CheckConstraint(
            "type IN ('list', 'table', 'board', 'calendar', 'gantt')", name="ck_project_views_type"
        ),
        Index(
            "uq_project_views_builtin",
            "user_id",
            "builtin",
            unique=True,
            postgresql_where=text("builtin IS NOT NULL"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    application: Mapped[str] = mapped_column(String(40), default="project")
    project_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("projects.id", ondelete="CASCADE"), index=True
    )
    title: Mapped[str] = mapped_column(String(80))
    # list | table | board | calendar | gantt
    type: Mapped[str] = mapped_column(String(10))
    filter: Mapped[str] = mapped_column(Text, default="", server_default="")
    sort: Mapped[str] = mapped_column(String(200), default="", server_default="")
    config: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict, server_default="{}")
    favorite: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")
    position: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    builtin: Mapped[str | None] = mapped_column(String(20))
    revision: Mapped[int] = mapped_column(Integer, default=1, server_default="1")


class ProjectChangeSet(Base):
    """Up to 100 operations the project agent composed, applied by one click of the person
    (``project_changesets.py``). Immutable: ``content_hash`` covers the project and the operations;
    only the state, the applied items, the result and the decision time change."""

    __tablename__ = "project_change_sets"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    application: Mapped[str] = mapped_column(String(40), default="project")
    project_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("projects.id", ondelete="CASCADE"), index=True
    )
    task_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), index=True)
    title: Mapped[str] = mapped_column(String(200), default="", server_default="")
    summary: Mapped[str] = mapped_column(Text, default="", server_default="")
    operations: Mapped[list[dict[str, Any]]] = mapped_column(JSONB)
    preview: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, default=list, server_default="[]")
    content_hash: Mapped[str] = mapped_column(String(64))
    # pending | running | applied | refused | failed
    status: Mapped[str] = mapped_column(String(10), default="pending", server_default="pending")
    applied: Mapped[list[int]] = mapped_column(JSONB, default=list, server_default="[]")
    result: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=True
    )
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class ProjectTemplate(Base):
    """The structure of a project to start new ones from (``project_templates.py``, PPH-04, 04j).
    ``content`` is one JSON document: topics, tasks, labels, relative dates, relations; no event
    history, no reports, no status. Personal: ``name`` is unique per person."""

    __tablename__ = "project_templates"
    __table_args__ = (
        UniqueConstraint("user_id", "application", "name", name="uq_project_templates_name"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    application: Mapped[str] = mapped_column(String(40), default="project")
    name: Mapped[str] = mapped_column(String(80))
    description: Mapped[str] = mapped_column(String(400), default="", server_default="")
    content: Mapped[dict[str, Any]] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=True
    )


class ReleaseBuild(Base):
    """A build the build service of a node made from one pinned commit (NS-01, phases 3 and 4,
    ARCHITECTURE D80). Administration starts it with a click, the build node reports and uploads,
    the admin signs in the browser, and only then is it published to the update channel. Personal
    (D20): only through ``releases.ReleaseStore``."""

    __tablename__ = "release_builds"
    __table_args__ = (
        CheckConstraint(
            "state IN ('requested', 'building', 'built', 'failed', 'published')",
            name="ck_release_builds_state",
        ),
        CheckConstraint("kind IN ('node_release', 'hub_image')", name="ck_release_builds_kind"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    application: Mapped[str] = mapped_column(String(10), default="admin", server_default="admin")
    kind: Mapped[str] = mapped_column(String(16))
    source: Mapped[str] = mapped_column(String(16), default="release_repo")
    ref: Mapped[str] = mapped_column(String(200))
    commit: Mapped[str | None] = mapped_column(String(40))
    version: Mapped[str | None] = mapped_column(String(40))
    state: Mapped[str] = mapped_column(String(12), default="requested")
    node_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    node_name: Mapped[str | None] = mapped_column(String(63))
    # what changed since the last release (the commit subjects), shown before the signing click
    changes: Mapped[str] = mapped_column(Text, default="", server_default="")
    # [{name, os, arch, size, sha256}] as the hub received them
    artifacts: Mapped[list[dict[str, Any]]] = mapped_column(
        JSONB, default=list, server_default="[]"
    )
    smoke: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    # the roll-out attempts of a hub image, newest last: [{at, node, dry_run, ok, output}]
    rollout: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, default=list, server_default="[]")
    log: Mapped[str] = mapped_column(Text, default="", server_default="")
    error: Mapped[str | None] = mapped_column(Text)
    channel: Mapped[str | None] = mapped_column(String(32))
    key_id: Mapped[str | None] = mapped_column(String(16))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class Report(Base):
    """A thorough research as a document (D102). Personal (D20): only read or written through
    :class:`topcoat_hub.report_store.ReportStore`. The text lives in ``ReportVersion``; this row
    is what the Ablage lists."""

    __tablename__ = "reports"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE")
    )
    application: Mapped[str] = mapped_column(String(40), default="research")
    title: Mapped[str] = mapped_column(String(200))
    question: Mapped[str] = mapped_column(Text, default="", server_default="")
    language: Mapped[str] = mapped_column(String(2), default="en", server_default="en")
    depth: Mapped[str] = mapped_column(String(10), default="thorough", server_default="thorough")
    # draft -> reviewed -> released (the person's clicks); only a released version leaves the Ablage
    status: Mapped[str] = mapped_column(String(12), default="draft", server_default="draft")
    current_version: Mapped[int] = mapped_column(Integer, default=1, server_default="1")
    # the version the person released last: what leaves the Ablage (knowledge base, other
    # programs) stays this one while a newer draft is being worked on
    released_version: Mapped[int | None] = mapped_column(Integer)
    task_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("agent_tasks.id", ondelete="SET NULL")
    )
    conversation_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("conversations.id", ondelete="SET NULL")
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    read_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    released_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        CheckConstraint("status IN ('draft', 'reviewed', 'released')", name="ck_reports_status"),
        Index("ix_reports_user_id", "user_id", "created_at"),
        Index("ix_reports_task_id", "task_id"),
    )


class ReportVersion(Base):
    """One immutable version of a report: Markdown with typed blocks, its metadata (models, times,
    counts), the hash of the text, the version it revised and why."""

    __tablename__ = "report_versions"

    report_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("reports.id", ondelete="CASCADE"), primary_key=True
    )
    version: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    parent_version: Mapped[int | None] = mapped_column(Integer)
    # agent (the run that made it) | revision (a run that revised a version) | person
    kind: Mapped[str] = mapped_column(String(12), default="agent", server_default="agent")
    body: Mapped[str] = mapped_column(Text)
    meta: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict, server_default="{}")
    content_hash: Mapped[str] = mapped_column(String(64))
    note: Mapped[str] = mapped_column(Text, default="", server_default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class ReportComment(Base):
    """A comment on a version of a report, anchored at a chapter (the slug of its heading)."""

    __tablename__ = "report_comments"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    report_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("reports.id", ondelete="CASCADE")
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE")
    )
    version: Mapped[int] = mapped_column(Integer)
    anchor: Mapped[str] = mapped_column(String(120), default="", server_default="")
    text: Mapped[str] = mapped_column(Text)
    author: Mapped[str] = mapped_column(String(12), default="person", server_default="person")
    resolved: Mapped[bool] = mapped_column(Boolean, default=False, server_default=false())
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    __table_args__ = (Index("ix_report_comments_report_id", "report_id", "created_at"),)


class ReportImage(Base):
    """A picture a research run placed in its report (2026-11-13): fetched once from the web by a
    node (``web.image``), kept here, and shown from the hub only to the person whose run it was
    (D20): the reader's browser never loads it from the stranger's server. Its page is the source
    the report credits under it."""

    __tablename__ = "report_images"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE")
    )
    task_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("agent_tasks.id", ondelete="SET NULL"), index=True
    )
    content_type: Mapped[str] = mapped_column(String(20))
    data: Mapped[bytes] = mapped_column(LargeBinary)
    image_url: Mapped[str] = mapped_column(Text)
    page_url: Mapped[str] = mapped_column(Text, default="", server_default="")
    caption: Mapped[str] = mapped_column(String(200), default="", server_default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class WikiPage(Base):
    """A page of Topcoat's own wiki: a report of the application ``wiki`` (its versions, status and
    release live there, D102) with a place in the tree. Personal (D20): only read or written
    through :class:`topcoat_hub.wiki_store.WikiStore`. A page changes only by a person's click on
    a project's document (a new draft version) and is read by assistants only as released."""

    __tablename__ = "wiki_pages"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE")
    )
    # the folder: names with "/" between them ("B Hub/Gateway"); the person moves pages
    section: Mapped[str] = mapped_column(String(240))
    title: Mapped[str] = mapped_column(String(200))
    path: Mapped[str] = mapped_column(String(450))  # folder/title, unique per person
    # free labels across the folders ("Entwurf", "Betrieb")
    categories: Mapped[list[str]] = mapped_column(JSONB, default=list, server_default="[]")
    report_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("reports.id", ondelete="CASCADE"), unique=True
    )
    # the project task whose document it was taken from last
    source_node_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("project_nodes.id", ondelete="SET NULL")
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    __table_args__ = (UniqueConstraint("user_id", "path", name="uq_wiki_pages_user_path"),)


class IssueFix(Base):
    """The fix of an issue (issue_fixes.py, ARCHITECTURE D103, D104): the edits a run of
    ``issue_fix`` wrote, what happened when the person applied them in the workshop (branch,
    commit, the diff, the tests) and the roll-out to the hub. Personal (D20): only through
    ``issue_fixes.FixStore``."""

    __tablename__ = "issue_fixes"
    __table_args__ = (
        CheckConstraint(
            "state IN ('draft', 'applying', 'testing', 'passed', 'failed', 'queued', 'live', "
            "'rolled_back', 'refused', 'discarded')",
            name="ck_issue_fixes_state",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    application: Mapped[str] = mapped_column(
        String(10), default="project", server_default="project"
    )
    issue_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("project_nodes.id", ondelete="CASCADE"), index=True
    )
    run_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))  # the issue_fix run
    summary: Mapped[str] = mapped_column(Text, default="", server_default="")
    # [{path, old, new}] and [{path, content}] (new files), as the run wrote them
    edits: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, default=list)
    files: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, default=list)
    content_hash: Mapped[str] = mapped_column(String(64))
    state: Mapped[str] = mapped_column(String(12), default="draft")
    branch: Mapped[str | None] = mapped_column(String(120))
    base_commit: Mapped[str | None] = mapped_column(String(40))
    commit: Mapped[str | None] = mapped_column(String(40))
    diff: Mapped[str] = mapped_column(Text, default="", server_default="")
    # {job_id, state, output, started_at, ended_at}
    tests: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    # {request, state, build, message, output, at}
    rollout: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    error: Mapped[str | None] = mapped_column(Text)
    decided_by: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=True
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=True
    )


class SheetRow(Base):
    """A chord sheet of the program Sheets (``topcoat_hub.sheets``). Personal (D20): only read or
    written through :class:`topcoat_hub.sheets.store.SheetStore`. ``chordpro`` is the stored form:
    the chords as they sound, the capo as ``{x_capo}``; ``key`` and ``capo`` repeat what it says so
    the list needs no parsing. ``sources`` are the pages a research found, each as ChordPro of its
    own, so the sheet can be compared with every one of them later."""

    __tablename__ = "sheets"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    title: Mapped[str] = mapped_column(String(200))
    artist: Mapped[str] = mapped_column(String(200), default="", server_default="")
    instrument: Mapped[str] = mapped_column(String(20), default="guitar", server_default="guitar")
    # the guitar view shows the shapes played with the capo (True) or the chords that sound
    capo_view: Mapped[bool] = mapped_column(Boolean, default=True, server_default=text("true"))
    key: Mapped[str | None] = mapped_column(String(8))
    capo: Mapped[int] = mapped_column(SmallInteger, default=0, server_default="0")
    version_note: Mapped[str] = mapped_column(String(300), default="", server_default="")
    chordpro: Mapped[str] = mapped_column(Text)
    # [{url, title, version, chordpro, note}]: what a research found, as it was found
    sources: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, default=list, server_default="[]")
    report_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("reports.id", ondelete="SET NULL")
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    __table_args__ = (
        CheckConstraint("instrument IN ('guitar', 'piano')", name="ck_sheets_instrument"),
        CheckConstraint("capo >= 0 AND capo <= 12", name="ck_sheets_capo"),
    )
