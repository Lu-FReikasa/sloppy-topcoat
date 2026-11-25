"""The trash (PPH-04, 04b; decision 3 of PPH-04 section 1). Deleting exists but is hidden, always by
hand, never by an agent: no tool of the project agent reaches anything in this module.

* **Move to the trash** hides a project or a node with its sub-tree (``trashed_at``); it can be
  restored, relations stay but are hidden.
* **Delete permanently** works on what is in the trash, with the title typed to confirm, in two
  modes. ``keep_timeline`` (the default) removes the rows and the texts of their lines (payload and
  reason of the events, and all versions); the lines themselves stay with title, reference and time,
  and the chain of the project still verifies, because the payload hash stays.
  ``remove_everything`` (a whole project only, for a mistaken or test project) removes the events
  too; only a content-free line in the hub's log remains.
* There is no automatic purge.
"""

import logging
import uuid
from typing import Literal

from sqlalchemy import delete, select, update

from topcoat_hub.clock import hub_now
from topcoat_hub.models import Project, ProjectEvent, ProjectNode
from topcoat_hub.project_base import (
    Invalid,
    StoreBase,
    bump,
    get_node,
    get_project,
    owned,
)

log = logging.getLogger(__name__)

PurgeMode = Literal["keep_timeline", "remove_everything"]
MODES = ("keep_timeline", "remove_everything")


async def _subtree(db, user_id: uuid.UUID, root: ProjectNode) -> list[ProjectNode]:  # type: ignore[no-untyped-def]
    """The node and all its descendants (also the trashed ones)."""
    rows = list(
        await db.scalars(
            select(ProjectNode).where(
                ProjectNode.project_id == root.project_id, owned(ProjectNode, user_id)
            )
        )
    )
    children: dict[uuid.UUID | None, list[ProjectNode]] = {}
    for n in rows:
        children.setdefault(n.parent_id, []).append(n)
    out, stack = [], [root]
    while stack:
        current = stack.pop()
        out.append(current)
        stack.extend(children.get(current.id, []))
    return out


