"""The Akte of a project: an append-only, hash-chained log of everything that happened to it
(brief PPH-04-vikunja-style-planning, 04a; decision D4-3 of brief PPH-00-implementation-prep).

Every change a :class:`topcoat_hub.project_store.ProjectStore` makes writes one event in the same
transaction, so there is no change without a line in the timeline. The events of one project form a
chain: each carries the hash of the one before it, so a changed or removed line shows up when the
chain is checked (:func:`verify`).

The hash covers the metadata and ``payload_hash`` (the hash of the payload and the reason), not the
payload itself. That is what lets a purge remove the texts and keep a chain that still verifies: the
payload and the reason become empty, ``payload_hash`` stays. The titles are copies on purpose, so a
line stays readable after its node is gone; ``project_id`` and ``node_id`` are plain ids without a
foreign key for the same reason.
"""

import contextvars
import hashlib
import json
import logging
import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import event as sa_event
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session

from topcoat_hub.models import Project, ProjectEvent, ProjectNode

log = logging.getLogger(__name__)

APPLICATION = "project"
GENESIS = "0" * 64
ACTOR_KINDS = ("person", "agent", "app", "system")
PAGE = 50
MAX_PAGE = 200


@dataclass(frozen=True)
class Actor:
    """Who made a change. ``agent_task_id`` is the agent task of an agent's change, ``chain_id`` the
    chain of messages it belongs to (PPH-05), ``change_set_id`` the change set that was applied."""

    kind: str = "system"
    app: str | None = None
    agent_task_id: uuid.UUID | None = None
    chain_id: uuid.UUID | None = None
    change_set_id: uuid.UUID | None = None

    def __post_init__(self) -> None:
        if self.kind not in ACTOR_KINDS:
            raise ValueError(f"unknown actor kind: {self.kind}")


PERSON = Actor("person")
SYSTEM = Actor("system")

# the closed list of event kinds, with the labels the web shows (en, de)
KINDS: dict[str, dict[str, str]] = {
    "baseline": {"en": "History starts here", "de": "Hier beginnt der Verlauf"},
    "project.created": {"en": "Project created", "de": "Projekt angelegt"},
    "project.updated": {"en": "Project changed", "de": "Projekt geändert"},
    "project.from_template": {
        "en": "Project created from a template",
        "de": "Projekt aus einer Vorlage angelegt",
    },
    "project.archived": {"en": "Project archived", "de": "Projekt archiviert"},
    "project.scope_changed": {"en": "Grants changed", "de": "Freigaben geändert"},
    "project.trashed": {"en": "Project moved to the trash", "de": "Projekt in den Papierkorb"},
    "project.restored": {"en": "Project restored", "de": "Projekt wiederhergestellt"},
    "project.purged": {"en": "Project deleted for good", "de": "Projekt endgültig gelöscht"},
    "node.created": {"en": "Created", "de": "Angelegt"},
    "node.updated": {"en": "Changed", "de": "Geändert"},
    "node.moved": {"en": "Moved", "de": "Verschoben"},
    "node.status_changed": {"en": "Status changed", "de": "Status geändert"},
    "node.requirements_changed": {"en": "Requirements changed", "de": "Vorgaben geändert"},
    "node.iteration_started": {"en": "New iteration started", "de": "Neue Iteration gestartet"},
    "node.iteration_filed": {"en": "Iteration filed", "de": "Iteration abgelegt"},
    "node.completed": {"en": "Completed", "de": "Erledigt"},
    "node.reopened": {"en": "Reopened", "de": "Wieder geöffnet"},
    "node.claimed": {"en": "Taken on", "de": "Übernommen"},
    "node.trashed": {"en": "Moved to the trash", "de": "In den Papierkorb"},
    "node.restored": {"en": "Restored", "de": "Wiederhergestellt"},
    "node.purged": {"en": "Deleted for good", "de": "Endgültig gelöscht"},
    "body.changed": {"en": "Text changed", "de": "Text geändert"},
    "question.answered": {"en": "Question answered", "de": "Frage beantwortet"},
    "request.created": {"en": "Request prepared", "de": "Anfrage vorbereitet"},
    "request.decided": {"en": "Request decided", "de": "Anfrage entschieden"},
    "request.queued": {"en": "Request sent to the queue", "de": "Anfrage in die Warteschlange"},
    "relation.added": {"en": "Relation added", "de": "Beziehung angelegt"},
    "relation.ended": {"en": "Relation ended", "de": "Beziehung beendet"},
    "label.added": {"en": "Label added", "de": "Label gesetzt"},
    "label.removed": {"en": "Label removed", "de": "Label entfernt"},
    "reminder.added": {"en": "Reminder added", "de": "Erinnerung angelegt"},
    "reminder.removed": {"en": "Reminder removed", "de": "Erinnerung entfernt"},
    "reminder.fired": {"en": "Reminder fired", "de": "Erinnerung ausgelöst"},
    "report.added": {"en": "Report added", "de": "Bericht abgelegt"},
    "report.superseded": {"en": "Report replaced", "de": "Bericht ersetzt"},
    "message.sent": {"en": "Message sent", "de": "Nachricht gesendet"},
    "message.received": {"en": "Message received", "de": "Nachricht erhalten"},
    "message.reviewed": {"en": "Message reviewed", "de": "Nachricht geprüft"},
    "message.state_changed": {"en": "Message state changed", "de": "Nachrichtenstatus geändert"},
    "chain.started": {"en": "Chain started", "de": "Kette gestartet"},
    "chain.flagged": {"en": "Chain flagged", "de": "Kette markiert"},
    "chain.stopped": {"en": "Chain stopped", "de": "Kette gestoppt"},
    "chain.finished": {"en": "Chain finished", "de": "Kette abgeschlossen"},
    "mandate.push_stopped": {"en": "Push stopped", "de": "Push angehalten"},
    "changeset.proposed": {"en": "Change set proposed", "de": "Änderungssatz vorgeschlagen"},
    "changeset.applied": {"en": "Change set applied", "de": "Änderungssatz angewendet"},
    "changeset.refused": {"en": "Change set refused", "de": "Änderungssatz abgelehnt"},
    "mandate.created": {"en": "Mandate created", "de": "Mandat erteilt"},
    "mandate.used": {"en": "Mandate used", "de": "Mandat genutzt"},
    "mandate.revoked": {"en": "Mandate revoked", "de": "Mandat widerrufen"},
}

