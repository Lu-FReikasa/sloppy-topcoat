"""Project templates (PPH-04, 04j): the structure of a project, saved to start new ones from.

A template keeps topics and tasks (title, body, priority, colour, estimate, milestone flag, repeat
rule), the labels they use, the open ``blocked_by``/``related``/``precedes`` relations between them,
and the dates and reminders **relative to the earliest date** in the project. It keeps no event
history, no reports (notes, questions, decisions, facts carry what happened), no status, no answers,
no agent requests and no grants. Whatever a body says is copied as written: a person who does not
want a text in the template takes it out of the project first.

Creating a project from a template runs in **one** transaction through the ordinary store methods
(the same engine a change set uses), so every rule and every line of the Akte comes for free and a
failure leaves nothing behind. The new project's Akte starts with ``project.created`` followed by a
``project.from_template`` line that names the template.

Known limit: a date is kept as a number of seconds after the anchor, so a copy that crosses a
daylight-saving change shows the same instants, not the same wall-clock hours.
"""

import uuid
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from topcoat_hub import clock
from topcoat_hub.models import (
    Project,
    ProjectLabel,
    ProjectNode,
    ProjectNodeLabel,
    ProjectRelation,
    ProjectReminder,
    ProjectTemplate,
)
from topcoat_hub.project_base import (
    APPLICATION,
    Invalid,
    NotFound,
    ProjectError,
    get_nodes,
    get_project,
    number_key,
    owned,
)
from topcoat_hub.project_changesets import _Shared, _SharedMaker, run_operations
from topcoat_hub.project_events import PERSON, Actor, EventLog
from topcoat_hub.project_progress import reset_checklist
from topcoat_hub.project_store import ProjectStore

VERSION = 1
MAX_NODES = 500
TEMPLATE_KINDS = ("topic", "task")  # reports are what happened, not structure
RELATION_KINDS = ("blocked_by", "related", "precedes")
DATES = ("start_at", "due_at", "end_at")
# the task fields a template keeps (the dates are kept as offsets)
KEPT_FIELDS = ("priority", "color", "estimate_minutes", "is_milestone", "favorite")


class TemplateError(ProjectError):
    pass


def _rule(rule: dict[str, Any] | None) -> dict[str, Any] | None:
    """A repeat rule without its anchor day: the copy settles it from its own dates."""
    if not rule:
        return None
    return {k: v for k, v in rule.items() if k != "anchor_day"}


def _seconds(value: datetime | None, anchor: datetime) -> int | None:
    return None if value is None else int((value - anchor).total_seconds())


def _anchor(nodes: list[ProjectNode]) -> datetime | None:
    found = [d for n in nodes for d in (n.start_at, n.due_at, n.end_at) if d is not None]
    return min(found) if found else None


async def build_content(
    db: AsyncSession, user_id: uuid.UUID, project_id: uuid.UUID
) -> dict[str, Any]:
    """The template document of one project of the person (a missing or foreign project raises
    :class:`NotFound`). The trashed nodes are left out."""
    project = await get_project(db, user_id, project_id)
    rows = [n for n in await get_nodes(db, user_id, project_id) if n.kind in TEMPLATE_KINDS]
    rows.sort(key=lambda n: number_key(n.number))
    if len(rows) > MAX_NODES:
        raise Invalid(f"a template has at most {MAX_NODES} nodes")
    anchor = _anchor(rows)
    ids = {n.id: f"n{i + 1}" for i, n in enumerate(rows)}
    by_id = {n.id: n for n in rows}
    labels = await _labels_by_node(db, user_id, list(ids))
    reminders = await _reminders_by_node(db, user_id, list(ids))
    used: dict[str, ProjectLabel] = {}
    nodes = []
    for n in rows:
        fields: dict[str, Any] = {}
        if n.kind == "task":
            fields = {
                k: getattr(n, k) for k in KEPT_FIELDS if getattr(n, k) not in (None, False, 0)
            }
            if n.repeat_rule:
                fields["repeat_rule"] = _rule(n.repeat_rule)
        elif n.color:
            fields["color"] = n.color
        entry: dict[str, Any] = {
            "key": ids[n.id],
            "parent": ids.get(n.parent_id) if n.parent_id else None,
            "kind": n.kind,
            "title": n.title,
            "body": reset_checklist(n.body),
            "fields": fields,
            "labels": sorted(label.title for label in labels.get(n.id, [])),
        }
        for label in labels.get(n.id, []):
            used[label.title] = label
        if anchor is not None:
            offsets = {k: _seconds(getattr(n, k), anchor) for k in DATES}
            entry["offsets"] = {k: v for k, v in offsets.items() if v is not None}
        entry["reminders"] = [
            {"relative_to": r.relative_to, "offset_seconds": r.offset_seconds}
            if r.relative_to
            else {"at_offset": _seconds(r.at, anchor) if anchor else None}
            for r in reminders.get(n.id, [])
            if r.relative_to or anchor is not None
        ]
        nodes.append(entry)
    relations = []
    found = await db.scalars(
        select(ProjectRelation).where(
            owned(ProjectRelation, user_id),
            ProjectRelation.kind.in_(RELATION_KINDS),
            ProjectRelation.valid_to.is_(None),
            ProjectRelation.node_id.in_(list(by_id)),
            ProjectRelation.other_id.in_(list(by_id)),
        )
    )
    for r in found:
        relations.append({"node": ids[r.node_id], "other": ids[r.other_id], "kind": r.kind})
    relations.sort(key=lambda r: (r["node"], r["other"], r["kind"]))
    return {
        "version": VERSION,
        "project": {"description": project.description, "color": project.color},
        "has_dates": anchor is not None,
        "labels": [
            {"title": t, "color": label.color, "description": label.description}
            for t, label in sorted(used.items())
        ],
        "nodes": nodes,
        "relations": relations,
    }


