"""Tidy the memory of the hub with the rules that now apply to new notes (2026-11-21).

Runs inside the hub container (it uses the hub's own store, so every change is a new version with
its reason and can be undone from the note's history):

    docker cp scripts/dev/memory_tidy.py topcoat-hub-1:/tmp/memory_tidy.py
    docker exec topcoat-hub-1 /app/hub/.venv/bin/python /tmp/memory_tidy.py          # dry run
    docker exec topcoat-hub-1 /app/hub/.venv/bin/python /tmp/memory_tidy.py --apply

What it does, only to notes learned from chats (``chat``, ``chat_digest``); rules and styles and
what the person wrote by hand are never touched:

1. a note the new rules call no memory (a project's own state, a snapshot, a note about notes,
   a state of the moment) is archived;
2. a note of the core learned in a program's conversation that is not about the person moves to
   that program's memory;
3. a note the user's message did not state loses ``doc`` and becomes an ``assumption``;
4. an old node name (``node_aliases``) is replaced by the node's name of today;
5. near-duplicates in one memory (same embedder, cosine >= 0.9, no value lost) are merged: the
   most used one stays, the others are superseded by it;
6. disputed notes and open questions are settled the way the hub now does it itself
   (stronger evidence, then the latest word).
"""

import argparse
import asyncio
import math
import re
import uuid
from collections import Counter, defaultdict
from typing import Any

from sqlalchemy import select

from topcoat_hub import memory_filter
from topcoat_hub.config import Settings
from topcoat_hub.db import make_engine, make_sessionmaker
from topcoat_hub.memory_clarify import (
    UNPROVEN_REASON,
    auto_verdict,
    entities,
    supports,
    unproven,
)
from topcoat_hub.memory_scope import DEFAULT_APPLICATION, about_the_person
from topcoat_hub.memory_store import MemoryStore
from topcoat_hub.models import (
    PSEUDO_USER_IDS,
    Conversation,
    MemoryItem,
    Message,
    Node,
    NodeAlias,
)

LEARNED = ("chat", "chat_digest")
ACTOR = "consolidation"
WHY = "tidy-up 2026-11-21"
DUP_COSINE = float(__import__("os").environ.get("TIDY_COSINE", "0.9"))


def vector(value: Any) -> list[float]:
    """A stored embedding as numbers (it may come back as the text "[0.1,0.2,…]")."""
    if isinstance(value, str):
        return [float(x) for x in value.strip("[]").split(",") if x.strip()]
    return [float(x) for x in value]


def cosine(a: Any, b: Any) -> float:
    a, b = vector(a), vector(b)
    dot = sum(x * y for x, y in zip(a, b, strict=False))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    return dot / (na * nb) if na and nb else 0.0


async def user_message(db: Any, note: MemoryItem) -> str | None:
    ref = note.source_ref or {}
    try:
        conversation = uuid.UUID(str(ref["conversation_id"]))
        answer_id = int(ref["message_id"])
    except (KeyError, ValueError, TypeError):
        return None
    return await db.scalar(
        select(Message.content)
        .where(
            Message.user_id == note.user_id,
            Message.conversation_id == conversation,
            Message.role == "user",
            Message.id < answer_id,
        )
        .order_by(Message.id.desc())
        .limit(1)
    )


async def program_of(db: Any, note: MemoryItem) -> str | None:
    raw = (note.source_ref or {}).get("conversation_id")
    try:
        conversation = uuid.UUID(str(raw))
    except (ValueError, TypeError):
        return None
    return await db.scalar(
        select(Conversation.application).where(
            Conversation.id == conversation, Conversation.user_id == note.user_id
        )
    )