_ACTOR: contextvars.ContextVar[Actor] = contextvars.ContextVar("project_actor", default=SYSTEM)


def current_actor() -> Actor:
    return _ACTOR.get()


@contextmanager
def acting_as(actor: Actor) -> Iterator[Actor]:
    """Everything the store writes inside this block is recorded as made by ``actor``."""
    token = _ACTOR.set(actor)
    try:
        yield actor
    finally:
        _ACTOR.reset(token)


# --- the hash chain (pure) ----------------------------------------------------------------------


def canonical(value: Any) -> bytes:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str
    ).encode()


def payload_hash(payload: Any, reason: str) -> str:
    return hashlib.sha256(canonical({"payload": payload, "reason": reason})).hexdigest()


def _iso(value: datetime) -> str:
    return value.astimezone(UTC).isoformat()


def core_of(e: ProjectEvent) -> dict[str, Any]:
    """The part of an event the chain covers."""
    return {
        "user_id": str(e.user_id),
        "application": e.application,
        "seq": e.seq,
        "at": _iso(e.at),
        "project_id": str(e.project_id),
        "node_id": None if e.node_id is None else str(e.node_id),
        "project_title": e.project_title,
        "node_ref": e.node_ref,
        "node_title": e.node_title,
        "actor_kind": e.actor_kind,
        "actor_app": e.actor_app,
        "agent_task_id": None if e.agent_task_id is None else str(e.agent_task_id),
        "chain_id": None if e.chain_id is None else str(e.chain_id),
        "change_set_id": None if e.change_set_id is None else str(e.change_set_id),
        "group_id": None if e.group_id is None else str(e.group_id),
        "kind": e.kind,
        "payload_hash": e.payload_hash,
    }


def event_hash(core: dict[str, Any], prev_hash: str) -> str:
    return hashlib.sha256(canonical({"core": core, "prev": prev_hash})).hexdigest()


def verify(events: list[ProjectEvent]) -> list[str]:
    """Check the chain of one project's events (in ``seq`` order). Returns what is wrong, empty
    when the chain holds. A purged payload (None) is not an error: its hash was kept."""
    problems: list[str] = []
    prev_hash, prev_seq = GENESIS, 0
    for e in events:
        if e.seq <= prev_seq:
            problems.append(f"seq {e.seq}: not increasing")
        if e.prev_hash != prev_hash:
            problems.append(f"seq {e.seq}: the chain is broken (a line is missing or changed)")
        if e.payload is not None and e.payload_hash != payload_hash(e.payload, e.reason or ""):
            problems.append(f"seq {e.seq}: the payload does not match its hash")
        if e.hash != event_hash(core_of(e), e.prev_hash):
            problems.append(f"seq {e.seq}: the line was changed")
        prev_hash, prev_seq = e.hash, e.seq
    return problems


# --- writing and reading ------------------------------------------------------------------------


# A change to a project is told to the person's open pages (event ``project_changed`` on the GUI
# stream), whoever made it: the agent, a batch, a click, a tool (the maintainer, 2026-11-17: the
# page showed an agent's change only after leaving the project and coming back). Told after the
# commit, never before: a page that reloads at once must find the change, and a change that is
# rolled back is never told. ``on_change`` is set by the app (main.py); none in scripts and tests.
_CHANGED = "project_changed"
on_change: Callable[[str, str], None] | None = None


