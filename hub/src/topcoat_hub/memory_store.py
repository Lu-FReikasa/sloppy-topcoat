"""Data access for the assistant's memory (D19, D20).

Everything here belongs to one person. A :class:`MemoryStore` is created for exactly one user
and **every statement it runs filters by that user's id** — including the vector and keyword
search that feed a chat prompt and the pair search of the consolidation job. Another user's
item id answers "not found". Every change of an item also writes a version row with its
reason, so nothing is lost silently.
"""

import re
import uuid
from collections.abc import Sequence
from datetime import datetime
from typing import Any

from sqlalchemy import and_, case, delete, exists, false, func, or_, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from topcoat_hub import core_rules, memory_filter
from topcoat_hub.memory_scope import (
    DEFAULT_APPLICATION,
    PROGRAM_KINDS,
    SHARED_KINDS,
    Scope,
    about_the_person,
    suggest_application,
)
from topcoat_hub.memory_tags import TagTree, default_tag
from topcoat_hub.models import (
    HOUSEHOLD_ID,
    PSEUDO_USER_IDS,
    SYSTEM_ID,
    Conversation,
    MemoryBlock,
    MemoryItem,
    MemoryItemTag,
    MemoryProposal,
    MemoryQuestion,
    MemoryRun,
    MemoryVersion,
)

STATUSES = ("active", "superseded", "disputed", "deleted", "archived")
EVIDENCE = ("live", "doc", "assumption")
TEXT_MAX = 1000
_UNSET: Any = object()

# E3-06 -----------------------------------------------------------------------------------------
FACT_KINDS = ("person", "preference", "operational")  # what is recalled, consolidated, checked
CORE_KINDS = ("style", "rule")  # what steers the assistant: changed only by a click of the user
KINDS = (*FACT_KINDS, *CORE_KINDS)
# where text came from that must never write or change an instruction (a web page, a tool result,
# a note file can contain "remember this as a rule": that is data, not the user's word)
UNTRUSTED_SOURCES = ("web", "tool", "kb")
# who may write an instruction (rule / style / core): the user's own hand, the admin's, the seed
CONFIRMING_ACTORS = ("user", "admin", "seed", "migration")
# D48 (owner, explicitly wanted): the one exception — a rule or style the assistant learned from the
# user's own words in a conversation (MemoryStore.learn_instruction): marked, capped, undoable
AGENT_ACTOR = "agent"
MAX_AGENT_INSTRUCTIONS = 20
MIRROR_ACTORS = ("kb_sync", "user", "admin")  # who may change a note mirrored from a note file
MAX_OPEN_PROPOSALS = 10
REJECTED_SILENCE_DAYS = 90
EXAMPLE_MAX = 240


class PlacementError(ValueError):
    """A note cannot go there: a fact about a place or a day of work is always a program's own
    (D74), never the core."""


class ProtectedWrite(Exception):
    """The write would change what steers the assistant (or a note mirrored from a file, or what
    belongs to the household or the system) without the confirmation the rules demand."""


class SecretBlocked(Exception):
    """The text looks like a secret (the D19 filter): it is not stored, not even as a proposal."""

    def __init__(self, rule: str) -> None:
        super().__init__(rule)
        self.rule = rule


def normalise_text(value: str) -> str:
    return " ".join(value.split())[:TEXT_MAX]


def _key(value: str) -> str:
    """Comparison key: case, spacing and trailing punctuation do not make a new fact."""
    return re.sub(r"\W+", " ", value.casefold()).strip()


def vector_literal(vec: Sequence[float]) -> str:
    return "[" + ",".join(repr(float(x)) for x in vec) + "]"


def keyword_query(query: str) -> str | None:
    """A ``to_tsquery('simple', …)`` string OR-ing the words of ``query`` (None if none)."""
    words = {w for w in re.findall(r"[^\W_]{3,}", query.casefold())}
    return " | ".join(sorted(f"{w}:*" for w in words)) or None