async def _labels_by_node(
    db: AsyncSession, user_id: uuid.UUID, node_ids: list[uuid.UUID]
) -> dict[uuid.UUID, list[ProjectLabel]]:
    out: dict[uuid.UUID, list[ProjectLabel]] = {}
    if not node_ids:
        return out
    rows = await db.execute(
        select(ProjectNodeLabel.node_id, ProjectLabel)
        .join(ProjectLabel, ProjectLabel.id == ProjectNodeLabel.label_id)
        .where(ProjectNodeLabel.user_id == user_id, ProjectNodeLabel.node_id.in_(node_ids))
    )
    for node_id, label in rows:
        out.setdefault(node_id, []).append(label)
    return out


async def _reminders_by_node(
    db: AsyncSession, user_id: uuid.UUID, node_ids: list[uuid.UUID]
) -> dict[uuid.UUID, list[ProjectReminder]]:
    out: dict[uuid.UUID, list[ProjectReminder]] = {}
    if not node_ids:
        return out
    rows = await db.scalars(
        select(ProjectReminder)
        .where(owned(ProjectReminder, user_id), ProjectReminder.node_id.in_(node_ids))
        .order_by(ProjectReminder.created_at, ProjectReminder.id)
    )
    for r in rows:
        out.setdefault(r.node_id, []).append(r)
    return out


def operations_of(content: dict[str, Any], start: datetime | None) -> list[dict[str, Any]]:
    """The change-set operations that build the template's structure, dates shifted so that the
    template's anchor falls on ``start`` (no dates and no absolute reminders when ``start`` is
    None)."""
    if content.get("version") != VERSION:
        raise TemplateError(f"unknown template version: {content.get('version')!r}")
    ops: list[dict[str, Any]] = []
    for node in content["nodes"]:
        op: dict[str, Any] = {
            "op": "create_node",
            "local": node["key"],
            "kind": node["kind"],
            "title": node["title"],
            "body": node["body"],
            **node["fields"],
        }
        if node["parent"]:
            op["parent"] = node["parent"]
        if node["labels"]:
            op["labels"] = node["labels"]
        if start is not None:
            for name, seconds in node.get("offsets", {}).items():
                op[name] = clock.iso(start + timedelta(seconds=seconds))
        ops.append(op)
    for node in content["nodes"]:
        for reminder in node["reminders"]:
            if "at_offset" in reminder:
                if start is None or reminder["at_offset"] is None:
                    continue
                at = clock.iso(start + timedelta(seconds=reminder["at_offset"]))
                ops.append({"op": "reminder", "node": node["key"], "at": at})
            else:
                ops.append({"op": "reminder", "node": node["key"], **reminder})
    for r in content["relations"]:
        ops.append({"op": "relate", **r})
    return ops


def view(row: ProjectTemplate, *, full: bool = False) -> dict[str, Any]:
    content = row.content
    out: dict[str, Any] = {
        "id": str(row.id),
        "name": row.name,
        "description": row.description,
        "nodes": len(content["nodes"]),
        "has_dates": bool(content.get("has_dates")),
        "created_at": clock.iso(row.created_at),
    }
    if full:
        out["content"] = content
    return out


