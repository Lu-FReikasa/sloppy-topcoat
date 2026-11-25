"""Saved views of the project application (PPH-04, 04c): a filter, a sort and a layout under a
title.

A view is a person's own, like a label: defining or changing one is not a change to any project, so
it writes no line in an Akte. A view with a ``project_id`` belongs to that project; one without
works across all the person's projects. Nothing is stored for the order of the nodes: a view holds a
rule (``sort``), and the nodes are sorted when they are read.

The seeded views (``BUILTIN``) are created for a person the first time they ask for their views,
with a stable ``builtin`` key that the web translates; they can be changed but not deleted.
"""

import json
import uuid
from typing import Any

from sqlalchemy import select

from topcoat_hub.models import ProjectView
from topcoat_hub.project_base import (
    Invalid,
    NotFound,
    StoreBase,
    check_revision,
    flush,
    get_project,
    owned,
)
from topcoat_hub.project_filter import FilterError, compile_filter
from topcoat_hub.project_sort import SortError, parse_sort

VIEW_TYPES = ("list", "table", "board", "calendar", "gantt")
MAX_VIEWS = 100
MAX_CONFIG_BYTES = 8 * 1024

# key -> (title, type, filter, sort): the views everybody starts with
BUILTIN: dict[str, tuple[str, str, str, str]] = {
    "today": ("Today", "list", "done = false && dueDate < now/d+1d", "dueDate,priority:desc"),
    "overdue": ("Overdue", "list", "done = false && dueDate < now", "dueDate"),
    "next7": (
        "Next 7 days", "list",
        "done = false && dueDate >= now/d && dueDate < now/d+7d", "dueDate,priority:desc",
    ),
    "ready": ("Ready", "list", "ready = true", "priority:desc,dueDate"),
    "blocked": ("Blocked", "list", "blocked = true && done = false", "dueDate"),
    "in_review": ("In review", "list", "status = in_review", "updated:desc"),
    "no_date": (
        "No date", "list", "kind = task && done = false && dueDate = null", "priority:desc",
    ),
    "unanswered": ("Waiting for an answer", "list", "unanswered = true", "created"),
}  # fmt: skip


def check_view(filter_text: str, sort: str, config: Any) -> None:
    """Refuse a view whose filter, sort or layout would not work."""
    try:
        if filter_text.strip():
            compile_filter(filter_text)
        parse_sort(sort)
    except (FilterError, SortError) as exc:
        raise Invalid(str(exc)) from exc
    if not isinstance(config, dict):
        raise Invalid("a view's layout is an object")
    if len(json.dumps(config)) > MAX_CONFIG_BYTES:
        raise Invalid(f"a view's layout is larger than {MAX_CONFIG_BYTES // 1024} KiB")


class ViewMixin(StoreBase):
    async def list_views(
        self, user_id: uuid.UUID, project_id: uuid.UUID | None = None
    ) -> list[ProjectView]:
        """The person's views across projects and, when a project is named, that project's views
        too. The seeded views are created on the first call."""
        async with self._sessionmaker() as db:
            await self._seed(db, user_id)
            statement = select(ProjectView).where(owned(ProjectView, user_id))
            if project_id is None:
                statement = statement.where(ProjectView.project_id.is_(None))
            else:
                await get_project(db, user_id, project_id)
                statement = statement.where(
                    (ProjectView.project_id.is_(None)) | (ProjectView.project_id == project_id)
                )
            rows = await db.scalars(
                statement.order_by(ProjectView.position, ProjectView.title, ProjectView.id)
            )
            return list(rows)

    async def get_view(self, user_id: uuid.UUID, view_id: uuid.UUID) -> ProjectView:
        async with self._sessionmaker() as db:
            return await _view(db, user_id, view_id)

    async def create_view(
        self,
        user_id: uuid.UUID,
        *,
        title: str,
        type: str = "list",
        filter: str = "",
        sort: str = "",
        config: dict[str, Any] | None = None,
        project_id: uuid.UUID | None = None,
        favorite: bool = False,
    ) -> ProjectView:
        title = title.strip()
        if not 1 <= len(title) <= 80:
            raise Invalid("a view has a title of 1 to 80 characters")
        if type not in VIEW_TYPES:
            raise Invalid(f"type must be one of {', '.join(VIEW_TYPES)}")
        check_view(filter, sort, config or {})
        async with self._sessionmaker() as db:
            if project_id is not None:
                await get_project(db, user_id, project_id)
            count = len(
                list(await db.scalars(select(ProjectView.id).where(owned(ProjectView, user_id))))
            )
            if count >= MAX_VIEWS:
                raise Invalid(f"a person has at most {MAX_VIEWS} views")
            view = ProjectView(
                id=uuid.uuid4(), user_id=user_id, application="project", project_id=project_id,
                title=title, type=type, filter=filter.strip(), sort=sort.strip(),
                config=config or {}, favorite=favorite, position=count + 100, revision=1,
            )  # fmt: skip
            db.add(view)
            await flush(db, "this view cannot be saved")
            await db.commit()
            return view

    async def update_view(
        self, user_id: uuid.UUID, view_id: uuid.UUID, *, revision: int, **changes: Any
    ) -> ProjectView:
        allowed = {"title", "type", "filter", "sort", "config", "favorite", "position"}
        if set(changes) - allowed:
            raise Invalid(f"a view has no field {sorted(set(changes) - allowed)[0]}")
        async with self._sessionmaker() as db:
            view = await _view(db, user_id, view_id)
            check_revision(view.revision, revision)
            merged = {
                "title": view.title, "type": view.type, "filter": view.filter, "sort": view.sort,
                "config": view.config, **changes,
            }  # fmt: skip
            title = str(merged["title"]).strip()
            if not 1 <= len(title) <= 80:
                raise Invalid("a view has a title of 1 to 80 characters")
            if merged["type"] not in VIEW_TYPES:
                raise Invalid(f"type must be one of {', '.join(VIEW_TYPES)}")
            check_view(str(merged["filter"]), str(merged["sort"]), merged["config"])
            for name, value in changes.items():
                setattr(view, name, value.strip() if isinstance(value, str) else value)
            view.title = title
            view.revision += 1
            await flush(db, "this view cannot be saved")
            await db.commit()
            return view

    async def delete_view(self, user_id: uuid.UUID, view_id: uuid.UUID) -> None:
        async with self._sessionmaker() as db:
            view = await _view(db, user_id, view_id)
            if view.builtin:
                raise Invalid("a seeded view can be changed but not deleted")
            await db.delete(view)
            await db.commit()

    async def _seed(self, db: Any, user_id: uuid.UUID) -> None:
        have = set(
            await db.scalars(
                select(ProjectView.builtin).where(
                    owned(ProjectView, user_id), ProjectView.builtin.is_not(None)
                )
            )
        )
        for position, (key, (title, type_, filter_text, sort)) in enumerate(BUILTIN.items()):
            if key in have:
                continue
            db.add(
                ProjectView(
                    id=uuid.uuid4(),
                    user_id=user_id,
                    application="project",
                    project_id=None,
                    title=title,
                    type=type_,
                    filter=filter_text,
                    sort=sort,
                    config={},
                    favorite=False,
                    position=position,
                    builtin=key,
                    revision=1,
                )
            )
        await db.commit()


async def _view(db: Any, user_id: uuid.UUID, view_id: uuid.UUID) -> ProjectView:
    view = await db.scalar(
        select(ProjectView).where(ProjectView.id == view_id, owned(ProjectView, user_id))
    )
    if view is None:
        raise NotFound("view")
    return view
