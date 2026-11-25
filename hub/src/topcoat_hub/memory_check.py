"""The cross-check of the program memories (ARCHITECTURE D74).

Every program keeps its own memory, so two programs may remember the same topic differently: Chat
noted "the NAS backs up at three", Administration knows it runs at two. The consolidation never
looks across memories; this check does. It compares notes of different memories (the core counts
as one) that resemble each other and asks the model whether they really contradict each other.

A contradiction is reported to the program **responsible for the topic** — found in the tag tree
(:meth:`TagTree.responsible_for`), not the owner of either note. Its assistant decides in its own
frame, for each of the two notes: keep, correct (a new version with the reason "cross-check by
Administration") or retire (archived, restorable). It may tell the program whose note it changed
what happened, in its own words — that notice is all the other program learns. When it cannot
decide, or the topic is the person's (or sensitive), the person gets a question.

Pairs that agree are remembered too, so the same pair in the same state is not looked at again;
a new version of either note makes it a new pair. Nothing here ever fails a care run."""

import hashlib
import logging
import uuid
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from topcoat_hub import chat, memory_filter
from topcoat_hub.config import Settings
from topcoat_hub.gateway import Gateway, GatewayError
from topcoat_hub.memory_clarify import auto_verdict, unproven
from topcoat_hub.memory_store import MemoryStore, normalise_text
from topcoat_hub.memory_tags import PERSON, TagTree
from topcoat_hub.models import MemoryItem, MemoryReport
from topcoat_hub.ui.access import clean_agent, frame_text
from topcoat_hub.ui.store import TemplateStore

log = logging.getLogger(__name__)

MAX_PAIRS = 20  # pairs compared in one run
MAX_DECISIONS = 10  # discrepancies decided in one run
NOTE_MAX = 300
ACTOR = "crosscheck"

DECIDE_SYSTEM = (
    "You are the assistant of the program {program}. This program is responsible for the topic "
    "{topic} in the user's memory. Two notes about that topic, kept by different programs, "
    "contradict each other. Decide for EACH note: keep it, correct it (give the corrected text: "
    "one short sentence in the language of the note) or retire it (it is wrong or outdated). "
    "Decide only from what the notes and your own knowledge of the user's setup make clear; if "
    'you cannot, set "ask" and give ONE short question to the user. You may tell a program '
    "whose note you corrected or retired what happened, in one short sentence: say only what "
    "that program needs to know, nothing else you know. Never put passwords, keys or account "
    "numbers into a text. Reply with JSON only: "
    '{{"A": {{"action": "keep", "text": ""}}, "B": {{"action": "correct", "text": "..."}}, '
    '"ask": false, "question": "", "notify": [{{"note": "B", "message": "..."}}], '
    '"reason": "one short sentence"}}'
)


def signature(a: MemoryItem, b: MemoryItem) -> str:
    """The pair in its current state: either note's new version makes it a new pair."""
    first, second = sorted([(str(a.id), a.version), (str(b.id), b.version)])
    raw = f"{first[0]}:{first[1]}|{second[0]}:{second[1]}"
    return hashlib.sha256(raw.encode()).hexdigest()[:64]


def _memory_name(application: str | None) -> str:
    return application or "core"


