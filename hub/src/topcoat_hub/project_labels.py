"""Labels of the project application (PPH-04, 04b). A label is a person's own: a title (unique per
person), a colour, a description. Putting a label on a node, or taking it off, is a line in that
node's project; the label itself belongs to no project, so defining one writes no event."""

import uuid
from typing import Any

from sqlalchemy import delete, select

from topcoat_hub.models import ProjectLabel, ProjectNode, ProjectNodeLabel
from topcoat_hub.project_base import (
    Invalid,
    NotFound,
    StoreBase,
    flush,
    get_node,
    get_project,
    owned,
)
from topcoat_hub.project_fields import FieldError, normalize

MAX_LABELS_PER_NODE = 20


async def _label(db: Any, user_id: uuid.UUID, label_id: uuid.UUID) -> ProjectLabel:
    label = await db.scalar(
        select(ProjectLabel).where(ProjectLabel.id == label_id, owned(ProjectLabel, user_id))
    )
    if label is None:
        raise NotFound("label")
    return label


def _clean(title: str, color: str | None) -> tuple[str, str | None]:
    title = title.strip()
    if not 1 <= len(title) <= 60:
        raise Invalid("a label title has 1 to 60 characters")
    try:
        return title, normalize("task", {"color": color}).get("color")
    except FieldError as exc:
        raise Invalid(str(exc)) from exc


class LabelMixin(StoreBase):
    # --- definitions (no project, so no event) --------------------------------------------------

    async def create_label(
        self, user_id: uuid.UUID, *, title: str, color: str | None = None, description: str = ""
    ) -> ProjectLabel:
        title, color = _clean(title, color)
        label = ProjectLabel(
            id=uuid.uuid4(),
            user_id=user_id,
            application="project",
            title=title,
            color=color,
            description=description[:200],
        )
        async with self._sessionmaker() as db:
            db.add(label)
            await flush(db, "a label with this title exists")
            await db.commit()
            return label

    async def list_labels(self, user_id: uuid.UUID) -> list[ProjectLabel]:
        async with self._sessionmaker() as db:
            rows = await db.scalars(
                select(ProjectLabel)
                .where(owned(ProjectLabel, user_id))
                .order_by(ProjectLabel.title)
            )
            return list(rows)

    async def update_label(
        self,
        user_id: uuid.UUID,
        label_id: uuid.UUID,
        *,
        title: str | None = None,
        color: str | None = None,
        description: str | None = None,
    ) -> ProjectLabel:
        async with self._sessionmaker() as db:
            label = await _label(db, user_id, label_id)
            label.title, label.color = _clean(
                title if title is not None else label.title,
                color if color is not None else label.color,
            )
            if description is not None:
                label.description = description[:200]
            await flush(db, "a label with this title exists")
            await db.commit()
            return label

    # --- on nodes (a line in the node's project) ------------------------------------------------

    async def labels_of(self, user_id: uuid.UUID, node_id: uuid.UUID) -> list[ProjectLabel]:
        async with self._sessionmaker() as db:
            await get_node(db, user_id, node_id)
            rows = await db.scalars(
                select(ProjectLabel)
                .join(ProjectNodeLabel, ProjectNodeLabel.label_id == ProjectLabel.id)
                .where(ProjectNodeLabel.node_id == node_id, owned(ProjectLabel, user_id))
                .order_by(ProjectLabel.title)
            )
            return list(rows)

    async def labels_for_nodes(
        self, user_id: uuid.UUID, node_ids: list[uuid.UUID]
    ) -> dict[uuid.UUID, list[ProjectLabel]]:
        out: dict[uuid.UUID, list[ProjectLabel]] = {n: [] for n in node_ids}
        if not node_ids:
            return out
        async with self._sessionmaker() as db:
            rows = await db.execute(
                select(ProjectNodeLabel.node_id, ProjectLabel)
                .join(ProjectLabel, ProjectNodeLabel.label_id == ProjectLabel.id)
                .where(ProjectNodeLabel.node_id.in_(node_ids), owned(ProjectLabel, user_id))
                .order_by(ProjectLabel.title)
            )
            for node_id, label in rows:
                out[node_id].append(label)
        return out

    async def set_node_labels(
        self,
        user_id: uuid.UUID,
        node_id: uuid.UUID,
        *,
        add: list[uuid.UUID] | None = None,
        remove: list[uuid.UUID] | None = None,
    ) -> list[ProjectLabel]:
        """Put labels on a node and take labels off it; the node's labels afterwards."""
        group = uuid.uuid4()
        async with self._sessionmaker() as db:
            node = await get_node(db, user_id, node_id)
            project = await get_project(db, user_id, node.project_id, lock=True)
            have = set(
                await db.scalars(
                    select(ProjectNodeLabel.label_id).where(ProjectNodeLabel.node_id == node_id)
                )
            )
            for label_id in dict.fromkeys(add or []):
                label = await _label(db, user_id, label_id)
                if label_id in have:
                    continue
                if len(have) >= MAX_LABELS_PER_NODE:
                    raise Invalid(f"a node has at most {MAX_LABELS_PER_NODE} labels")
                db.add(ProjectNodeLabel(node_id=node_id, label_id=label_id, user_id=user_id))
                have.add(label_id)
                await self._record(
                    db,
                    user_id,
                    project,
                    "label.added",
                    node=node,
                    group_id=group,
                    payload={"label": label.title},
                )
            for label_id in dict.fromkeys(remove or []):
                label = await _label(db, user_id, label_id)
                if label_id not in have:
                    continue
                await db.execute(
                    delete(ProjectNodeLabel).where(
                        ProjectNodeLabel.node_id == node_id, ProjectNodeLabel.label_id == label_id
                    )
                )
                have.discard(label_id)
                await self._record(
                    db,
                    user_id,
                    project,
                    "label.removed",
                    node=node,
                    group_id=group,
                    payload={"label": label.title},
                )
            await db.commit()
        return await self.labels_of(user_id, node_id)

    async def delete_label(self, user_id: uuid.UUID, label_id: uuid.UUID) -> None:
        """Remove a label from every node that has it (a line in each project), then the label."""
        group = uuid.uuid4()
        async with self._sessionmaker() as db:
            label = await _label(db, user_id, label_id)
            rows = await db.execute(
                select(ProjectNode)
                .join(ProjectNodeLabel, ProjectNodeLabel.node_id == ProjectNode.id)
                .where(ProjectNodeLabel.label_id == label_id, owned(ProjectNode, user_id))
            )
            for node in rows.scalars().all():
                project = await get_project(db, user_id, node.project_id, lock=True)
                await self._record(
                    db,
                    user_id,
                    project,
                    "label.removed",
                    node=node,
                    group_id=group,
                    payload={"label": label.title, "label_deleted": True},
                )
            await db.delete(label)
            await db.commit()
