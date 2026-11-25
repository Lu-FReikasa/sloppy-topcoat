"""One continuous conversation: segments, hand-over and recall (user area, behind
``main_thread_enabled``).

The user sees one history. For the model it is cut into segments: a segment ends when the
conversation rested ``segment_idle_seconds``. Nothing runs on a clock; the cut is derived from the
message times when the next message arrives. Nothing is summarised here and nothing waits: the
running summary of a chat is the digest that ``chat_digest`` brings up to date after every answer
(each answer is treated as the last one), so a new section always has something to start from.
The prompt of a question is built from

* the **hand-over**: the summary of a segment written before the running summary existed, else
  the digest of the chat plus the messages the digest does not cover yet, else the verbatim end
  of the segment,
* up to ``segment_recall_max`` older notes (summaries of earlier segments and digests of the
  chats before this one) that share words with the question,
* the messages of the current segment (as before, trimmed by the context budget).

The model has no memory of its own: what is not in this prompt, it does not know.
"""

import re
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from topcoat_hub.chat import CHARS_PER_TOKEN
from topcoat_hub.config import Settings
from topcoat_hub.digest_store import DigestStore
from topcoat_hub.models import ChatSegment

TAIL_MESSAGES = 4  # verbatim end of a segment the summary does not cover (yet)
TAIL_CHARS = 280
RECALL_POOL = 200  # newest closed segments looked through for a recall
MIN_SHARED_WORDS = 2
WORD = re.compile(r"[\wäöüß]{4,}", re.IGNORECASE)
STOP_WORDS = frozenset(
    {
        "that",
        "this",
        "with",
        "have",
        "from",
        "what",
        "when",
        "where",
        "which",
        "about",
        "eine",
        "einen",
        "einem",
        "einer",
        "dass",
        "nicht",
        "habe",
        "haben",
        "wird",
        "wurde",
        "sind",
        "oder",
        "auch",
        "noch",
        "mein",
        "dein",
        "sein",
        "ihre",
        "kann",
        "mich",
        "dich",
        "sich",
        "mal",
        "wie",
        "warum",
        "weil",
        "aber",
        "wenn",
        "dann",
        "doch",
        "schon",
        "bitte",
        "danke",
    }
)


@dataclass(frozen=True)
class Seg:
    first_id: int
    last_id: int
    started_at: datetime
    ended_at: datetime
    count: int


def _aware(moment: datetime | None, fallback: datetime | None) -> datetime:
    if moment is None:
        moment = fallback if fallback is not None else datetime.now(UTC)
    return moment if moment.tzinfo else moment.replace(tzinfo=UTC)


def parse_breaks(raw: Any) -> list[datetime]:
    """The stored ``segment_breaks`` (ISO timestamps) as aware datetimes; junk is dropped."""
    out: list[datetime] = []
    for item in raw if isinstance(raw, list) else []:
        try:
            moment = datetime.fromisoformat(str(item))
        except ValueError:
            continue
        out.append(moment if moment.tzinfo else moment.replace(tzinfo=UTC))
    return sorted(out)


def split(
    messages: list[Any], idle_seconds: float, breaks: list[datetime] | None = None
) -> list[Seg]:
    """The segments of ``messages`` (oldest first; objects with ``id``, ``created_at``, ``kind``).
    A gap of at least ``idle_seconds`` between two messages ends a segment, and so does a break
    (a moment the user asked for a new section) that lies between them. Progress lines are
    ignored; a message without a time belongs to the segment of the one before it."""
    segs: list[Seg] = []
    cur: list[Any] = []
    last_at: datetime | None = None
    first_at: datetime | None = None
    for m in messages:
        if getattr(m, "kind", "say") == "progress":
            continue
        at = _aware(m.created_at, last_at or datetime.now(UTC))
        cut = last_at is not None and any(last_at < b <= at for b in breaks or [])
        if cur and last_at is not None and (cut or (at - last_at).total_seconds() >= idle_seconds):
            segs.append(Seg(cur[0].id, cur[-1].id, first_at or last_at, last_at, len(cur)))
            cur, first_at = [], None
        if not cur:
            first_at = at
        cur.append(m)
        last_at = at
    if cur and last_at is not None:
        segs.append(Seg(cur[0].id, cur[-1].id, first_at or last_at, last_at, len(cur)))
    return segs