class TemplateStore:
    """Templates of one person; every query is scoped by ``user_id`` and the application."""

    def __init__(
        self,
        sessionmaker: async_sessionmaker[AsyncSession],
        actor: Actor = PERSON,
        default_zone: str = "UTC",
    ) -> None:
        self._sessionmaker = sessionmaker
        self.actor = actor
        self.default_zone = default_zone

    async def save(
        self,
        user_id: uuid.UUID,
        project_id: uuid.UUID,
        *,
        name: str,
        description: str = "",
    ) -> ProjectTemplate:
        """Save the structure of a project of the person as a template."""
        name = name.strip()
        if not 1 <= len(name) <= 80:
            raise Invalid("the name of a template is 1 to 80 characters")
        if len(description) > 400:
            raise Invalid("the description of a template is at most 400 characters")
        async with self._sessionmaker() as db:
            row = ProjectTemplate(
                id=uuid.uuid4(),
                user_id=user_id,
                application=APPLICATION,
                name=name,
                description=description,
                content=await build_content(db, user_id, project_id),
            )
            db.add(row)
            try:
                await db.commit()
            except IntegrityError as exc:
                raise Invalid("a template with this name exists") from exc
            await db.refresh(row)
            return row

    async def list_templates(self, user_id: uuid.UUID) -> list[ProjectTemplate]:
        async with self._sessionmaker() as db:
            rows = await db.scalars(
                select(ProjectTemplate)
                .where(owned(ProjectTemplate, user_id))
                .order_by(ProjectTemplate.name)
            )
            return list(rows)

    async def get(self, user_id: uuid.UUID, template_id: uuid.UUID) -> ProjectTemplate:
        async with self._sessionmaker() as db:
            return await _get(db, user_id, template_id)

    async def delete(self, user_id: uuid.UUID, template_id: uuid.UUID) -> None:
        async with self._sessionmaker() as db:
            await db.delete(await _get(db, user_id, template_id))
            await db.commit()

    async def create_project(
        self,
        user_id: uuid.UUID,
        template_id: uuid.UUID,
        *,
        title: str,
        start_at: datetime | None = None,
        identifier: str | None = None,
    ) -> uuid.UUID:
        """A new project from a template, in one transaction; returns the id of the project."""
        _check_start(start_at)
        async with self._sessionmaker() as db:
            row = await _get(db, user_id, template_id)
            project = await self._create_from(
                db, user_id, row.content, title, start_at, identifier,
                {"template_id": str(row.id), "template": row.name},
            )  # fmt: skip
            await db.commit()
            return project.id

    async def duplicate(
        self,
        user_id: uuid.UUID,
        project_id: uuid.UUID,
        *,
        title: str,
        start_at: datetime | None = None,
        identifier: str | None = None,
    ) -> uuid.UUID:
        """A copy of a project's structure under a new title (the same engine as a template, without
        keeping a template); returns the id of the new project. Dates move so that the earliest one
        falls on ``start_at`` when it is given."""
        _check_start(start_at)
        async with self._sessionmaker() as db:
            source = await get_project(db, user_id, project_id)
            content = await build_content(db, user_id, project_id)
            project = await self._create_from(
                db, user_id, content, title, start_at, identifier,
                {"template_id": None, "template": None, "duplicate_of": str(source.id)},
            )  # fmt: skip
            await db.commit()
            return project.id

    async def _create_from(
        self,
        db: AsyncSession,
        user_id: uuid.UUID,
        content: dict[str, Any],
        title: str,
        start_at: datetime | None,
        identifier: str | None,
        origin: dict[str, Any],
    ) -> Project:
        ops = operations_of(content, start_at)
        store = ProjectStore(_SharedMaker(_Shared(db)), self.actor, self.default_zone)  # type: ignore[arg-type]
        project = await store.create_project(
            user_id,
            title=title,
            description=content["project"]["description"],
            color=content["project"]["color"],
            identifier=identifier,
        )
        try:
            await run_operations(
                db, user_id, project.id, ops, set(range(len(ops))), self.actor, self.default_zone
            )
        except ProjectError as exc:
            raise TemplateError(f"the template cannot be applied: {exc}") from exc
        await EventLog.record(
            db,
            user_id,
            project,
            "project.from_template",
            actor=self.actor,
            payload={**origin, "start_at": clock.iso(start_at), "nodes": len(content["nodes"])},
        )
        return project


def _check_start(start_at: datetime | None) -> None:
    if start_at is not None:
        try:
            clock.aware(start_at)
        except ValueError as exc:
            raise Invalid(str(exc)) from exc


async def _get(db: AsyncSession, user_id: uuid.UUID, template_id: uuid.UUID) -> ProjectTemplate:
    row = await db.scalar(
        select(ProjectTemplate).where(
            ProjectTemplate.id == template_id, owned(ProjectTemplate, user_id)
        )
    )
    if row is None:
        raise NotFound("template")
    return row
