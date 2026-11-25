"""What the parts of the project store share: the errors, the checks, the queries that every part
needs, and the base class that records an event for a change (see ``project_store.py``).

Every query filters by the person (``user_id``) and by the application (``project``); a row of
another person or application looks exactly like a missing row.
"""

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from topcoat_hub import clock
from topcoat_hub.clock import hub_now
from topcoat_hub.models import Project, ProjectEvent, ProjectNode, ProjectRelation, User
from topcoat_hub.project_events import SYSTEM, Actor, EventLog

APPLICATION = "project"
PROJECT_STATUSES = ("active", "paused", "done", "dropped")
NODE_KINDS = ("topic", "task", "report")
TASK_STATUSES = ("todo", "in_progress", "in_review", "blocked", "done", "dropped")
REPORT_KINDS = ("note", "question", "decision", "document", "fact")
RELATION_KINDS = ("blocked_by", "related", "duplicate_of", "precedes", "copied_from", "supersedes")

# marks "not given" where None is a value (a node without a parent)
UNSET: Any = object()


class ProjectError(Exception):
    """Base of the errors the application's callers handle."""


class NotFound(ProjectError):
    """No row of this person in this application (also for another person's row)."""


class Conflict(ProjectError):
    """The row changed since the caller read it (stale ``revision``)."""


class Invalid(ProjectError):
    """A value outside the allowed set, a tree rule that is broken, or a title already taken."""


class StoreBase:
    def __init__(
        self,
        sessionmaker: async_sessionmaker[AsyncSession],
        actor: Actor = SYSTEM,
        default_zone: str = "UTC",
    ) -> None:
        self._sessionmaker = sessionmaker
        self.actor = actor
        # the zone a repeat rule gets when it names none (the hub's zone, D4-4)
        self.default_zone = default_zone

    def with_actor(self, actor: Actor) -> "Any":
        """The same store, recording its changes as made by ``actor``."""
        return type(self)(self._sessionmaker, actor, self.default_zone)

    async def _record(
        self,
        db: AsyncSession,
        user_id: uuid.UUID,
        project: Project,
        kind: str,
        *,
        node: ProjectNode | None = None,
        payload: dict[str, Any] | None = None,
        group_id: uuid.UUID | None = None,
        at: datetime | None = None,
    ) -> ProjectEvent:
        return await EventLog.record(
            db,
            user_id,
            project,
            kind,
            node=node,
            payload=payload,
            group_id=group_id,
            actor=self.actor,
            at=at,
        )


# --- queries ------------------------------------------------------------------------------------


def owned(model: Any, user_id: uuid.UUID) -> Any:
    return (model.user_id == user_id) & (model.application == APPLICATION)


async def get_project(
    db: AsyncSession, user_id: uuid.UUID, project_id: uuid.UUID, *, lock: bool = False
) -> Project:
    statement = select(Project).where(Project.id == project_id, owned(Project, user_id))
    if lock:
        statement = statement.with_for_update()
    project = await db.scalar(statement)
    if project is None:
        raise NotFound("project")
    return project


async def get_node(
    db: AsyncSession, user_id: uuid.UUID, node_id: uuid.UUID, *, lock: bool = False
) -> ProjectNode:
    statement = select(ProjectNode).where(ProjectNode.id == node_id, owned(ProjectNode, user_id))
    if lock:
        statement = statement.with_for_update()
    node = await db.scalar(statement)
    if node is None:
        raise NotFound("node")
    await attach_blockers(db, user_id, [node])
    return node


async def get_nodes(
    db: AsyncSession, user_id: uuid.UUID, project_id: uuid.UUID, *, trashed: bool = False
) -> list[ProjectNode]:
    """The nodes of one project; the trashed ones only when asked for."""
    statement = select(ProjectNode).where(
        ProjectNode.project_id == project_id, owned(ProjectNode, user_id)
    )
    if not trashed:
        statement = statement.where(ProjectNode.trashed_at.is_(None))
    nodes = list(await db.scalars(statement))
    await attach_blockers(db, user_id, nodes)
    return nodes