class TrashMixin(StoreBase):
    # --- projects --------------------------------------------------------------------------------

    async def trash_project(self, user_id: uuid.UUID, project_id: uuid.UUID) -> Project:
        async with self._sessionmaker() as db:
            project = await get_project(db, user_id, project_id, lock=True)
            if project.trashed_at is not None:
                raise Invalid("the project is in the trash already")
            project.trashed_at = hub_now()
            bump(project)
            await self._record(db, user_id, project, "project.trashed")
            await db.commit()
            return project

    async def restore_project(self, user_id: uuid.UUID, project_id: uuid.UUID) -> Project:
        async with self._sessionmaker() as db:
            project = await get_project(db, user_id, project_id, lock=True)
            if project.trashed_at is None:
                raise Invalid("the project is not in the trash")
            project.trashed_at = None
            bump(project)
            await self._record(db, user_id, project, "project.restored")
            await db.commit()
            return project

    async def purge_project(
        self,
        user_id: uuid.UUID,
        project_id: uuid.UUID,
        *,
        confirm_title: str,
        mode: PurgeMode = "keep_timeline",
    ) -> None:
        if mode not in MODES:
            raise Invalid(f"mode must be one of {', '.join(MODES)}")
        async with self._sessionmaker() as db:
            project = await get_project(db, user_id, project_id, lock=True)
            if project.trashed_at is None:
                raise Invalid("only a project in the trash is deleted for good")
            if confirm_title != project.title:
                raise Invalid("type the title of the project to confirm")
            if mode == "keep_timeline":
                await db.execute(
                    update(ProjectEvent)
                    .where(ProjectEvent.project_id == project.id)
                    .values(payload=None, reason="")
                )
                await self._record(db, user_id, project, "project.purged", payload={"mode": mode})
            else:
                await db.execute(delete(ProjectEvent).where(ProjectEvent.project_id == project.id))
                log.info("a project was deleted with everything it had (mode %s)", mode)
            await db.delete(
                project
            )  # its nodes, versions, labels on them, relations, reminders go with it
            await db.commit()

    # --- nodes (the sub-tree goes with its root)
    # ---------------------------------------------------

    async def trash_node(self, user_id: uuid.UUID, node_id: uuid.UUID) -> ProjectNode:
        async with self._sessionmaker() as db:
            node = await get_node(db, user_id, node_id)
            project = await get_project(db, user_id, node.project_id, lock=True)
            if node.trashed_at is not None:
                raise Invalid("the node is in the trash already")
            when = hub_now()
            branch = [n for n in await _subtree(db, user_id, node) if n.trashed_at is None]
            for n in branch:
                n.trashed_at = when
            bump(node)
            await self._record(
                db, user_id, project, "node.trashed", node=node, payload={"nodes": len(branch)}
            )
            await db.commit()
            return node

    async def restore_node(self, user_id: uuid.UUID, node_id: uuid.UUID) -> ProjectNode:
        async with self._sessionmaker() as db:
            node = await get_node(db, user_id, node_id)
            project = await get_project(db, user_id, node.project_id, lock=True)
            if node.trashed_at is None:
                raise Invalid("the node is not in the trash")
            if project.trashed_at is not None:
                raise Invalid("the project is in the trash: restore it first")
            if node.parent_id is not None:
                parent = await get_node(db, user_id, node.parent_id)
                if parent.trashed_at is not None:
                    raise Invalid("the node below which it sat is in the trash: restore that first")
            when = node.trashed_at
            branch = [n for n in await _subtree(db, user_id, node) if n.trashed_at == when]
            for n in branch:
                n.trashed_at = None
            bump(node)
            await self._record(
                db, user_id, project, "node.restored", node=node, payload={"nodes": len(branch)}
            )
            await db.commit()
            return node

    async def purge_node(
        self, user_id: uuid.UUID, node_id: uuid.UUID, *, confirm_title: str
    ) -> None:
        """Delete a node of the trash with its sub-tree for good. The lines of the timeline stay
        (``keep_timeline``); removing everything is for whole projects only."""
        async with self._sessionmaker() as db:
            node = await get_node(db, user_id, node_id)
            project = await get_project(db, user_id, node.project_id, lock=True)
            if node.trashed_at is None:
                raise Invalid("only a node in the trash is deleted for good")
            if confirm_title != node.title:
                raise Invalid("type the title of the node to confirm")
            branch = await _subtree(db, user_id, node)
            ids = [n.id for n in branch]
            # also the lines about it in the Akte of another project (a relation across projects)
            await db.execute(
                update(ProjectEvent)
                .where(ProjectEvent.user_id == user_id, ProjectEvent.node_id.in_(ids))
                .values(payload=None, reason="")
            )
            await self._record(
                db, user_id, project, "node.purged", node=node, payload={"nodes": len(ids)}
            )
            await db.execute(delete(ProjectNode).where(ProjectNode.id.in_(ids)))
            await db.commit()

    # --- what is in the trash
    # ----------------------------------------------------------------------

    async def list_trash(
        self, user_id: uuid.UUID
    ) -> tuple[list[Project], list[tuple[ProjectNode, Project]]]:
        """The trashed projects, and the trashed nodes that are the root of a trashed branch (a node
        below a trashed parent is restored with it), each with its project."""
        async with self._sessionmaker() as db:
            projects = list(
                await db.scalars(
                    select(Project)
                    .where(owned(Project, user_id), Project.trashed_at.is_not(None))
                    .order_by(Project.trashed_at.desc())
                )
            )
            nodes = list(
                await db.scalars(
                    select(ProjectNode)
                    .where(owned(ProjectNode, user_id), ProjectNode.trashed_at.is_not(None))
                    .order_by(ProjectNode.trashed_at.desc())
                )
            )
            by_id = {n.id: n for n in nodes}
            roots = [
                n
                for n in nodes
                if n.parent_id is None
                or n.parent_id not in by_id
                or by_id[n.parent_id].trashed_at != n.trashed_at
            ]
            titles = {
                p.id: p
                for p in await db.scalars(
                    select(Project).where(
                        owned(Project, user_id), Project.id.in_({n.project_id for n in roots})
                    )
                )
            }
            return projects, [(n, titles[n.project_id]) for n in roots if n.project_id in titles]