def words_of(text: str) -> set[str]:
    return {w.lower() for w in WORD.findall(text)} - STOP_WORDS


def pick_recalls(question: str, rows: list[Any], limit: int) -> list[Any]:
    """The ``limit`` summaries (objects with ``summary``) that share the most words with the
    question; at least ``MIN_SHARED_WORDS`` shared, newest first on a tie."""
    want = words_of(question)
    scored: list[tuple[int, int, Any]] = []
    for age, row in enumerate(rows):  # rows are newest first
        shared = len(want & words_of(row.summary))
        if shared >= MIN_SHARED_WORDS:
            scored.append((shared, -age, row))
    scored.sort(key=lambda t: (t[0], t[1]), reverse=True)
    return [row for _, _, row in scored[:limit]]


def _day(moment: datetime) -> str:
    return moment.strftime("%Y-%m-%d %H:%M")


def tail_text(messages: list[Any]) -> str:
    lines = [f"{m.role}: {m.content.strip()[:TAIL_CHARS]}" for m in messages[-TAIL_MESSAGES:]]
    return "\n".join(lines)


def block(
    handover: str | None,
    handover_at: datetime | None,
    recalls: list[tuple[datetime, str]],
    max_tokens: int,
) -> str:
    """The text put in front of the question; empty when there is nothing to say. Limited to
    ``max_tokens`` (the hand-over first, recalls only as far as they fit)."""
    if not handover and not recalls:
        return ""
    limit = max(max_tokens * CHARS_PER_TOKEN, 0)
    head = (
        "[Notes about earlier parts of this conversation. You do not see those messages, this is "
        "what was written down; it can be incomplete or wrong, say so when you are unsure.]\n"
    )
    parts = [head]
    used = len(head)
    if handover:
        when = f" ({_day(handover_at)})" if handover_at else ""
        line = f"Where we stopped last time{when}: {handover}\n"
        parts.append(line[: max(limit - used, 0)])
        used += len(parts[-1])
    for at, summary in recalls:
        line = f"Related, {_day(at)}: {summary}\n"
        if used + len(line) > limit:
            break
        parts.append(line)
        used += len(line)
    text = "".join(parts)
    return text + "\n" if len(text) > len(head) else ""


@dataclass
class Plan:
    messages: list[Any]  # the messages of the current segment (the model sees these verbatim)
    block: str  # hand-over and recalls; "" when this is the first segment
    handover_from: str | None  # "summary" | "digest" | "tail" | None
    recalled: int


@dataclass(frozen=True)
class Note:
    """The digest of an earlier chat, offered to the recall like a segment summary."""

    summary: str
    ended_at: datetime | None
    created_at: datetime | None = None


def handover_of(row: Any, digest: Any, ended: list[Any]) -> tuple[str | None, str | None]:
    """The hand-over of a closed segment and where it comes from. ``row``: a summary stored for
    exactly that segment (written before the running summary existed); ``digest``: the running
    summary of the chat, which never waits for anything; ``ended``: the segment's messages. The
    messages the digest does not cover yet are appended verbatim."""
    if row is not None:
        return row.summary, "summary"
    if digest is not None:
        fresh = [m for m in ended if m.id > digest.last_message_id]
        text = digest.summary
        if fresh:
            text += "\nLatest messages, not in the summary yet:\n" + tail_text(fresh)
        return text, "digest"
    if ended:
        return tail_text(ended), "tail"
    return None, None


class SegmentStore:
    def __init__(self, db: AsyncSession, user_id: uuid.UUID) -> None:
        self.db = db
        self.user_id = user_id

    async def for_conversation(self, conversation_id: uuid.UUID) -> list[ChatSegment]:
        """Newest first."""
        rows = await self.db.scalars(
            select(ChatSegment)
            .where(
                ChatSegment.user_id == self.user_id,
                ChatSegment.conversation_id == conversation_id,
            )
            .order_by(ChatSegment.first_message_id.desc())
            .limit(RECALL_POOL)
        )
        return list(rows)

    async def put(
        self,
        conversation_id: uuid.UUID,
        seg: Seg,
        summary: str,
        author: tuple[str | None, str | None],
    ) -> ChatSegment:
        row = ChatSegment(
            user_id=self.user_id,
            conversation_id=conversation_id,
            first_message_id=seg.first_id,
            last_message_id=seg.last_id,
            started_at=seg.started_at,
            ended_at=seg.ended_at,
            summary=summary,
            author_model=author[0],
            author_node=author[1],
        )
        self.db.add(row)
        await self.db.flush()
        return row