async def blockers_of(
    db: AsyncSession, user_id: uuid.UUID, node_ids: list[uuid.UUID]
) -> dict[uuid.UUID, list[uuid.UUID]]:
    """For each node the nodes that block it: an open ``blocked_by`` relation, or an open
    ``precedes`` relation the other way round. A trashed blocker is hidden (the relation stays)."""
    out: dict[uuid.UUID, list[uuid.UUID]] = {n: [] for n in node_ids}
    if not node_ids:
        return out
    ids = list(node_ids)
    direct = await db.execute(
        select(ProjectRelation.node_id, ProjectRelation.other_id)
        .join(ProjectNode, ProjectNode.id == ProjectRelation.other_id)
        .where(
            ProjectRelation.user_id == user_id,
            ProjectRelation.node_id.in_(ids),
            ProjectRelation.kind == "blocked_by",
            ProjectRelation.valid_to.is_(None),
            ProjectNode.trashed_at.is_(None),
        )
    )
    for node_id, other_id in direct:
        out[node_id].append(other_id)
    inverse = await db.execute(
        select(ProjectRelation.other_id, ProjectRelation.node_id)
        .join(ProjectNode, ProjectNode.id == ProjectRelation.node_id)
        .where(
            ProjectRelation.user_id == user_id,
            ProjectRelation.other_id.in_(ids),
            ProjectRelation.kind == "precedes",
            ProjectRelation.valid_to.is_(None),
            ProjectNode.trashed_at.is_(None),
        )
    )
    for node_id, before_id in inverse:
        out[node_id].append(before_id)
    return out


async def attach_blockers(db: AsyncSession, user_id: uuid.UUID, nodes: list[ProjectNode]) -> None:
    """Give each loaded node its ``depends_on`` (the ids of what blocks it)."""
    found = await blockers_of(db, user_id, [n.id for n in nodes])
    for node in nodes:
        node.depends_on = [str(b) for b in found.get(node.id, [])]


async def blocker_graph(db: AsyncSession, user_id: uuid.UUID) -> dict[str, set[str]]:
    """Every open blocking relation of the person, across projects, as node -> its blockers."""
    graph: dict[str, set[str]] = {}
    rows = await db.execute(
        select(ProjectRelation.node_id, ProjectRelation.other_id, ProjectRelation.kind).where(
            ProjectRelation.user_id == user_id,
            ProjectRelation.kind.in_(("blocked_by", "precedes")),
            ProjectRelation.valid_to.is_(None),
        )
    )
    for node_id, other_id, kind in rows:
        blocked, blocker = (node_id, other_id) if kind == "blocked_by" else (other_id, node_id)
        graph.setdefault(str(blocked), set()).add(str(blocker))
    return graph


async def user_zone(
    sessionmaker: async_sessionmaker[AsyncSession], user_id: uuid.UUID, hub_zone: str
) -> str:
    """The time zone that applies to a person: their own, else the hub's."""
    async with sessionmaker() as db:
        own = await db.scalar(select(User.timezone).where(User.id == user_id))
    return str(clock.zone(own, hub_zone))


# --- small checks --------------------------------------------------------------------------------


def number_key(number: str) -> tuple[int, ...]:
    """Sort key of a number: ``1.10`` comes after ``1.9``."""
    return tuple(int(part) for part in number.split("."))


def by_number(nodes: list[ProjectNode]) -> list[ProjectNode]:
    return sorted(nodes, key=lambda n: number_key(n.number))


def check_title(title: str) -> None:
    if not title.strip():
        raise Invalid("a title is needed")


async def flush(db: AsyncSession, message: str) -> None:
    """Send the pending rows to the database now, so a constraint (a title already taken) fails
    here, before the event that belongs to the change is written."""
    try:
        await db.flush()
    except IntegrityError as exc:
        await db.rollback()
        raise Invalid(message) from exc


def check_revision(current: int, given: int) -> None:
    if current != given:
        raise Conflict("revision")


def check_one_of(value: str, allowed: tuple[str, ...]) -> None:
    if value not in allowed:
        raise Invalid(f"value must be one of {', '.join(allowed)}")


def bump(row: Any) -> None:
    row.revision = (row.revision or 1) + 1
    row.updated_at = hub_now()


def as_str(value: uuid.UUID | str | None) -> str | None:
    return None if value is None else str(value)