class MemoryStore:
    def __init__(
        self,
        db: AsyncSession,
        user_id: uuid.UUID,
        acting_admin: bool = False,
        scope: Scope | None = None,
    ) -> None:
        """``user_id`` is the owner of everything this store reads and writes — a user, or one of
        the two pseudo users that own the household's and the system's notes. ``acting_admin``:
        the caller is an admin (never an API key): the only one who may write those two.
        ``scope``: the program that reads (D74): what it sees is then the core, minus the notes
        of sensitive branches not released for it, and its own memory; none means no limit (the
        consolidation, the person's own pages)."""
        self.db = db
        self.user_id = user_id
        self.acting_admin = acting_admin
        self.scope = scope

    # --- which notes a program may see (D74) ------------------------------------------------

    def _visible(self) -> Any:
        """The condition on ``MemoryItem`` for the reader of this store."""
        s = self.scope
        if s is None:
            return and_(True)
        core = MemoryItem.application.is_(None)
        if s.hidden:
            core = and_(
                core,
                ~exists().where(
                    MemoryItemTag.item_id == MemoryItem.id,
                    MemoryItemTag.tag_id.in_(list(s.hidden)),
                ),
            )
        return or_(core, MemoryItem.application == s.application)

    def _visible_sql(self) -> tuple[str, dict[str, Any]]:
        """The same condition for the hand-written searches."""
        s = self.scope
        if s is None:
            return "", {}
        if s.hidden:
            return (
                " AND ((application IS NULL AND NOT EXISTS (SELECT 1 FROM memory_item_tags t "
                "WHERE t.item_id = memory_items.id "
                "AND t.tag_id = ANY(CAST(:vis_hidden AS uuid[])))) OR application = :vis_app)",
                {"vis_hidden": [str(t) for t in s.hidden], "vis_app": s.application},
            )
        return " AND (application IS NULL OR application = :vis_app)", {"vis_app": s.application}

    # --- tags (D74) -----------------------------------------------------------------------------

    def tree(self) -> TagTree:
        return TagTree(self.db, self.user_id)

    async def tag_names(self, items: Sequence[MemoryItem]) -> dict[uuid.UUID, list[str]]:
        """The names of the tags each note carries."""
        found = await self.tree().tags_of([i.id for i in items])
        return {item_id: [t.name for t in tags] for item_id, tags in found.items()}

    async def _tag_new(self, item: MemoryItem, tags: Sequence[str] | None, actor: str) -> None:
        """A new note of the person gets its tags; one the writer gave none (or none that exists)
        gets the tag of its kind and program, so every note stands somewhere in the tree."""
        if item.scope != "user" or self.user_id in PSEUDO_USER_IDS:
            return
        tree = self.tree()
        ids = await tree.resolve(
            tags or [],
            by="user" if actor in CONFIRMING_ACTORS else "agent",
            allow_new=True,
            fallback=default_tag(item.kind, item.application),
        )
        await tree.add_item_tags(item.id, ids)

    # --- who may write what (E3-06) -------------------------------------------------------------

    def _own_scope(self) -> str:
        if self.user_id == HOUSEHOLD_ID:
            return "household"
        if self.user_id == SYSTEM_ID:
            return "system"
        return "user"

    def _guard(
        self,
        *,
        kind: str,
        core: bool,
        scope: str,
        source_type: str,
        actor: str,
        existing: MemoryItem | None,
        texts: Sequence[str | None] = (),
        changes_content: bool = True,
    ) -> None:
        """Refuse a write the rules do not allow — here, in the store, so no prompt and no
        careless caller can get around it:

        * household and system notes belong to the pseudo users; only an admin writes them, the
          system rules only an admin or the seed — never the assistant;
        * an instruction (a ``rule`` or ``style`` note, or any note that is pinned into the core)
          is written only by the user's own hand (``actor`` user / admin / seed): the assistant
          *proposes* it (:meth:`propose`); text that came from the web, a tool or a note file
          never writes one;
        * a note mirrored from a note file changes only through the sync (the file wins);
        * every such text passes the secret filter.
        """
        if kind not in KINDS:
            raise ValueError(kind)
        if scope != self._own_scope():
            raise ProtectedWrite(f"{scope} notes belong to their own owner, not to {self.user_id}")
        if self.user_id in PSEUDO_USER_IDS and not (
            self.acting_admin or actor in ("seed", "migration")
        ):
            raise ProtectedWrite("only an admin may write household or system notes")
        if scope == "system" and actor not in ("admin", "seed", "migration"):
            raise ProtectedWrite("the system rules are changed by an admin only, never by the AI")
        instruction = (
            kind in CORE_KINDS
            or core
            or (existing is not None and (existing.kind in CORE_KINDS or existing.core))
        )
        if instruction:
            if source_type in UNTRUSTED_SOURCES:
                raise ProtectedWrite(f"text from {source_type} cannot write an instruction")
            if actor == AGENT_ACTOR and (
                source_type != AGENT_ACTOR or scope != "user" or kind not in CORE_KINDS
            ):
                raise ProtectedWrite("the assistant learns only rules and styles of its own user")
            if actor not in (*CONFIRMING_ACTORS, AGENT_ACTOR):
                raise ProtectedWrite(
                    f"an instruction needs the user's own click (actor {actor!r} proposes it)"
                )
        if (
            existing is not None
            and existing.source_type == "kb"
            and changes_content
            and actor not in MIRROR_ACTORS
        ):
            raise ProtectedWrite("a note mirrored from a note file changes only with the file")
        if instruction or scope != "user":
            for value in texts:
                if value and (rule := memory_filter.check(value)):
                    raise SecretBlocked(rule)

    # --- items ---------------------------------------------------------------------------

    async def get(self, item_id: uuid.UUID) -> MemoryItem | None:
        return await self.db.scalar(
            select(MemoryItem).where(MemoryItem.id == item_id, MemoryItem.user_id == self.user_id)
        )

    async def items(
        self,
        q: str | None = None,
        status: str | None = None,
        evidence: str | None = None,
        conversation_id: uuid.UUID | None = None,
        limit: int = 500,
        kind: str | None = None,
        kinds: Sequence[str] | None = None,
        application: str | None = _UNSET,
        tag: str | None = None,
    ) -> list[MemoryItem]:
        """The user's items, newest change first; ``q`` matches the text (case-insensitive
        substring), ``status`` defaults to everything except deleted — and except archived ones,
        unless the search ``q`` is given (an archive stays searchable)."""
        stmt = select(MemoryItem).where(MemoryItem.user_id == self.user_id, self._visible())
        if status:
            stmt = stmt.where(MemoryItem.status == status)
        elif q and q.strip():
            stmt = stmt.where(MemoryItem.status != "deleted")
        else:
            stmt = stmt.where(MemoryItem.status.notin_(("deleted", "archived")))
        if kind:
            stmt = stmt.where(MemoryItem.kind == kind)
        elif kinds is not None:
            stmt = stmt.where(MemoryItem.kind.in_(list(kinds)))
        else:  # the notes page: facts (instructions have their own page)
            stmt = stmt.where(MemoryItem.kind.in_(FACT_KINDS))
        if evidence:
            stmt = stmt.where(MemoryItem.evidence == evidence)
        if application is not _UNSET:  # None: the core; a key: that program's own memory
            stmt = stmt.where(
                MemoryItem.application.is_(None)
                if application is None
                else MemoryItem.application == application
            )
        if tag:  # the tag and everything below it
            tree = self.tree()
            found = await tree.by_name(tag)
            below = await tree.subtree(found) if found is not None else set()
            stmt = stmt.where(
                exists().where(
                    MemoryItemTag.item_id == MemoryItem.id,
                    MemoryItemTag.tag_id.in_(list(below)),
                )
                if below
                else false()
            )
        if q and q.strip():
            escaped = q.strip().replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            stmt = stmt.where(MemoryItem.text.ilike(f"%{escaped}%", escape="\\"))
        if conversation_id is not None:
            stmt = stmt.where(
                MemoryItem.source_ref["conversation_id"].astext == str(conversation_id)
            )
        stmt = stmt.order_by(MemoryItem.updated_at.desc(), MemoryItem.id).limit(limit)
        return list(await self.db.scalars(stmt))

    async def counts_by_application(self) -> dict[str, int]:
        """How many live notes each program holds; ``core`` for the notes
        about the person that every program reads."""
        rows = await self.db.execute(
            select(MemoryItem.application, func.count())
            .where(
                MemoryItem.user_id == self.user_id,
                MemoryItem.scope == "user",
                MemoryItem.status.in_(("active", "disputed")),
                MemoryItem.kind.in_(FACT_KINDS),
            )
            .group_by(MemoryItem.application)
        )
        return {(app or "core"): n for app, n in rows.all()}

    async def counts(self) -> dict[str, int]:
        rows = await self.db.execute(
            select(MemoryItem.status, func.count())
            .where(MemoryItem.user_id == self.user_id, MemoryItem.kind.in_(FACT_KINDS))
            .group_by(MemoryItem.status)
        )
        counts = dict.fromkeys(STATUSES, 0)
        counts.update({status: n for status, n in rows.all()})
        return counts

    async def add(
        self,
        text_: str,
        *,
        embedding: list[float] | None,
        embedder: str | None,
        evidence: str,
        confidence: float,
        source_type: str,
        source_ref: dict[str, Any],
        reason: str,
        actor: str,
        kind: str = "person",
        expires_at: datetime | None = None,
        last_verified_at: datetime | None = None,
        core: bool = False,
        scope: str | None = None,
        example: str | None = None,
        weak: bool | None = None,
        application: str | None = None,
        tags: Sequence[str] | None = None,
    ) -> MemoryItem:
        scope = scope or self._own_scope()
        example = core_rules.clean_example(example) if kind == "style" else None
        if (
            scope == "user"
            and not core
            and application is None
            and kind in ("person", "preference")
            and source_type in ("chat", "chat_digest")
            and (suggested := suggest_application(text_)) is not None
        ):
            # "works on the repository X" is not about the person but about a place: it belongs to
            # that program's memory and is not read out in every other chat
            kind, application = "operational", suggested
        if (
            scope == "user"
            and not core
            and application is None
            and kind in ("person", "preference")
            and source_type in ("chat", "chat_digest")
            and not about_the_person(text_)
        ):
            # learned in a program's conversation and not about the person ("Themes are defined by
            # a manifest"): that program's own memory, not the core every prompt reads (live
            # 2026-11-21: theme details and task plans filled the core)
            program = await self._conversation_program(source_ref)
            if program is not None and program != DEFAULT_APPLICATION:
                kind, application = "operational", program
        self._guard(
            kind=kind,
            core=core,
            scope=scope,
            source_type=source_type,
            actor=actor,
            existing=None,
            texts=(text_, example),
        )
        if weak is None:
            weak = kind == "style" and core_rules.is_weak_style(text_, example)
        belongs = await self._application_for(kind, core, scope, source_ref, application)
        if belongs is None and scope == "user" and kind in PROGRAM_KINDS:
            belongs = DEFAULT_APPLICATION  # a fact about a place is always some program's own
        item = MemoryItem(
            application=belongs,
            kind=kind,
            core=core,
            scope=scope,
            example=example,
            weak=bool(weak),
            expires_at=expires_at,
            last_verified_at=last_verified_at,
            verified_count=1 if last_verified_at is not None else 0,
            user_id=self.user_id,
            text=normalise_text(text_),
            embedding=embedding,
            embedder=embedder,
            status="active",
            confidence=confidence,
            evidence=evidence,
            source_type=source_type,
            source_ref=source_ref,
            version=1,
        )
        self.db.add(item)
        await self.db.flush()
        self._version(item, reason, actor)
        await self._tag_new(item, tags, actor)
        await self.db.flush()
        await self.db.refresh(item)
        return item

    async def _conversation_program(self, source_ref: dict[str, Any]) -> str | None:
        """The program of the conversation a note was learned in, or None."""
        raw = source_ref.get("conversation_id") if isinstance(source_ref, dict) else None
        try:
            conversation_id = uuid.UUID(str(raw)) if raw else None
        except ValueError:
            return None
        if conversation_id is None:
            return None
        found = await self.db.scalar(
            select(Conversation.application).where(
                Conversation.id == conversation_id, Conversation.user_id == self.user_id
            )
        )
        return found or None

    async def _application_for(
        self,
        kind: str,
        core: bool,
        scope: str,
        source_ref: dict[str, Any],
        explicit: str | None,
    ) -> str | None:
        """Where a new note belongs (D74). What is about the person is the core; what is about a
        place belongs to the program of the conversation it was learned in (a conversation from
        before programs was a chat). The caller may say it: an empty string means the core."""
        if explicit is not None:
            return explicit or None
        if scope != "user" or core or kind in SHARED_KINDS:
            return None
        raw = source_ref.get("conversation_id") if isinstance(source_ref, dict) else None
        if not raw:
            return DEFAULT_APPLICATION
        try:
            conversation_id = uuid.UUID(str(raw))
        except ValueError:
            return DEFAULT_APPLICATION
        found = await self.db.scalar(
            select(Conversation.application).where(
                Conversation.id == conversation_id, Conversation.user_id == self.user_id
            )
        )
        return found or DEFAULT_APPLICATION

    async def set_placement(
        self,
        item_id: uuid.UUID,
        *,
        application: str | None = _UNSET,
        tags: Sequence[str] | None = None,
    ) -> MemoryItem | None:
        """Move a note to a program (None = the core) and/or set its tags (names; ``parent/name``
        makes a new one). It changes where a note is seen, not what it says: no new version of the
        text. A fact about a place or a day of work cannot go to the core
        (:class:`PlacementError`)."""
        item = await self.get(item_id)
        if item is None or item.scope != "user":
            return None
        if application is not _UNSET:
            if not application and item.kind in PROGRAM_KINDS:
                raise PlacementError("a fact about a place or a day of work stays in a program")
            item.application = application or None
        if tags is not None:
            tree = self.tree()
            ids = await tree.resolve(
                tags,
                by="user",
                allow_new=True,
                fallback=default_tag(item.kind, item.application),
            )
            await tree.set_item_tags(item.id, ids)
        item.updated_at = func.now()
        await self.db.flush()
        await self.db.refresh(item)
        return item

    def _version(
        self, item: MemoryItem, reason: str, actor: str, source: dict[str, Any] | None = None
    ) -> None:
        self.db.add(
            MemoryVersion(
                item_id=item.id,
                user_id=self.user_id,
                version=item.version,
                text=item.text,
                status=item.status,
                evidence=item.evidence,
                confidence=item.confidence,
                reason=reason,
                actor=actor,
                source=source or {},
            )
        )

    async def change(
        self,
        item_id: uuid.UUID,
        *,
        reason: str,
        actor: str,
        text_: str | None = None,
        embedding: list[float] | None = _UNSET,
        embedder: str | None = _UNSET,
        status: str | None = None,
        evidence: str | None = None,
        confidence: float | None = None,
        disputed_with: uuid.UUID | None = _UNSET,
        superseded_by: uuid.UUID | None = _UNSET,
        checked: bool = False,
        source: dict[str, Any] | None = None,
        kind: str | None = None,
        expires_at: datetime | None = _UNSET,
        core: bool | None = None,
        example: str | None = _UNSET,
    ) -> MemoryItem | None:
        """Change an item and record the new version with its reason; None if it is not the
        user's. Only the given fields change."""
        item = await self.get(item_id)
        if item is None:
            return None
        if example is not _UNSET:
            example = core_rules.clean_example(example) if (kind or item.kind) == "style" else None
        self._guard(
            kind=kind or item.kind,
            core=item.core if core is None else core,
            scope=item.scope,
            source_type=item.source_type,
            actor=actor,
            existing=item,
            texts=(text_, None if example is _UNSET else example),
            changes_content=any(
                v is not None and v is not _UNSET for v in (text_, status, kind, core, example)
            ),
        )
        if core is not None:
            item.core = core
        if example is not _UNSET:
            item.example = example
        if text_ is not None and item.kind == "style":
            item.weak = core_rules.is_weak_style(text_, item.example)
        if kind is not None:
            item.kind = kind
        if expires_at is not _UNSET:
            item.expires_at = expires_at
        if text_ is not None:
            item.text = normalise_text(text_)
        if embedding is not _UNSET:
            item.embedding = embedding
        if embedder is not _UNSET:
            item.embedder = embedder
        if status is not None:
            if status not in STATUSES:
                raise ValueError(status)
            item.status = status
        if evidence is not None:
            if evidence not in EVIDENCE:
                raise ValueError(evidence)
            item.evidence = evidence
        if confidence is not None:
            item.confidence = confidence
        if disputed_with is not _UNSET:
            item.disputed_with = disputed_with
        if superseded_by is not _UNSET:
            item.superseded_by = superseded_by
        item.version += 1
        item.updated_at = func.now()
        if checked:
            item.checked_at = func.now()
        self._version(item, reason, actor, source)
        await self.db.flush()
        await self.db.refresh(item)
        return item

    async def confirm(self, item_id: uuid.UUID) -> MemoryItem | None:
        """The user said it again: count it and remember when. No new version (nothing about
        the note changed) and no ``updated_at`` (that would send it to the consolidation
        again). None if it is not the user's."""
        await self.db.execute(
            update(MemoryItem)
            .where(MemoryItem.id == item_id, MemoryItem.user_id == self.user_id)
            .values(
                confirmed_count=MemoryItem.confirmed_count + 1,
                last_confirmed_at=func.now(),
                trust=1.0,
            )
        )
        item = await self.get(item_id)
        if item is not None:
            await self.db.refresh(item)
        return item

    async def mark_checked(self, item_ids: Sequence[uuid.UUID]) -> None:
        if item_ids:
            await self.db.execute(
                update(MemoryItem)
                .where(MemoryItem.user_id == self.user_id, MemoryItem.id.in_(list(item_ids)))
                .values(checked_at=func.now())
            )

    async def versions(self, item_id: uuid.UUID) -> list[MemoryVersion]:
        """History of one of the user's items, newest first (empty for any other)."""
        rows = await self.db.scalars(
            select(MemoryVersion)
            .where(MemoryVersion.item_id == item_id, MemoryVersion.user_id == self.user_id)
            .order_by(MemoryVersion.version.desc())
        )
        return list(rows)

    async def version(self, item_id: uuid.UUID, number: int) -> MemoryVersion | None:
        return await self.db.scalar(
            select(MemoryVersion).where(
                MemoryVersion.item_id == item_id,
                MemoryVersion.user_id == self.user_id,
                MemoryVersion.version == number,
            )
        )

    async def find_same(self, text_: str, embedder: str | None) -> MemoryItem | None:
        """A live item with the same words (ignoring case and punctuation)."""
        wanted = _key(text_)
        rows = await self.items(limit=2000, status=None)
        archived = await self.items(limit=2000, status="archived")
        for item in [*rows, *archived]:
            if item.status in ("active", "disputed", "archived") and _key(item.text) == wanted:
                return item
        return None

    # --- search --------------------------------------------------------------------------

    async def nearest(
        self, vec: list[float], embedder: str, limit: int = 8
    ) -> list[tuple[MemoryItem, float]]:
        """Live items of this user (same embedder) by cosine similarity, best first."""
        visible, extra = self._visible_sql()
        rows = await self.db.execute(
            text(
                "SELECT id, 1 - (embedding <=> CAST(:q AS vector)) AS sim FROM memory_items "  # noqa: S608 (the fragment is fixed text from _visible_sql)
                "WHERE user_id = :uid AND embedder = :emb AND embedding IS NOT NULL "
                "AND status IN ('active', 'disputed') "
                "AND kind IN ('person', 'preference', 'operational') "
                + visible
                + " ORDER BY embedding <=> CAST(:q AS vector) LIMIT :n"
            ),
            {"q": vector_literal(vec), "uid": self.user_id, "emb": embedder, "n": limit, **extra},
        )
        found = [(row.id, float(row.sim)) for row in rows]
        return await self._with_items(found)

    async def keyword(self, query: str, limit: int = 8) -> list[tuple[MemoryItem, float]]:
        tsq = keyword_query(query)
        if tsq is None:
            return []
        visible, extra = self._visible_sql()
        rows = await self.db.execute(
            text(
                "SELECT id, ts_rank_cd(to_tsvector('simple', text), to_tsquery('simple', :q)) AS r "
                "FROM memory_items WHERE user_id = :uid AND status IN ('active', 'disputed') "
                "AND kind IN ('person', 'preference', 'operational') "
                "AND to_tsvector('simple', text) @@ to_tsquery('simple', :q) "
                + visible
                + " ORDER BY r DESC LIMIT :n"
            ),
            {"q": tsq, "uid": self.user_id, "n": limit, **extra},
        )
        return await self._with_items([(row.id, float(row.r)) for row in rows])

    async def tagged(
        self, tag_ids: set[uuid.UUID], limit: int = 12
    ) -> list[tuple[MemoryItem, float]]:
        """Live facts this reader may see that carry one of these tags, the newest first."""
        rows = await self.db.scalars(
            select(MemoryItem)
            .where(
                MemoryItem.user_id == self.user_id,
                MemoryItem.status.in_(("active", "disputed")),
                MemoryItem.kind.in_(FACT_KINDS),
                self._visible(),
                exists().where(
                    MemoryItemTag.item_id == MemoryItem.id,
                    MemoryItemTag.tag_id.in_(list(tag_ids)),
                ),
            )
            .order_by(MemoryItem.updated_at.desc(), MemoryItem.id)
            .limit(limit)
        )
        return [(i, 1.0) for i in rows]

    async def similar_pairs(
        self, embedder: str, threshold: float, limit: int = 400
    ) -> list[tuple[uuid.UUID, uuid.UUID, float]]:
        """Pairs of the user's active items that are similar enough to be checked together."""
        rows = await self.db.execute(
            text(
                "SELECT a.id AS a, b.id AS b, 1 - (a.embedding <=> b.embedding) AS sim "
                "FROM memory_items a JOIN memory_items b "
                "ON a.user_id = b.user_id AND a.id < b.id AND a.embedder = b.embedder "
                "WHERE a.user_id = :uid AND a.embedder = :emb AND a.status = 'active' "
                "AND a.kind IN ('person', 'preference', 'operational') "
                "AND b.kind IN ('person', 'preference', 'operational') AND b.status = 'active' "
                # one memory at a time (D74): the core with the core, a program with itself
                "AND a.application IS NOT DISTINCT FROM b.application "
                "AND a.embedding IS NOT NULL AND b.embedding IS NOT NULL "
                "AND 1 - (a.embedding <=> b.embedding) >= :t ORDER BY sim DESC LIMIT :n"
            ),
            {"uid": self.user_id, "emb": embedder, "t": threshold, "n": limit},
        )
        return [(row.a, row.b, float(row.sim)) for row in rows]

    async def _with_items(
        self, found: list[tuple[uuid.UUID, float]]
    ) -> list[tuple[MemoryItem, float]]:
        if not found:
            return []
        items = {
            i.id: i
            for i in await self.db.scalars(
                select(MemoryItem).where(
                    MemoryItem.user_id == self.user_id,
                    MemoryItem.id.in_([f[0] for f in found]),
                )
            )
        }
        return [(items[i], score) for i, score in found if i in items]

    async def needing_embedding(self, embedder: str, limit: int = 200) -> list[MemoryItem]:
        rows = await self.db.scalars(
            select(MemoryItem)
            .where(
                MemoryItem.user_id == self.user_id,
                MemoryItem.status.in_(("active", "disputed")),
                MemoryItem.kind.in_(FACT_KINDS),
                (MemoryItem.embedder.is_(None)) | (MemoryItem.embedder != embedder),
            )
            .order_by(MemoryItem.created_at)
            .limit(limit)
        )
        return list(rows)

    async def unchecked(self, limit: int = 500) -> list[MemoryItem]:
        """Active items the consolidation has not looked at since their last change."""
        rows = await self.db.scalars(
            select(MemoryItem)
            .where(
                MemoryItem.user_id == self.user_id,
                MemoryItem.status == "active",
                MemoryItem.kind.in_(FACT_KINDS),
                MemoryItem.source_type != "kb",  # a note file wins: nothing to merge
                (MemoryItem.checked_at.is_(None)) | (MemoryItem.updated_at > MemoryItem.checked_at),
            )
            .order_by(MemoryItem.created_at)
            .limit(limit)
        )
        return list(rows)

    async def top(self, limit: int = 12) -> list[MemoryItem]:
        """The user's most important live notes (surest evidence, highest confidence, newest
        first): what a question about the user itself is answered from."""
        rank = case((MemoryItem.evidence == "live", 0), (MemoryItem.evidence == "doc", 1), else_=2)
        rows = await self.db.scalars(
            select(MemoryItem)
            .where(
                MemoryItem.user_id == self.user_id,
                MemoryItem.status.in_(("active", "disputed")),
                MemoryItem.kind.in_(FACT_KINDS),
                self._visible(),
            )
            .order_by(rank, MemoryItem.confidence.desc(), MemoryItem.updated_at.desc())
            .limit(limit)
        )
        return list(rows)

    async def live(self, limit: int = 2000) -> list[MemoryItem]:
        """Active and disputed notes: what the assistant currently believes or doubts."""
        rows = await self.db.scalars(
            select(MemoryItem)
            .where(
                MemoryItem.user_id == self.user_id,
                MemoryItem.status.in_(("active", "disputed")),
                MemoryItem.kind.in_(FACT_KINDS),
                self._visible(),
            )
            .order_by(MemoryItem.created_at)
            .limit(limit)
        )
        return list(rows)

    async def get_many(self, item_ids: Sequence[uuid.UUID]) -> list[MemoryItem]:
        if not item_ids:
            return []
        rows = await self.db.scalars(
            select(MemoryItem).where(
                MemoryItem.user_id == self.user_id, MemoryItem.id.in_(list(item_ids))
            )
        )
        return list(rows)

    async def superseded_by(self, item_id: uuid.UUID) -> list[MemoryItem]:
        """Notes that were replaced or withdrawn because of ``item_id`` (to undo it)."""
        rows = await self.db.scalars(
            select(MemoryItem).where(
                MemoryItem.user_id == self.user_id, MemoryItem.superseded_by == item_id
            )
        )
        return list(rows)

    # --- clarification questions -------------------------------------------------------------

    async def add_question(
        self, item_ids: Sequence[uuid.UUID], topic: str, origin: str, context: dict[str, Any]
    ) -> MemoryQuestion:
        """Ask the user which of the notes is true; an open question about the same notes is
        reused (one question per doubt)."""
        wanted = {str(i) for i in item_ids}
        for open_q in await self.questions("open"):
            if set(open_q.item_ids) == wanted:
                return open_q
        question = MemoryQuestion(
            user_id=self.user_id,
            item_ids=sorted(wanted),
            topic=topic,
            origin=origin,
            status="open",
            context=context,
        )
        self.db.add(question)
        await self.db.flush()
        await self.db.refresh(question)
        return question

    async def annotate_question(self, question_id: uuid.UUID, **extra: Any) -> None:
        """Add facts to the context of a question (the GUI reads ``question``)."""
        question = await self.question(question_id)
        if question is not None:
            question.context = {**(question.context or {}), **extra}

    async def mark_question_checked(self, item_ids: Sequence[uuid.UUID], signature: str) -> None:
        """Remember that the model read the evidence of this doubt in this state."""
        wanted = {str(i) for i in item_ids}
        for q in await self.questions("open"):
            if set(q.item_ids) == wanted:
                q.context = {**(q.context or {}), "model_checked": signature}

    async def questions(self, status: str | None = "open") -> list[MemoryQuestion]:
        stmt = select(MemoryQuestion).where(MemoryQuestion.user_id == self.user_id)
        if status:
            stmt = stmt.where(MemoryQuestion.status == status)
        rows = await self.db.scalars(stmt.order_by(MemoryQuestion.created_at, MemoryQuestion.id))
        return list(rows)

    async def question(self, question_id: uuid.UUID) -> MemoryQuestion | None:
        return await self.db.scalar(
            select(MemoryQuestion).where(
                MemoryQuestion.id == question_id, MemoryQuestion.user_id == self.user_id
            )
        )

    async def close_question(
        self,
        question_id: uuid.UUID,
        status: str,
        resolution: str,
        answer: dict[str, Any] | None = None,
    ) -> MemoryQuestion | None:
        question = await self.question(question_id)
        if question is None:
            return None
        question.status = status
        question.resolution = resolution
        question.answer = answer
        question.answered_at = func.now()
        await self.db.flush()
        await self.db.refresh(question)
        return question

    async def close_questions_about(self, item_ids: Sequence[uuid.UUID], resolution: str) -> int:
        """The doubt is gone (a note was settled, deleted or reverted): close the open
        questions that name any of these notes."""
        wanted = {str(i) for i in item_ids}
        n = 0
        for q in await self.questions("open"):
            if wanted & set(q.item_ids):
                await self.close_question(q.id, "answered", resolution)
                n += 1
        return n

    async def active(self, limit: int = 2000, mirror: bool = False) -> list[MemoryItem]:
        """The user's active facts (not rules or styles); the notes mirrored from a note
        file only when asked for — the consolidation must not touch them."""
        stmt = select(MemoryItem).where(
            MemoryItem.user_id == self.user_id,
            MemoryItem.status == "active",
            MemoryItem.kind.in_(FACT_KINDS),
        )
        if not mirror:
            stmt = stmt.where(MemoryItem.source_type != "kb")
        rows = await self.db.scalars(stmt.order_by(MemoryItem.created_at).limit(limit))
        return list(rows)

    # --- bulk ----------------------------------------------------------------------------

    async def forget_all(self, reason: str = "forgotten by the user") -> int:
        """Soft-delete everything (each item gets a version; ``restore`` brings it back)."""
        n = 0
        for item in await self.items(limit=100000):
            await self.change(
                item.id,
                reason=reason,
                actor="user",
                status="deleted",
                disputed_with=None,
                superseded_by=None,
            )
            n += 1
        for q in await self.questions("open"):
            await self.close_question(q.id, "dismissed", "all notes were forgotten")
        return n

    async def purge_deleted(self) -> int:
        """Permanently remove items that were soft-deleted (versions go with them)."""
        result = await self.db.execute(
            delete(MemoryItem)
            .where(MemoryItem.user_id == self.user_id, MemoryItem.status == "deleted")
            .returning(MemoryItem.id)
        )
        return len(result.all())

    # --- blocked candidates, runs ----------------------------------------------------------

    async def add_block(self, reason: str, source_type: str, source_ref: dict[str, Any]) -> None:
        self.db.add(
            MemoryBlock(
                user_id=self.user_id, reason=reason, source_type=source_type, source_ref=source_ref
            )
        )
        await self.db.flush()

    async def blocked_count(self) -> int:
        return (
            await self.db.scalar(
                select(func.count())
                .select_from(MemoryBlock)
                .where(MemoryBlock.user_id == self.user_id)
            )
        ) or 0

    async def start_run(self, trigger: str, kind: str = "consolidation") -> MemoryRun:
        run = MemoryRun(
            user_id=self.user_id, trigger=trigger, kind=kind, status="running", stats={}
        )
        self.db.add(run)
        await self.db.flush()
        await self.db.refresh(run)
        return run

    async def finish_run(self, run_id: uuid.UUID, status: str, stats: dict[str, Any]) -> None:
        finished: datetime | Any = func.now()
        await self.db.execute(
            update(MemoryRun)
            .where(MemoryRun.id == run_id, MemoryRun.user_id == self.user_id)
            .values(status=status, stats=stats, finished_at=finished)
        )

    async def runs(self, limit: int = 5, kind: str | None = None) -> list[MemoryRun]:
        stmt = select(MemoryRun).where(MemoryRun.user_id == self.user_id)
        if kind:
            stmt = stmt.where(MemoryRun.kind == kind)
        rows = await self.db.scalars(stmt.order_by(MemoryRun.started_at.desc()).limit(limit))
        return list(rows)

    async def running_run(self, kind: str | None = None) -> MemoryRun | None:
        stmt = select(MemoryRun).where(
            MemoryRun.user_id == self.user_id, MemoryRun.status == "running"
        )
        if kind:
            stmt = stmt.where(MemoryRun.kind == kind)
        return await self.db.scalar(stmt.limit(1))

    # --- care (E3-05): use, feedback, expiry ------------------------------------------------

    async def record_recalled(self, item_ids: Sequence[uuid.UUID]) -> None:
        """These notes went into a prompt."""
        if item_ids:
            await self.db.execute(
                update(MemoryItem)
                .where(MemoryItem.user_id == self.user_id, MemoryItem.id.in_(list(item_ids)))
                .values(recalled_count=MemoryItem.recalled_count + 1)
            )

    async def record_used(self, item_ids: Sequence[uuid.UUID], now: datetime) -> None:
        """The answer used these notes (two signals agreed, see ``memory_care.counts_as_used``)."""
        if item_ids:
            await self.db.execute(
                update(MemoryItem)
                .where(MemoryItem.user_id == self.user_id, MemoryItem.id.in_(list(item_ids)))
                .values(used_count=MemoryItem.used_count + 1, last_used_at=now)
            )

    async def feedback(
        self, item_id: uuid.UUID, positive: bool, now: datetime
    ) -> MemoryItem | None:
        """The user said a note helped / did not fit. It never deletes anything; it weighs
        heavily in the usefulness."""
        column = MemoryItem.feedback_positive if positive else MemoryItem.feedback_negative
        values: dict[str, Any] = {
            "feedback_positive" if positive else "feedback_negative": column + 1
        }
        if positive:
            values["last_used_at"] = now
        await self.db.execute(
            update(MemoryItem)
            .where(MemoryItem.id == item_id, MemoryItem.user_id == self.user_id)
            .values(**values)
        )
        item = await self.get(item_id)
        if item is not None:
            await self.db.refresh(item)
        return item

    async def expired(self, now: datetime) -> list[MemoryItem]:
        """Live notes whose expiry has passed (volatile notes only)."""
        rows = await self.db.scalars(
            select(MemoryItem).where(
                MemoryItem.user_id == self.user_id,
                MemoryItem.status.in_(("active", "disputed")),
                MemoryItem.expires_at.is_not(None),
                MemoryItem.expires_at <= now,
            )
        )
        return list(rows)

    # --- the core: instructions, profile, household, system rules (E3-06) ---------

    async def instructions(self, status: str = "active") -> list[MemoryItem]:
        """This user's style and rule notes."""
        rows = await self.db.scalars(
            select(MemoryItem)
            .where(
                MemoryItem.user_id == self.user_id,
                MemoryItem.scope == "user",
                MemoryItem.kind.in_(CORE_KINDS),
                MemoryItem.status == status,
                self._visible(),
            )
            .order_by(MemoryItem.created_at, MemoryItem.id)
        )
        return list(rows)

    async def profile(self) -> list[MemoryItem]:
        """The facts about this user that may go into the core: active, person / preference, not a
        passing state."""
        rows = await self.db.scalars(
            select(MemoryItem)
            .where(
                MemoryItem.user_id == self.user_id,
                MemoryItem.scope == "user",
                MemoryItem.kind.in_(("person", "preference")),
                MemoryItem.status == "active",
                MemoryItem.expires_at.is_(None),
                self._visible(),
            )
            .order_by(MemoryItem.created_at, MemoryItem.id)
        )
        return list(rows)

    async def pinned_facts(self) -> list[MemoryItem]:
        rows = await self.db.scalars(
            select(MemoryItem).where(
                MemoryItem.user_id == self.user_id,
                MemoryItem.scope == "user",
                MemoryItem.kind.in_(FACT_KINDS),
                MemoryItem.status == "active",
                MemoryItem.core.is_(True),
                self._visible(),
            )
        )
        return list(rows)

    async def household(self) -> list[MemoryItem]:
        """The household's basic facts: owned by the household pseudo user, readable by everybody
        (written by an admin only)."""
        rows = await self.db.scalars(
            select(MemoryItem)
            .where(
                MemoryItem.user_id == HOUSEHOLD_ID,
                MemoryItem.scope == "household",
                MemoryItem.status == "active",
            )
            .order_by(MemoryItem.created_at, MemoryItem.id)
        )
        return list(rows)

    async def system_rules(self, language: str | None = None) -> list[MemoryItem]:
        """The fixed rule block (one note per language), readable by everybody."""
        stmt = select(MemoryItem).where(
            MemoryItem.user_id == SYSTEM_ID,
            MemoryItem.scope == "system",
            MemoryItem.status == "active",
        )
        if language:
            stmt = stmt.where(MemoryItem.source_ref["lang"].astext == language)
        return list(await self.db.scalars(stmt.order_by(MemoryItem.created_at, MemoryItem.id)))

    async def legacy_item(self, key: str) -> MemoryItem | None:
        """The note taken over from the chat settings (``system_prompt``, ``address``,
        ``language``), whatever its status: one the user deleted stays deleted."""
        return await self.db.scalar(
            select(MemoryItem).where(
                MemoryItem.user_id == self.user_id,
                MemoryItem.scope == "user",
                MemoryItem.source_ref["legacy"].astext == key,
            )
        )

    # --- proposals: a change to an instruction is a question to the user -------------------------

    @staticmethod
    def fingerprint(
        action: str, target_id: uuid.UUID | None, kind: str, text_: str, core: bool
    ) -> str:
        import hashlib

        raw = f"{action}|{target_id or ''}|{kind}|{_key(text_)}|{int(core)}"
        return hashlib.sha1(raw.encode(), usedforsecurity=False).hexdigest()

    async def proposals(self, status: str | None = "proposed") -> list[MemoryProposal]:
        stmt = select(MemoryProposal).where(MemoryProposal.user_id == self.user_id)
        if status:
            stmt = stmt.where(MemoryProposal.status == status)
        rows = await self.db.scalars(stmt.order_by(MemoryProposal.created_at, MemoryProposal.id))
        return list(rows)

    async def get_proposal(self, proposal_id: uuid.UUID) -> MemoryProposal | None:
        return await self.db.scalar(
            select(MemoryProposal).where(
                MemoryProposal.id == proposal_id, MemoryProposal.user_id == self.user_id
            )
        )

    async def propose(
        self,
        *,
        action: str,
        kind: str,
        text_: str = "",
        example: str | None = None,
        core: bool = False,
        reason: str,
        evidence: dict[str, Any],
        origin: str,
        source_type: str,
        target: MemoryItem | None = None,
        now: datetime | None = None,
    ) -> tuple[MemoryProposal | None, str]:
        """Queue a change to an instruction for the user to decide: ``(proposal, "ok")``, or
        ``(None, why)`` — ``exists`` (the note is there already), ``limit`` (ten are waiting),
        ``rejected_before`` (the user said no in the last 90 days), ``duplicate`` (the same one is
        waiting: returned as the proposal). Text from the web, a tool or a note file never gets a
        proposal (:class:`ProtectedWrite`), a secret never does (:class:`SecretBlocked`)."""
        from datetime import UTC, timedelta

        if action not in ("add", "change", "remove", "pin", "unpin", "kb_edit"):
            raise ValueError(action)
        if kind not in KINDS:
            raise ValueError(kind)
        if self.user_id in PSEUDO_USER_IDS:
            raise ProtectedWrite("household and system notes are changed by an admin, not proposed")
        if source_type in UNTRUSTED_SOURCES:
            raise ProtectedWrite(f"text from {source_type} cannot propose an instruction")
        text_ = normalise_text(text_)
        example = core_rules.clean_example(example) if kind == "style" else None
        for value in (text_, example, reason):
            if value and (rule := memory_filter.check(value)):
                raise SecretBlocked(rule)
        now = now or datetime.now(UTC)
        fp = self.fingerprint(action, target.id if target else None, kind, text_, core)
        if action == "add" and text_:
            for item in await self.items(limit=2000, kinds=(kind,)):
                if _key(item.text) == _key(text_):
                    return None, "exists"
        same = await self.proposals(None)
        for p in same:
            if p.fingerprint != fp:
                continue
            if p.status == "proposed":
                return p, "duplicate"
            if p.status == "rejected" and p.decided_at is not None:
                decided = p.decided_at if p.decided_at.tzinfo else p.decided_at.replace(tzinfo=UTC)
                if now - decided < timedelta(days=REJECTED_SILENCE_DAYS):
                    return None, "rejected_before"
        if sum(1 for p in same if p.status == "proposed") >= MAX_OPEN_PROPOSALS:
            return None, "limit"
        before = (
            {
                "text": target.text,
                "example": target.example,
                "core": target.core,
                "kind": target.kind,
            }
            if target
            else {}
        )
        after = {"text": text_ or (target.text if target else ""), "example": example, "core": core}
        proposal = MemoryProposal(
            user_id=self.user_id,
            action=action,
            scope="user",
            target_id=target.id if target else None,
            kind=kind,
            text=text_,
            example=example,
            core=core,
            weak=kind == "style" and core_rules.is_weak_style(text_, example),
            reason=" ".join(reason.split())[:500],
            evidence=evidence,
            diff={"before": before, "after": after},
            previous=(
                {**before, "status": target.status, "version": target.version} if target else None
            ),
            origin=origin,
            fingerprint=fp,
        )
        self.db.add(proposal)
        await self.db.flush()
        await self.db.refresh(proposal)
        return proposal, "ok"

    async def learn_instruction(
        self,
        *,
        kind: str,
        text_: str,
        example: str | None,
        evidence: dict[str, Any],
    ) -> tuple[MemoryItem | None, str]:
        """D48: the assistant writes a rule or style itself, from the user's own words (the caller
        checked the quote). ``(item, "ok")`` or ``(None, why)``: ``exists``, ``limit`` (twenty
        learned ones are active), ``loosens`` (it would relax a confirmation or permission: only
        the user's own click does that), ``rejected_before``. The item has
        ``source_type="agent"``: the page shows it as learned, one click removes it."""
        if kind not in CORE_KINDS:
            raise ValueError(kind)
        text_ = normalise_text(text_)
        if core_rules.loosens_safety(text_) or core_rules.loosens_safety(example or ""):
            return None, "loosens"
        for item in await self.items(limit=2000, kinds=(kind,)):
            if _key(item.text) == _key(text_):
                return None, "exists"
        learned = [
            i
            for i in await self.items(limit=2000, kinds=CORE_KINDS)
            if i.source_type == AGENT_ACTOR and i.status == "active"
        ]
        if len(learned) >= MAX_AGENT_INSTRUCTIONS:
            return None, "limit"
        fp = self.fingerprint("add", None, kind, text_, True)
        for p in await self.proposals("rejected"):
            if p.fingerprint == fp:
                return None, "rejected_before"
        item = await self.add(
            text_,
            embedding=None,
            embedder=None,
            evidence="doc",
            confidence=0.8,
            source_type=AGENT_ACTOR,
            source_ref={**evidence, "learned_by_agent": True},
            reason="learned by the assistant from the user's own words",
            actor=AGENT_ACTOR,
            kind=kind,
            core=True,
            example=example,
        )
        return item, "ok"

    async def accept_proposal(
        self,
        proposal_id: uuid.UUID,
        *,
        text_: str | None = None,
        example: str | None = _UNSET,
    ) -> tuple[MemoryProposal, MemoryItem | None] | None:
        """The user's one click: the proposal takes effect as a version (reason, source and
        actor ``user`` say so, ``revert`` / ``restore`` undo it). ``text_`` / ``example``: the user
        edited it first. ``None`` if there is no such open proposal."""
        proposal = await self.get_proposal(proposal_id)
        if proposal is None or proposal.status != "proposed":
            return None
        text_final = normalise_text(text_) if text_ else proposal.text
        example_final = proposal.example if example is _UNSET else example
        source = {"proposal": str(proposal.id), "origin": proposal.origin}
        why = f"accepted proposal ({proposal.origin}): {proposal.reason}"[:600]
        item: MemoryItem | None = None
        target = await self.get(proposal.target_id) if proposal.target_id else None
        if proposal.action == "add":
            item = await self.add(
                text_final,
                embedding=None,
                embedder=None,
                evidence="doc",
                confidence=1.0,
                source_type="user",
                source_ref=source,
                reason=why,
                actor="user",
                kind=proposal.kind,
                core=proposal.core,
                example=example_final,
            )
        elif target is not None and proposal.action == "change":
            item = await self.change(
                target.id,
                reason=why,
                actor="user",
                source=source,
                text_=text_final or None,
                example=example_final,
                core=proposal.core if proposal.core != target.core else None,
            )
        elif target is not None and proposal.action == "remove":
            item = await self.change(
                target.id, reason=why, actor="user", source=source, status="deleted"
            )
        elif target is not None and proposal.action in ("pin", "unpin"):
            item = await self.change(
                target.id, reason=why, actor="user", source=source, core=proposal.action == "pin"
            )
        elif proposal.action != "kb_edit":
            return None  # the note it is about is gone
        from datetime import UTC

        proposal.status = "accepted"
        proposal.decided_at = datetime.now(UTC)
        proposal.decision = {
            "item_id": str(item.id) if item else None,
            "edited": bool(text_ and text_final != proposal.text),
        }
        await self.db.flush()
        return proposal, item

    async def reject_proposal(self, proposal_id: uuid.UUID) -> MemoryProposal | None:
        """No: it is not proposed again for 90 days."""
        proposal = await self.get_proposal(proposal_id)
        if proposal is None or proposal.status != "proposed":
            return None
        from datetime import UTC

        proposal.status = "rejected"
        proposal.decided_at = datetime.now(UTC)
        await self.db.flush()
        return proposal