class SegmentService:
    """Cuts a conversation and assembles the hand-over. It writes nothing and calls no model."""

    def __init__(self, sessionmaker: async_sessionmaker[AsyncSession], settings: Settings) -> None:
        self.sessionmaker = sessionmaker
        self.settings = settings

    async def plan(
        self,
        user_id: uuid.UUID,
        conversation_id: uuid.UUID,
        messages: list[Any],
        question: str,
        max_tokens: int,
        breaks: list[datetime] | None = None,
        previous: list[uuid.UUID] | None = None,
    ) -> Plan:
        """Cut ``messages`` and assemble what the next prompt gets (read-only). ``previous`` are
        the chats this one continues (newest first): when this chat has not been cut, the
        hand-over comes from the last of them and the recalls from all of them."""
        segs = split(messages, self.settings.segment_idle_seconds, breaks)
        if len(segs) < 2:
            if previous:
                return await self._chain_plan(user_id, previous, messages, question, max_tokens)
            return Plan(list(messages), "", None, 0)
        current, last_closed = segs[-1], segs[-2]
        mine = [m for m in messages if m.id >= current.first_id]
        async with self.sessionmaker() as db:
            rows = await SegmentStore(db, user_id).for_conversation(conversation_id)
            digest = await DigestStore(db, user_id).get(conversation_id)
        ended = [
            m
            for m in messages
            if last_closed.first_id <= m.id <= last_closed.last_id
            and getattr(m, "kind", "say") == "say"
        ]
        stored = {r.first_message_id: r for r in rows}
        handover, source = handover_of(stored.get(last_closed.first_id), digest, ended)
        older = [r for r in rows if r.first_message_id != last_closed.first_id]
        recalls = pick_recalls(question, older, self.settings.segment_recall_max)
        text = block(
            handover,
            last_closed.ended_at,
            [(_aware(r.ended_at, r.created_at), r.summary) for r in recalls],
            max_tokens,
        )
        return Plan(mine, text, source, len(recalls) if text else 0)

    async def _chain_plan(
        self,
        user_id: uuid.UUID,
        previous: list[uuid.UUID],
        messages: list[Any],
        question: str,
        max_tokens: int,
    ) -> Plan:
        """The hand-over of a chat that continues others: the running summary of the nearest one
        (and what it does not cover yet), and everything earlier in the chain that fits the
        question."""
        from topcoat_hub.chat_store import ChatStore

        nearest = previous[0]
        async with self.sessionmaker() as db:
            old = [
                m
                for m in await ChatStore(db, user_id).messages(nearest)
                if getattr(m, "kind", "say") != "progress"
            ]
            segs = split(old, self.settings.segment_idle_seconds)
            pool: list[Any] = []
            for cid in previous:
                pool.extend(await SegmentStore(db, user_id).for_conversation(cid))
            digests = DigestStore(db, user_id)
            digest = await digests.get(nearest)
            earlier = [d for cid in previous[1:] if (d := await digests.get(cid)) is not None]
        if not segs:
            return Plan(list(messages), "", None, 0)
        last = segs[-1]
        stored = {(r.conversation_id, r.first_message_id): r for r in pool}
        ended = [m for m in old if m.id >= last.first_id and getattr(m, "kind", "say") == "say"]
        handover, source = handover_of(stored.get((nearest, last.first_id)), digest, ended)
        notes: list[Any] = [
            r for r in pool if (r.conversation_id, r.first_message_id) != (nearest, last.first_id)
        ]
        notes += [Note(d.summary, d.updated_at) for d in earlier]
        older = sorted(notes, key=lambda r: _aware(r.ended_at, r.created_at), reverse=True)
        recalls = pick_recalls(question, older, self.settings.segment_recall_max)
        text = block(
            handover,
            last.ended_at,
            [(_aware(r.ended_at, r.created_at), r.summary) for r in recalls],
            max_tokens,
        )
        return Plan(list(messages), text, source, len(recalls) if text else 0)