async def tidy_user(db: Any, user_id: uuid.UUID, apply: bool, out: Counter) -> None:
    store = MemoryStore(db, user_id)
    notes = [
        n
        for n in await store.live()
        if n.source_type in LEARNED and n.kind in ("person", "preference", "operational")
    ]
    gone: set[uuid.UUID] = set()
    # the embedding is deferred: read it once here, before changes expire it again
    vectors: dict[uuid.UUID, list[float]] = {}
    for n in notes:
        await db.refresh(n, ["embedding"])
        if n.embedding is not None:
            vectors[n.id] = vector(n.embedding)
        if not apply:
            db.expunge(n)  # a dry run changes its copies only, nothing is flushed

    async def change(note: MemoryItem, what: str, **kw: Any) -> None:
        out[what] += 1
        print(f"  {what:12} [{note.application or 'core'}] {note.text[:110]}")
        if apply:
            await store.change(note.id, actor=ACTOR, **kw)

    # 1. no memory by the new rules
    for n in notes:
        if rule := memory_filter.transient(n.text):
            await change(n, f"archive:{rule}", reason=f"{WHY}: {rule}, not a memory", status="archived")
            gone.add(n.id)
    notes = [n for n in notes if n.id not in gone]

    # 2. the core keeps what is about the person
    for n in notes:
        if n.application is None and n.kind in ("person", "preference") and not about_the_person(n.text):
            program = await program_of(db, n)
            if program and program != DEFAULT_APPLICATION:
                out["move"] += 1
                print(f"  {'move':12} [core -> {program}] {n.text[:110]}")
                if apply:
                    await store.set_placement(n.id, application=program)
                    n.application = program

    # 3. evidence: only what the user's message states is "doc"
    for n in notes:
        if n.source_type == "chat" and n.evidence == "doc":
            said = await user_message(db, n)
            if said is not None and not supports(said, n.text):
                await change(
                    n,
                    "assumption",
                    reason=f"{WHY}: the user's message does not state it",
                    evidence="assumption",
                )
                n.evidence = "assumption"

    # 4. old node names
    renames: dict[str, str] = {}
    for alias, current in (
        await db.execute(select(NodeAlias.name, Node.name).join(Node, Node.id == NodeAlias.node_id))
    ).all():
        if alias != current:
            renames[alias] = current
    for n in notes:
        text = n.text
        for old, new in renames.items():
            text = re.sub(rf"\b{re.escape(old)}\b", new, text)
        if text != n.text:
            await change(n, "rename", reason=f"{WHY}: the node is called so today", text_=text)
            n.text = text

    # 5. near-duplicates within one memory
    groups: dict[tuple[str | None, str | None], list[MemoryItem]] = defaultdict(list)
    for n in notes:
        if n.id in vectors and n.status == "active":
            groups[(n.application, n.embedder)].append(n)
    for group in groups.values():
        group.sort(key=lambda n: (n.used_count or 0, n.recalled_count or 0, n.created_at), reverse=True)
        kept: list[MemoryItem] = []
        for n in group:
            twin = next(
                (
                    k
                    for k in kept
                    if cosine(vectors[k.id], vectors[n.id]) >= DUP_COSINE
                    and entities(n.text, [k.text]) <= entities(k.text, [n.text])
                ),
                None,
            )
            if twin is None:
                kept.append(n)
                continue
            await change(
                n,
                "merge",
                reason=f"{WHY}: the same as “{twin.text}”",
                status="superseded",
                superseded_by=twin.id,
                disputed_with=None,
            )
            gone.add(n.id)

    # 6. disputes and open questions: the hub decides
    live = {n.id: n for n in await store.live()}
    disputes: list[tuple[Any, list[MemoryItem]]] = [
        (q, [live[uuid.UUID(i)] for i in q.item_ids if uuid.UUID(i) in live])
        for q in await store.questions("open")
    ]
    # disputed pairs that no open question holds any more
    seen = {n.id for _, group in disputes for n in group}
    for n in live.values():
        partner = live.get(n.disputed_with) if n.disputed_with else None
        if n.status == "disputed" and n.id not in seen and partner is not None:
            disputes.append((None, [n, partner]))
            seen |= {n.id, partner.id}
    for q, group in disputes:
        verdict = auto_verdict(group) if len(group) >= 2 else None
        if verdict is None and unproven(group):
            out["unproven"] += len(group)
            for n in group:
                print(f"  {'unproven':12} [{n.application or 'core'}] {n.text[:100]}")
                if apply:
                    await store.change(
                        n.id,
                        reason=f"{WHY}: {UNPROVEN_REASON}",
                        actor=ACTOR,
                        status="archived",
                        disputed_with=None,
                    )
            if apply:
                await store.close_questions_about([n.id for n in group], UNPROVEN_REASON)
            continue
        if verdict is None:
            if q is not None and len(group) < 2:
                out["question_closed"] += 1
                if apply:
                    await store.close_question(q.id, "answered", f"{WHY}: nothing left to decide")
            continue
        out["settled"] += 1
        print(f"  {'settle':12} keeps “{verdict.winner.text[:80]}”")
        if apply:
            for loser in verdict.losers:
                await store.change(
                    loser.id,
                    reason=f"{WHY}: {verdict.reason}",
                    actor=ACTOR,
                    status="superseded",
                    superseded_by=verdict.winner.id,
                    disputed_with=None,
                )
            if verdict.winner.status == "disputed":
                await store.change(
                    verdict.winner.id,
                    reason=f"{WHY}: {verdict.reason}",
                    actor=ACTOR,
                    status="active",
                    disputed_with=None,
                )
            await store.close_questions_about(
                [verdict.winner.id, *(n.id for n in verdict.losers)], f"{WHY}: {verdict.reason}"
            )


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true", help="write (default: dry run)")
    args = parser.parse_args()
    settings = Settings()
    sessionmaker = make_sessionmaker(make_engine(settings.database_url))
    out: Counter = Counter()
    async with sessionmaker() as db:
        users = [
            u
            for u in (await db.scalars(select(MemoryItem.user_id).distinct())).all()
            if u not in PSEUDO_USER_IDS
        ]
    for user_id in users:
        print(f"user {user_id}")
        async with sessionmaker() as db:
            await tidy_user(db, user_id, args.apply, out)
            if args.apply:
                await db.commit()
    print("\nsummary:", dict(sorted(out.items())), "(applied)" if args.apply else "(dry run)")


if __name__ == "__main__":
    asyncio.run(main())
