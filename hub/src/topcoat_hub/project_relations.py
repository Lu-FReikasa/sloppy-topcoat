"""Relations between nodes (PPH-04, 04b): blocked by, comes before, related, duplicate of, copy of,
replaces. They may cross project borders (between one person's own projects), they are versioned
(ending a relation sets ``valid_to``, nothing is deleted) and each change is a line in the Akte of
both projects, tied together by one ``group_id``.

Whether a blocking relation would close a loop is checked over the person's whole graph, not one
project's.
"""

import uuid
from typing import Any

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from topcoat_hub.clock import hub_now
from topcoat_hub.models import Project, ProjectNode, ProjectRelation
from topcoat_hub.project_base import (
    RELATION_KINDS,
    Invalid,
    NotFound,
    StoreBase,
    blocker_graph,
    flush,
    get_node,
    get_project,
    owned,
)
from topcoat_hub.project_tree import TreeError, check_blocking

MAX_OPEN_PER_NODE = 200
_TASK_KINDS = ("blocked_by", "precedes")
_REPORT_KINDS = ("supersedes",)


def _ref(project: Project, node: ProjectNode) -> str:
    return f"{project.title} {node.number}"


def _check_pair(kind: str, node: ProjectNode, other: ProjectNode) -> None:
    if kind not in RELATION_KINDS:
        raise Invalid(f"kind must be one of {', '.join(RELATION_KINDS)}")
    if node.id == other.id:
        raise Invalid("a node cannot be related to itself")
    if node.trashed_at is not None or other.trashed_at is not None:
        raise Invalid("a node in the trash cannot be related; restore it first")
    if kind in _TASK_KINDS and (node.kind != "task" or other.kind != "task"):
        raise Invalid(f"{kind} relates two tasks")
    if kind in _REPORT_KINDS and (node.kind != "report" or other.kind != "report"):
        raise Invalid(f"{kind} relates two reports")