class CrossCheck:
    def __init__(
        self,
        sessionmaker: async_sessionmaker[AsyncSession],
        gateway: Gateway,
        memory: Any,
        settings: Settings,
    ) -> None:
        self.sessionmaker = sessionmaker
        self.gateway = gateway
        self.memory = memory  # MemoryService: its embedder, its model and its comparison
        self.settings = settings

    # --- finding ----------------------------------------------------------------------------

    async def pairs(
        self, db: AsyncSession, user_id: uuid.UUID, limit: int = MAX_PAIRS
    ) -> list[tuple[MemoryItem, MemoryItem]]:
        """Similar live facts of two different memories, most similar first, that were not looked
        at in this state yet."""
        rows = await db.execute(
            text(
                "SELECT a.id AS a, b.id AS b FROM memory_items a JOIN memory_items b "
                "ON a.user_id = b.user_id AND a.id < b.id AND a.embedder = b.embedder "
                "WHERE a.user_id = :uid AND a.embedder = :emb AND a.status = 'active' "
                "AND b.status = 'active' AND a.scope = 'user' AND b.scope = 'user' "
                "AND a.kind IN ('person', 'preference', 'operational') "
                "AND b.kind IN ('person', 'preference', 'operational') "
                "AND a.application IS DISTINCT FROM b.application "
                "AND a.embedding IS NOT NULL AND b.embedding IS NOT NULL "
                "AND 1 - (a.embedding <=> b.embedding) >= :t "
                "ORDER BY 1 - (a.embedding <=> b.embedding) DESC LIMIT :n"
            ),
            {
                "uid": user_id,
                "emb": self.memory.embedder.id,
                "t": self.memory.embedder.cluster_threshold,
                "n": limit * 3,
            },
        )
        found = [(row.a, row.b) for row in rows]
        store = MemoryStore(db, user_id)
        items = {i.id: i for i in await store.get_many([x for pair in found for x in pair])}
        seen = set(
            await db.scalars(
                select(MemoryReport.signature).where(
                    MemoryReport.user_id == user_id, MemoryReport.kind != "notice"
                )
            )
        )
        out = []
        for a, b in found:
            if a in items and b in items and signature(items[a], items[b]) not in seen:
                out.append((items[a], items[b]))
            if len(out) >= limit:
                break
        return out

    async def run(self, user_id: uuid.UUID, stats: dict[str, Any]) -> None:
        """One look across the memories of one person, within the run's budget. Never raises."""
        try:
            await self._run(user_id, stats)
        except GatewayError as exc:
            log.info("memory cross-check paused: %s", exc.message)
        except Exception:
            log.exception("memory cross-check failed")

    async def _run(self, user_id: uuid.UUID, stats: dict[str, Any]) -> None:
        stats.setdefault("tokens", 0)
        for key in ("crosschecked", "discrepancies", "agreed", "decided", "asked_person"):
            stats.setdefault(key, 0)
        model = await self.memory._model()
        async with self.sessionmaker() as db:
            pairs = await self.pairs(db, user_id)
        for a, b in pairs:
            relation, _, question = await self.memory._compare([a, b], model, stats)
            if relation == "unknown":
                return  # no model now: the pairs wait for the next run
            stats["crosschecked"] += 1
            async with self.sessionmaker() as db:
                await self._record(db, user_id, a, b, relation, question, stats)
                await db.commit()
        await self.decide_open(user_id, model, stats)

    async def _record(
        self,
        db: AsyncSession,
        user_id: uuid.UUID,
        a: MemoryItem,
        b: MemoryItem,
        relation: str,
        question: str,
        stats: dict[str, Any],
    ) -> None:
        sig = signature(a, b)
        if relation != "contradict":
            db.add(
                MemoryReport(
                    user_id=user_id,
                    kind="agreement",
                    recipient=PERSON,
                    item_a=a.id,
                    item_b=b.id,
                    status="agreed",
                    signature=sig,
                    reason=relation,
                    decision={},
                )
            )
            stats["agreed"] += 1
            return
        tree = TagTree(db, user_id)
        tags = await tree.tags_of([a.id, b.id])
        ids = [t.id for t in tags[a.id] + tags[b.id]]
        responsible = await tree.responsible_for(ids) or PERSON
        deepest = max(tags[a.id] + tags[b.id], key=lambda t: len(t.name), default=None)
        topic = deepest.name if deepest is not None else ""
        report = MemoryReport(
            user_id=user_id,
            kind="discrepancy",
            recipient=responsible,
            item_a=a.id,
            item_b=b.id,
            topic=topic,
            status="open",
            signature=sig,
            reason=question,
            decision={},
        )
        db.add(report)
        await db.flush()
        stats["discrepancies"] += 1
        if responsible == PERSON:
            if await self._ask_person(db, user_id, report, [a, b], question, PERSON):
                stats["asked_person"] += 1
            else:
                stats["decided"] += 1

    # --- deciding ---------------------------------------------------------------------------

    async def decide_open(self, user_id: uuid.UUID, model: str, stats: dict[str, Any]) -> None:
        """The responsible programs decide the open discrepancies, each in its own frame."""
        async with self.sessionmaker() as db:
            open_reports = list(
                await db.scalars(
                    select(MemoryReport)
                    .where(
                        MemoryReport.user_id == user_id,
                        MemoryReport.kind == "discrepancy",
                        MemoryReport.status == "open",
                        MemoryReport.recipient != PERSON,
                    )
                    .order_by(MemoryReport.created_at)
                    .limit(MAX_DECISIONS)
                )
            )
        for report in open_reports:
            if stats["tokens"] >= self.settings.memory_run_max_tokens:
                return
            await self.decide(user_id, report.id, model, stats)

    async def decide(
        self, user_id: uuid.UUID, report_id: uuid.UUID, model: str, stats: dict[str, Any]
    ) -> None:
        async with self.sessionmaker() as db:
            report = await db.scalar(
                select(MemoryReport).where(
                    MemoryReport.id == report_id, MemoryReport.user_id == user_id
                )
            )
            if report is None or report.status != "open":
                return
            store = MemoryStore(db, user_id)
            notes = await store.get_many([i for i in (report.item_a, report.item_b) if i])
            by_id = {n.id: n for n in notes}
            a, b = by_id.get(report.item_a), by_id.get(report.item_b)  # type: ignore[arg-type]
            if a is None or b is None or a.status != "active" or b.status != "active":
                report.status, report.decided_at = "decided", datetime.now(UTC)
                report.decision = {"outcome": "gone"}
                await db.commit()
                return
            program = report.recipient
            template = await TemplateStore(db).get(program)
            frame = frame_text(clean_agent(template.agent)["frame"]) if template else ""
            title = template.title if template else program
        system = DECIDE_SYSTEM.format(program=title, topic=report.topic or "this topic")
        if frame:
            system += f"\n\nThe frame of {title}:\n{frame}"
        content = (
            f"Note A (kept by {_memory_name(a.application)}): {a.text}\n"
            f"Note B (kept by {_memory_name(b.application)}): {b.text}"
            + (f"\nThe point they disagree on: {report.reason}" if report.reason else "")
        )
        payload = {
            "model": model,
            "stream": False,
            "temperature": 0,
            "max_tokens": 400,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": content},
            ],
        }
        data = await self.gateway.call(model, "/v1/chat/completions", payload, label="memory")
        from topcoat_hub.memory import _answer_text, parse_json_object

        answer_text, tokens = _answer_text(data)
        stats["tokens"] += tokens or chat.estimate_tokens(content + answer_text)
        answer = parse_json_object(answer_text) or {}
        async with self.sessionmaker() as db:
            await self.apply(db, user_id, report_id, answer)
            await db.commit()
        stats["decided"] += 1

    async def apply(
        self, db: AsyncSession, user_id: uuid.UUID, report_id: uuid.UUID, answer: dict[str, Any]
    ) -> MemoryReport | None:
        """Carry out what the responsible program decided. Anything unclear is a question to the
        person; a text the secret filter refuses is not written (the note stays as it was)."""
        report = await db.scalar(
            select(MemoryReport).where(
                MemoryReport.id == report_id, MemoryReport.user_id == user_id
            )
        )
        if report is None or report.status != "open":
            return report
        store = MemoryStore(db, user_id)
        notes = {n.id: n for n in await store.get_many([report.item_a, report.item_b])}  # type: ignore[list-item]
        pair = {"A": notes.get(report.item_a), "B": notes.get(report.item_b)}  # type: ignore[arg-type]
        program = report.recipient
        why = " ".join(str(answer.get("reason") or "").split())[:300]
        actions: dict[str, dict[str, Any]] = {}
        for key in ("A", "B"):
            raw = answer.get(key)
            action = raw.get("action") if isinstance(raw, dict) else None
            text_ = normalise_text(str(raw.get("text") or "")) if isinstance(raw, dict) else ""
            if action not in ("keep", "correct", "retire") or (action == "correct" and not text_):
                action = None
            actions[key] = {"action": action, "text": text_}
        # both kept is a decision too: each is right in its own program (nothing changes)
        unclear = answer.get("ask") is True or any(a["action"] is None for a in actions.values())
        if unclear:
            question = " ".join(str(answer.get("question") or report.reason or "").split())[:240]
            group = [n for n in pair.values() if n is not None]
            await self._ask_person(db, user_id, report, group, question, program)
            return report
        done: list[dict[str, Any]] = []
        reason = f"cross-check by {program}" + (f": {why}" if why else "")
        for key, act in actions.items():
            note = pair[key]
            if note is None or act["action"] == "keep":
                continue
            if act["action"] == "correct":
                if memory_filter.check(act["text"]):
                    continue
                vec, embedder = await self.memory.embed_one(act["text"])
                await store.change(
                    note.id,
                    reason=reason,
                    actor=ACTOR,
                    text_=act["text"],
                    embedding=vec,
                    embedder=embedder,
                    source={"report": str(report.id), "program": program},
                )
            else:
                await store.change(
                    note.id,
                    reason=reason,
                    actor=ACTOR,
                    status="archived",
                    source={"report": str(report.id), "program": program},
                )
            done.append({"note": key, "id": str(note.id), "action": act["action"]})
        notices = []
        for entry in answer.get("notify") if isinstance(answer.get("notify"), list) else []:
            if not isinstance(entry, dict) or entry.get("note") not in ("A", "B"):
                continue
            note = pair[entry["note"]]
            message = " ".join(str(entry.get("message") or "").split())[:NOTE_MAX]
            if note is None or not message or memory_filter.check(message):
                continue
            to = note.application or PERSON
            if to == program:
                continue  # its own note: nobody to tell
            db.add(
                MemoryReport(
                    user_id=user_id,
                    kind="notice",
                    recipient=to,
                    sender=program,
                    item_a=note.id,
                    topic=report.topic,
                    status="open",
                    message=message,
                    decision={},
                )
            )
            notices.append(to)
        report.status = "decided"
        report.decided_at = datetime.now(UTC)
        report.decision = {"outcome": "decided", "changes": done, "notified": notices, "why": why}
        await db.flush()
        return report

    async def _ask_person(
        self,
        db: AsyncSession,
        user_id: uuid.UUID,
        report: MemoryReport,
        group: Sequence[MemoryItem],
        question: str,
        where: str,
    ) -> bool:
        """A question to the person about the two notes, shown where the decision lies (the
        responsible program's memory, or Settings for the person's own topics). With
        ``memory_auto_settle`` the hub decides itself (stronger evidence, then the latest word)
        and nobody is asked. True: a question was opened."""
        store = MemoryStore(db, user_id)
        if getattr(self.settings, "memory_auto_settle", True) and (
            verdict := auto_verdict(list(group))
        ):
            await self.memory._apply_verdict(store, verdict, ACTOR)
            report.status = "decided"
            report.decided_at = datetime.now(UTC)
            report.decision = {
                "outcome": "auto",
                "winner": str(verdict.winner.id),
                "why": verdict.reason[:300],
            }
            await db.flush()
            return False
        if getattr(self.settings, "memory_auto_settle", True) and unproven(list(group)):
            await self.memory.retire_unproven(store, list(group), ACTOR)
            report.status = "decided"
            report.decided_at = datetime.now(UTC)
            report.decision = {"outcome": "auto", "why": "unproven: both archived"}
            await db.flush()
            return False
        if len(group) == 2:
            await self.memory._ask(store, list(group), "crosscheck", question)
            wanted = {str(n.id) for n in group}
            for q in await store.questions("open"):
                if set(q.item_ids) == wanted:
                    await store.annotate_question(q.id, program=where, report=str(report.id))
        report.status = "asked"
        report.decided_at = datetime.now(UTC)
        report.decision = {"outcome": "asked", "where": where}
        await db.flush()
        return True

    # --- reading --------------------------------------------------------------------------------

    async def reports(
        self, db: AsyncSession, user_id: uuid.UUID, recipient: str, limit: int = 50
    ) -> list[MemoryReport]:
        """What a program (or the person) was told: discrepancies it decided or must decide, and
        notices from other programs — newest first."""
        rows = await db.scalars(
            select(MemoryReport)
            .where(
                MemoryReport.user_id == user_id,
                MemoryReport.recipient == recipient,
                MemoryReport.kind.in_(("discrepancy", "notice")),
            )
            .order_by(MemoryReport.created_at.desc())
            .limit(limit)
        )
        return list(rows)