async def users_with_memory(db: AsyncSession) -> list[uuid.UUID]:
    """System level (the care scheduler): users with notes and memory switched on. The care
    then works on **one user at a time** through a :class:`MemoryStore`."""
    rows = await db.execute(
        text(
            "SELECT DISTINCT m.user_id FROM memory_items m "
            "LEFT JOIN user_settings s ON s.user_id = m.user_id "
            "WHERE m.status IN ('active', 'disputed') AND COALESCE(s.memory_enabled, true) "
            "AND m.user_id NOT IN "
            "(CAST(:household AS uuid), CAST(:system AS uuid))"
        ),
        {"household": str(HOUSEHOLD_ID), "system": str(SYSTEM_ID)},
    )
    return [row.user_id for row in rows]


async def users_with_pending_work(db: AsyncSession) -> list[uuid.UUID]:
    """System level (the scheduler, not a user request): users who have notes the
    consolidation has not looked at and who have memory switched on. The job then works on
    **one user at a time** through a :class:`MemoryStore`."""
    rows = await db.execute(
        text(
            "SELECT DISTINCT m.user_id FROM memory_items m "
            "LEFT JOIN user_settings s ON s.user_id = m.user_id "
            "WHERE m.status = 'active' AND (m.checked_at IS NULL OR m.updated_at > m.checked_at) "
            "AND COALESCE(s.memory_enabled, true) "
            "AND m.kind IN ('person', 'preference', 'operational') AND m.source_type <> 'kb' "
            "AND m.user_id NOT IN (CAST(:household AS uuid), CAST(:system AS uuid))"
        ),
        {"household": str(HOUSEHOLD_ID), "system": str(SYSTEM_ID)},
    )
    return [row.user_id for row in rows]


async def sweep_orphaned_runs(sessionmaker: async_sessionmaker[AsyncSession]) -> int:
    """At startup: a consolidation or care run still ``running`` died with the hub that started
    it (nothing runs yet in a fresh process). Marked ``interrupted``, it no longer keeps the next
    care run from starting."""
    async with sessionmaker() as db:
        result = await db.execute(
            update(MemoryRun)
            .where(MemoryRun.status == "running")
            .values(status="interrupted", finished_at=func.now())
        )
        await db.commit()
        return int(result.rowcount or 0)  # type: ignore[attr-defined]