class RelationMixin(StoreBase):
    async def add_relation(
        self, user_id: uuid.UUID, node_id: uuid.UUID, other_id: uuid.UUID, kind: str
    ) -> ProjectRelation:
        async with self._sessionmaker() as db:
            node = await get_node(db, user_id, node_id)
            other = await get_node(db, user_id, other_id)
            _check_pair(kind, node, other)
            if kind == "related":  # symmetric: stored once, in a fixed order
                node, other = sorted((node, other), key=lambda n: str(n.id))
            group = uuid.uuid4()
            relation = await self._open(db, user_id, node, other, kind, group)
            await db.commit()
            return relation

    async def end_relation(self, user_id: uuid.UUID, relation_id: uuid.UUID) -> ProjectRelation:
        """End a relation: it stays in the history with the time it stopped to hold."""
        async with self._sessionmaker() as db:
            relation = await db.scalar(
                select(ProjectRelation)
                .where(ProjectRelation.id == relation_id, owned(ProjectRelation, user_id))
                .with_for_update()
            )
            if relation is None or relation.valid_to is not None:
                raise NotFound("relation")
            node = await get_node(db, user_id, relation.node_id)
            other = await get_node(db, user_id, relation.other_id)
            await self._close(db, user_id, relation, node, other, uuid.uuid4())
            await db.commit()
            return relation

    async def relations_of(
        self, user_id: uuid.UUID, node_id: uuid.UUID, *, include_ended: bool = False
    ) -> list[tuple[ProjectRelation, ProjectNode]]:
        """The relations a node takes part in, each with the node on the other side."""
        async with self._sessionmaker() as db:
            await get_node(db, user_id, node_id)
            statement = select(ProjectRelation).where(
                owned(ProjectRelation, user_id),
                or_(ProjectRelation.node_id == node_id, ProjectRelation.other_id == node_id),
            )
            if not include_ended:
                statement = statement.where(ProjectRelation.valid_to.is_(None))
            rows = list(await db.scalars(statement.order_by(ProjectRelation.valid_from)))
            out = []
            for r in rows:
                far = r.other_id if r.node_id == node_id else r.node_id
                node = await db.scalar(
                    select(ProjectNode).where(ProjectNode.id == far, owned(ProjectNode, user_id))
                )
                if node is not None:
                    out.append((r, node))
            return out

    # --- used by the store when a node's blockers change ------------------------------------------

    async def _set_blockers(
        self,
        db: AsyncSession,
        user_id: uuid.UUID,
        node: ProjectNode,
        wanted: list[uuid.UUID],
        group: uuid.UUID,
    ) -> bool:
        """Make the node's direct ``blocked_by`` relations exactly ``wanted`` (opening and ending
        the difference). True when something changed."""
        wanted_ids = list(dict.fromkeys(wanted))
        current = {
            r.other_id: r
            for r in await db.scalars(
                select(ProjectRelation).where(
                    owned(ProjectRelation, user_id),
                    ProjectRelation.node_id == node.id,
                    ProjectRelation.kind == "blocked_by",
                    ProjectRelation.valid_to.is_(None),
                )
            )
        }
        add = [i for i in wanted_ids if i not in current]
        end = [r for i, r in current.items() if i not in set(wanted_ids)]
        if not add and not end:
            return False
        graph = await blocker_graph(db, user_id)
        keep = {str(i) for i in wanted_ids}
        # the node's other blockers (from a "comes before" relation) stay in the picture
        derived = graph.get(str(node.id), set()) - {str(i) for i in current}
        graph.pop(str(node.id), None)
        try:
            check_blocking(str(node.id), sorted(keep | derived), graph)
        except TreeError as exc:
            raise Invalid(str(exc)) from exc
        for relation in end:
            other = await get_node(db, user_id, relation.other_id)
            await self._close(db, user_id, relation, node, other, group)
        for other_id in add:
            other = await get_node(db, user_id, other_id)
            _check_pair("blocked_by", node, other)
            await self._open(db, user_id, node, other, "blocked_by", group, checked=True)
        return True

    # --- one relation, one line per project ------------------------------------------------------

    async def _open(
        self,
        db: AsyncSession,
        user_id: uuid.UUID,
        node: ProjectNode,
        other: ProjectNode,
        kind: str,
        group: uuid.UUID,
        *,
        checked: bool = False,
    ) -> ProjectRelation:
        if not checked and kind in _TASK_KINDS:
            graph = await blocker_graph(db, user_id)
            blocked, blocker = (node, other) if kind == "blocked_by" else (other, node)
            own = graph.pop(
                str(blocked.id), set()
            )  # the node's own blockers are part of the change
            try:
                check_blocking(str(blocked.id), [str(blocker.id), *own], graph)
            except TreeError as exc:
                raise Invalid(str(exc)) from exc
        same = (ProjectRelation.node_id == node.id) & (ProjectRelation.other_id == other.id)
        reverse = (ProjectRelation.node_id == other.id) & (ProjectRelation.other_id == node.id)
        existing = await db.scalar(
            select(ProjectRelation.id).where(
                owned(ProjectRelation, user_id),
                ProjectRelation.valid_to.is_(None),
                ProjectRelation.kind == kind,
                or_(same, reverse) if kind == "related" else same,
            )
        )
        if existing is not None:
            raise Invalid("this relation exists")
        open_count = len(
            list(
                await db.scalars(
                    select(ProjectRelation.id).where(
                        owned(ProjectRelation, user_id),
                        ProjectRelation.valid_to.is_(None),
                        or_(
                            ProjectRelation.node_id == node.id, ProjectRelation.other_id == node.id
                        ),
                    )
                )
            )
        )
        if open_count >= MAX_OPEN_PER_NODE:
            raise Invalid(f"a node has at most {MAX_OPEN_PER_NODE} open relations")
        relation = ProjectRelation(
            id=uuid.uuid4(),
            user_id=user_id,
            application="project",
            node_id=node.id,
            other_id=other.id,
            kind=kind,
            valid_from=hub_now(),
        )
        db.add(relation)
        await flush(db, "this relation exists")
        await self._relation_lines(db, user_id, "relation.added", relation, node, other, group)
        return relation

    async def _close(
        self,
        db: AsyncSession,
        user_id: uuid.UUID,
        relation: ProjectRelation,
        node: ProjectNode,
        other: ProjectNode,
        group: uuid.UUID,
    ) -> None:
        relation.valid_to = hub_now()
        await self._relation_lines(db, user_id, "relation.ended", relation, node, other, group)

    async def _relation_lines(
        self,
        db: AsyncSession,
        user_id: uuid.UUID,
        event_kind: str,
        relation: ProjectRelation,
        node: ProjectNode,
        other: ProjectNode,
        group: uuid.UUID,
    ) -> None:
        # lock the projects in a fixed order, so two relations across the same pair cannot deadlock
        ids = sorted({node.project_id, other.project_id}, key=str)
        projects: dict[uuid.UUID, Project] = {}
        for pid in ids:
            projects[pid] = await get_project(db, user_id, pid, lock=True)
        payload: dict[str, Any] = {
            "relation": relation.kind,
            "node": _ref(projects[node.project_id], node),
            "other": _ref(projects[other.project_id], other),
            "valid_from": relation.valid_from.isoformat(),
            "valid_to": None if relation.valid_to is None else relation.valid_to.isoformat(),
        }
        await self._record(
            db,
            user_id,
            projects[node.project_id],
            event_kind,
            node=node,
            payload=payload,
            group_id=group,
        )
        if other.project_id != node.project_id:
            await self._record(
                db,
                user_id,
                projects[other.project_id],
                event_kind,
                node=other,
                payload=payload,
                group_id=group,
            )