@sa_event.listens_for(Session, "after_commit")
def _announce(session: Session) -> None:
    changed = session.info.pop(_CHANGED, None)
    if not changed or on_change is None:
        return
    for user_id, project_id in changed:
        try:
            on_change(user_id, project_id)
        except Exception:  # telling a page must never fail a change
            log.exception("could not announce a project change")


@sa_event.listens_for(Session, "after_rollback")
def _forget(session: Session) -> None:
    session.info.pop(_CHANGED, None)


class EventLog:
    """The only writer of ``project_events``. Called by the store inside the transaction of the
    change; the project row is locked first, so two writers cannot fork the chain."""

    @staticmethod
    async def record(
        db: AsyncSession,
        user_id: uuid.UUID,
        project: Project,
        kind: str,
        *,
        node: ProjectNode | None = None,
        payload: dict[str, Any] | None = None,
        reason: str = "",
        group_id: uuid.UUID | None = None,
        actor: Actor | None = None,
        at: datetime | None = None,
    ) -> ProjectEvent:
        if kind not in KINDS:
            raise ValueError(f"unknown event kind: {kind}")
        actor = actor or current_actor()
        await db.execute(select(Project.id).where(Project.id == project.id).with_for_update())
        last = (
            await db.execute(
                select(ProjectEvent.seq, ProjectEvent.hash)
                .where(ProjectEvent.project_id == project.id)
                .order_by(ProjectEvent.seq.desc())
                .limit(1)
            )
        ).first()
        seq, prev = (1, GENESIS) if last is None else (last.seq + 1, last.hash)
        event = ProjectEvent(
            id=uuid.uuid4(),
            user_id=user_id,
            application=APPLICATION,
            seq=seq,
            at=at or datetime.now(UTC),
            project_id=project.id,
            node_id=None if node is None else node.id,
            project_title=project.title,
            node_ref=None if node is None else f"{project.title} {node.number}",
            node_title=None if node is None else node.title,
            actor_kind=actor.kind,
            actor_app=actor.app,
            agent_task_id=actor.agent_task_id,
            chain_id=actor.chain_id,
            change_set_id=actor.change_set_id,
            group_id=group_id,
            kind=kind,
            payload=payload,
            reason=reason,
            payload_hash=payload_hash(payload, reason),
            prev_hash=prev,
        )
        event.hash = event_hash(core_of(event), prev)
        db.add(event)
        await db.flush()
        # told to the person's open pages once the change is committed (see _announce)
        db.info.setdefault(_CHANGED, set()).add((str(user_id), str(project.id)))
        return event

    @staticmethod
    async def timeline(
        db: AsyncSession,
        user_id: uuid.UUID,
        project_id: uuid.UUID,
        *,
        after: int = 0,
        kinds: list[str] | None = None,
        node_id: uuid.UUID | None = None,
        limit: int = PAGE,
    ) -> list[ProjectEvent]:
        """The events of one project after ``after`` (a ``seq``), oldest first."""
        statement = select(ProjectEvent).where(
            ProjectEvent.user_id == user_id,
            ProjectEvent.application == APPLICATION,
            ProjectEvent.project_id == project_id,
            ProjectEvent.seq > after,
        )
        if kinds:
            statement = statement.where(ProjectEvent.kind.in_(kinds))
        if node_id is not None:
            statement = statement.where(ProjectEvent.node_id == node_id)
        statement = statement.order_by(ProjectEvent.seq).limit(max(1, min(limit, MAX_PAGE)))
        return list((await db.scalars(statement)).all())

    @staticmethod
    async def chain(
        db: AsyncSession, user_id: uuid.UUID, project_id: uuid.UUID
    ) -> list[ProjectEvent]:
        """Every event of the project in order (for the check of the chain)."""
        rows = await db.scalars(
            select(ProjectEvent)
            .where(
                ProjectEvent.user_id == user_id,
                ProjectEvent.application == APPLICATION,
                ProjectEvent.project_id == project_id,
            )
            .order_by(ProjectEvent.seq)
        )
        return list(rows)


def view(e: ProjectEvent) -> dict[str, Any]:
    """An event as the timeline shows it."""
    return {
        "id": str(e.id),
        "seq": e.seq,
        "at": e.at.isoformat(),
        "kind": e.kind,
        "label": KINDS.get(e.kind, {}),
        "project_id": str(e.project_id),
        "project_title": e.project_title,
        "node_id": None if e.node_id is None else str(e.node_id),
        "node_ref": e.node_ref,
        "node_title": e.node_title,
        "actor": {
            "kind": e.actor_kind,
            "app": e.actor_app,
            "agent_task_id": None if e.agent_task_id is None else str(e.agent_task_id),
            "chain_id": None if e.chain_id is None else str(e.chain_id),
            "change_set_id": None if e.change_set_id is None else str(e.change_set_id),
        },
        "group_id": None if e.group_id is None else str(e.group_id),
        "payload": e.payload,
        "reason": e.reason,
    }
