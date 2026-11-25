"""The data of the Projekte application (brief PPH-01-planning-app, 01a; rework 2026-10-27).

Every query filters by the person (``user_id``) and by the application (``project``). A row of
another person and a row of another application look exactly like a missing row: the callers get
:class:`NotFound` for both. A write with a stale ``revision`` raises :class:`Conflict` and changes
nothing; the caller reloads and decides again.

A project's tree is numbered: a node's number is its identity (``1``, ``1.2``, ``1.2.1``). It is
given once, from the counter of its parent (or of the project for the top level), and is never
changed or reused. Moving a node changes its parent only. Leaving the working set is a status
(``dropped``); deleting is the trash (``project_trash.py``), by hand only.

Every change is recorded in the project's Akte (``project_events.py``) in the same transaction, and
a change of a node's title or body also writes a version (``project_versions.py``). The store is the
only writer of both: a method that changes data and writes no event would be a silent write path,
so :data:`MUTATORS`, :data:`EVENTLESS` and :data:`READS` list every public method and a test checks
the lists. The parts of the store live in mixins: labels, relations, reminders, the trash.
"""

import uuid
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any

from sqlalchemy import func, select, text, true
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from topcoat_hub import clock, project_repeat, project_scope
from topcoat_hub.models import (
    Project,
    ProjectEvent,
    ProjectLabel,
    ProjectNode,
    ProjectNodeVersion,
)
from topcoat_hub.models import ProjectNodeLabel as ProjectNodeLabelRow
from topcoat_hub.project_base import (
    APPLICATION,
    NODE_KINDS,
    PROJECT_STATUSES,
    REPORT_KINDS,
    TASK_STATUSES,
    UNSET,
    Conflict,
    Invalid,
    NotFound,
    ProjectError,
    as_str,
    attach_blockers,
    bump,
    by_number,
    check_one_of,
    check_revision,
    check_title,
    flush,
    get_node,
    get_nodes,
    get_project,
    number_key,
    owned,
)
from topcoat_hub.project_complete import CompleteMixin
from topcoat_hub.project_events import SYSTEM, Actor, EventLog, verify
from topcoat_hub.project_fields import FieldError, check_dates, iso, normalize
from topcoat_hub.project_filter import compile_filter
from topcoat_hub.project_labels import LabelMixin
from topcoat_hub.project_mandates import MandateMixin
from topcoat_hub.project_messages import MessageMixin
from topcoat_hub.project_progress import progress
from topcoat_hub.project_relations import RelationMixin
from topcoat_hub.project_reminders import ReminderMixin, recompute_reminders
from topcoat_hub.project_sort import child_counts, parse_sort, sort_nodes
from topcoat_hub.project_trash import TrashMixin
from topcoat_hub.project_tree import check_parent
from topcoat_hub.project_versions import VersionLog, display_diff, make_diff
from topcoat_hub.project_view_defs import ViewMixin

__all__ = [
    "APPLICATION",
    "EVENTLESS",
    "MUTATORS",
    "NODE_KINDS",
    "PROJECT_STATUSES",
    "READS",
    "REPORT_KINDS",
    "TASK_STATUSES",
    "Actor",
    "Conflict",
    "Invalid",
    "NotFound",
    "ProjectError",
    "ProjectStore",
    "number_key",
]

# every public method of ProjectStore is one of these (tests/test_project_events.py)
MUTATORS = (
    "create_project",
    "update_project",
    "set_scope",
    "create_node",
    "update_node",
    "move_node",
    "answer_question",
    "supersede_report",
    "set_node_labels",
    "delete_label",
    "add_relation",
    "end_relation",
    "add_reminder",
    "remove_reminder",
    "fire_reminder",
    "complete_node",
    "trash_project",
    "restore_project",
    "purge_project",
    "trash_node",
    "restore_node",
    "purge_node",
    "start_chain",
    "flag_chain",
    "stop_chain",
    "finish_chain",
    "add_message",
    "set_message_state",
    "create_mandate",
    "revoke_mandate",
    "use_mandate",
    "note_mandate",
)
# changes that belong to no project: a label's definition is a person's own, only its use on a node
# is a line in that node's project
EVENTLESS = (
    "create_label",
    "update_label",
    "create_view",
    "update_view",
    "delete_view",
)
READS = (
    "list_projects",
    "get_project",
    "get_node",
    "tree",
    "timeline",
    "verify_chain",
    "node_history",
    "node_at",
    "node_diff",
    "size_report",
    "with_actor",
    "list_labels",
    "get_chain",
    "get_mandate",
    "mandates_of",
    "find_mandate",
    "chains_of",
    "messages_of",
    "message_for_task",
    "labels_of",
    "labels_for_nodes",
    "relations_of",
    "reminders_of",
    "list_trash",
    "list_views",
    "get_view",
    "search_nodes",
    "latest_events",
)


def _check_identifier(value: str | None) -> str | None:
    if value is None:
        return None
    if not (1 <= len(value) <= 10 and value.isalnum() and value.isascii()):
        raise Invalid("an identifier has 1 to 10 letters or digits")
    return value.upper()


def _plain(value: Any) -> Any:
    """A field value as it goes into an event: times as ISO text."""
    return iso(value) if hasattr(value, "isoformat") else value


@dataclass
class SearchResult:
    """A page of a search: rows of (node, project title, progress, labels), how many nodes match
    in all, and where the next page starts (None at the end)."""

    rows: list[tuple[ProjectNode, str, int | None, list[ProjectLabel]]]
    total: int
    next_after: int | None


SEARCH_CAP = 2000  # more matches than this are cut: narrow the filter


class ProjectStore(
    CompleteMixin,
    LabelMixin,
    MandateMixin,
    MessageMixin,
    RelationMixin,
    ReminderMixin,
    TrashMixin,
    ViewMixin,
):
    def __init__(
        self,
        sessionmaker: async_sessionmaker[AsyncSession],
        actor: Actor = SYSTEM,
        default_zone: str = "UTC",
    ) -> None:
        super().__init__(sessionmaker, actor, default_zone)

    def _settle_rule(self, node: ProjectNode) -> None:
        """A repeat rule keeps the zone it was made in and, for months and years, the day of the
        month it is anchored to (project_repeat.py)."""
        rule = node.repeat_rule
        if not rule:
            return
        rule = {**rule}
        rule.setdefault("tz", self.default_zone)
        node.repeat_rule = project_repeat.with_anchor(
            rule, node.due_at or node.end_at or node.start_at, self.default_zone
        )

    # --- projects -------------------------------------------------------------------------------

    async def create_project(
        self,
        user_id: uuid.UUID,
        *,
        title: str,
        description: str = "",
        parent_id: uuid.UUID | None = None,
        favorite: bool = False,
        color: str | None = None,
        identifier: str | None = None,
    ) -> Project:
        check_title(title)
        project = Project(
            id=uuid.uuid4(),
            user_id=user_id,
            application=APPLICATION,
            title=title,
            description=description,
            status="active",
            scope={},
            next_child=0,
            revision=1,
            favorite=favorite,
            identifier=_check_identifier(identifier),
        )
        try:
            project.color = normalize("topic", {"color": color}).get("color")
        except FieldError as exc:
            raise Invalid(str(exc)) from exc
        async with self._sessionmaker() as db:
            if parent_id is not None:
                await get_project(db, user_id, parent_id)
                project.parent_id = parent_id
            db.add(project)
            await flush(db, "a project with this title or identifier exists")
            await self._record(db, user_id, project, "project.created", payload={"title": title})
            await db.commit()
            return project

    async def list_projects(
        self, user_id: uuid.UUID, *, include_trashed: bool = False
    ) -> list[Project]:
        async with self._sessionmaker() as db:
            statement = select(Project).where(owned(Project, user_id))
            if not include_trashed:
                statement = statement.where(Project.trashed_at.is_(None))
            return list(await db.scalars(statement.order_by(Project.title)))

    async def get_project(self, user_id: uuid.UUID, project_id: uuid.UUID) -> Project:
        async with self._sessionmaker() as db:
            return await get_project(db, user_id, project_id)

    async def get_node(self, user_id: uuid.UUID, node_id: uuid.UUID) -> ProjectNode:
        async with self._sessionmaker() as db:
            return await get_node(db, user_id, node_id)

    async def update_project(
        self,
        user_id: uuid.UUID,
        project_id: uuid.UUID,
        *,
        revision: int,
        title: str | None = None,
        description: str | None = None,
        status: str | None = None,
        favorite: bool | None = None,
        color: Any = UNSET,
        identifier: Any = UNSET,
        parent_id: Any = UNSET,
    ) -> Project:
        if status is not None:
            check_one_of(status, PROJECT_STATUSES)
        if title is not None:
            check_title(title)
        async with self._sessionmaker() as db:
            project = await get_project(db, user_id, project_id, lock=True)
            check_revision(project.revision, revision)
            wanted: dict[str, Any] = {}
            for name, value in (
                ("title", title),
                ("description", description),
                ("status", status),
                ("favorite", favorite),
            ):
                if value is not None:
                    wanted[name] = value
            if color is not UNSET:
                try:
                    wanted["color"] = normalize("topic", {"color": color}).get("color")
                except FieldError as exc:
                    raise Invalid(str(exc)) from exc
            if identifier is not UNSET:
                wanted["identifier"] = _check_identifier(identifier)
            if parent_id is not UNSET:
                await self._check_parent_project(db, user_id, project, parent_id)
                wanted["parent_id"] = parent_id
            changed: dict[str, list[Any]] = {}
            for name, new in wanted.items():
                old = getattr(project, name)
                if new != old:
                    as_text = name == "parent_id"
                    changed[name] = [
                        as_str(old) if as_text else old,
                        as_str(new) if as_text else new,
                    ]
                    setattr(project, name, new)
            bump(project)
            await flush(db, "a project with this title or identifier exists")
            archived = "status" in changed and changed["status"][1] == "dropped"
            await self._record(
                db,
                user_id,
                project,
                "project.archived" if archived else "project.updated",
                payload=changed,
            )
            await db.commit()
            return project

    async def _check_parent_project(
        self, db: AsyncSession, user_id: uuid.UUID, project: Project, parent_id: uuid.UUID | None
    ) -> None:
        """A project may sit below another project of the person, never below itself or its own
        descendants."""
        if parent_id is None:
            return
        seen = {project.id}
        current: Project | None = await get_project(db, user_id, parent_id)
        while current is not None:
            if current.id in seen:
                raise Invalid("a project cannot sit below itself")
            seen.add(current.id)
            current = (
                None
                if current.parent_id is None
                else await get_project(db, user_id, current.parent_id)
            )

    async def set_scope(
        self, user_id: uuid.UUID, project_id: uuid.UUID, *, revision: int, scope: Any
    ) -> Project:
        """The person's grants for this project. Called from the person's own page, never from
        the project agent: no tool reaches this method."""
        try:
            stored = project_scope.normalize(scope)
        except project_scope.ScopeError as exc:
            raise Invalid(str(exc)) from exc
        async with self._sessionmaker() as db:
            project = await get_project(db, user_id, project_id, lock=True)
            check_revision(project.revision, revision)
            before = dict(project.scope or {})
            project.scope = stored
            bump(project)
            await self._record(
                db,
                user_id,
                project,
                "project.scope_changed",
                payload={"before": before, "after": stored},
            )
            await db.commit()
            return project

    # --- nodes ----------------------------------------------------------------------------------

    async def tree(
        self, user_id: uuid.UUID, project_id: uuid.UUID, *, include_trashed: bool = False
    ) -> list[ProjectNode]:
        """Every node of the project, in the order of their numbers (the trashed ones only when
        asked for)."""
        async with self._sessionmaker() as db:
            await get_project(db, user_id, project_id)
            return by_number(await get_nodes(db, user_id, project_id, trashed=include_trashed))

    async def create_node(
        self,
        user_id: uuid.UUID,
        project_id: uuid.UUID,
        *,
        kind: str,
        title: str,
        body: str = "",
        parent_id: uuid.UUID | None = None,
        depends_on: Iterable[uuid.UUID] = (),
        report_kind: str | None = None,
        fields: Mapping[str, Any] | None = None,
    ) -> ProjectNode:
        """A topic, a task or a report below ``parent_id`` (or at the top). The number is taken
        from the parent's counter and stays with the node. ``fields`` are the dates, priority and
        the other task fields (see :mod:`project_fields`); ``depends_on`` becomes ``blocked_by``
        relations."""
        check_one_of(kind, NODE_KINDS)
        if kind == "report":
            check_one_of(report_kind or "", REPORT_KINDS)
        elif report_kind is not None:
            raise Invalid("only a report has a report kind")
        checked = self._fields(kind, fields or {})
        blockers = list(depends_on)
        if blockers and kind != "task":
            raise Invalid("only a task can be blocked by another")
        async with self._sessionmaker() as db:
            node = await self._insert_node(
                db,
                user_id,
                project_id,
                kind=kind,
                title=title,
                body=body,
                parent_id=parent_id,
                report_kind=report_kind,
                fields=checked,
            )
            if blockers:
                await self._set_blockers(db, user_id, node, blockers, uuid.uuid4())
            await db.commit()
            await attach_blockers(db, user_id, [node])
            return node

    async def update_node(
        self,
        user_id: uuid.UUID,
        node_id: uuid.UUID,
        *,
        revision: int,
        title: str | None = None,
        body: str | None = None,
        status: str | None = None,
        depends_on: Iterable[uuid.UUID] | None = None,
        fields: Mapping[str, Any] | None = None,
    ) -> ProjectNode:
        async with self._sessionmaker() as db:
            node = await get_node(db, user_id, node_id)
            project = await get_project(db, user_id, node.project_id, lock=True)
            check_revision(node.revision, revision)
            checked = self._fields(node.kind, fields or {})
            old_title, old_body, old_status = node.title, node.body, node.status
            old_fields = {name: getattr(node, name) for name in checked}
            old_dates = (node.start_at, node.due_at, node.end_at)
            if status is not None:
                if node.kind != "task":
                    raise Invalid("only a task has a status")
                check_one_of(status, TASK_STATUSES)
                node.status = status
                if status == "done" and old_status != "done":
                    node.done_at = clock.hub_now()
                elif status != "done":
                    node.done_at = None
            if title is not None:
                check_title(title)
                node.title = title
            if body is not None:
                node.body = body
            for name, value in checked.items():
                setattr(node, name, value)
            if node.kind == "task":
                try:
                    check_dates(
                        node.start_at, node.due_at, node.end_at, milestone=node.is_milestone
                    )
                except FieldError as exc:
                    raise Invalid(str(exc)) from exc
            if node.kind == "task":
                self._settle_rule(node)
            bump(node)
            group = uuid.uuid4()
            events: list[ProjectEvent] = []
            if node.status != old_status:
                kind = "node.status_changed"
                if node.status == "done":
                    kind = "node.completed"
                elif old_status == "done":
                    kind = "node.reopened"
                events.append(
                    await self._record(
                        db,
                        user_id,
                        project,
                        kind,
                        node=node,
                        group_id=group,
                        payload={"status": [old_status, node.status]},
                    )
                )
            if node.title != old_title:
                events.append(
                    await self._record(
                        db,
                        user_id,
                        project,
                        "node.updated",
                        node=node,
                        group_id=group,
                        payload={"title": [old_title, node.title]},
                    )
                )
            changed = {
                name: [_plain(old_fields[name]), _plain(value)]
                for name, value in checked.items()
                if old_fields[name] != value
            }
            if changed:
                events.append(
                    await self._record(
                        db,
                        user_id,
                        project,
                        "node.updated",
                        node=node,
                        group_id=group,
                        payload=changed,
                    )
                )
            if (node.start_at, node.due_at, node.end_at) != old_dates:
                await recompute_reminders(db, node)
            relations_changed = False
            if depends_on is not None:
                wanted = list(depends_on)
                if wanted and node.kind != "task":
                    raise Invalid("only a task can be blocked by another")
                relations_changed = await self._set_blockers(db, user_id, node, wanted, group)
            body_event = None
            if node.body != old_body:
                body_event = await self._record(
                    db,
                    user_id,
                    project,
                    "body.changed",
                    node=node,
                    group_id=group,
                    payload={
                        "chars": [len(old_body), len(node.body)],
                        "diff": make_diff(old_body, node.body),
                    },
                )
                events.append(body_event)
            if not events and not relations_changed:
                # a call that changed nothing is still a revision, so it is a line
                events.append(
                    await self._record(db, user_id, project, "node.updated", node=node, payload={})
                )
            text_event = body_event or next(
                (e for e in events if e.payload and "title" in e.payload), None
            )
            if text_event is not None:
                await VersionLog.record(
                    db, node, event_id=text_event.id, old_title=old_title, old_body=old_body
                )
            await db.commit()
            await attach_blockers(db, user_id, [node])
            return node

    async def move_node(
        self,
        user_id: uuid.UUID,
        node_id: uuid.UUID,
        *,
        revision: int,
        parent_id: Any = UNSET,
    ) -> ProjectNode:
        """Put a node below another node of the same project, or at the top (``None``). The
        number does not change: it is the node's identity, the tree only shows where it sits."""
        async with self._sessionmaker() as db:
            node = await get_node(db, user_id, node_id)
            project = await get_project(db, user_id, node.project_id, lock=True)
            check_revision(node.revision, revision)
            if parent_id is UNSET:
                raise Invalid("parent_id is required")
            if parent_id is not None:
                parent = await get_node(db, user_id, parent_id)
                if parent.project_id != node.project_id:
                    raise Invalid("parent is not in this project")
                if parent.kind == "report":
                    raise Invalid("a report cannot have children")
            rows = await get_nodes(db, user_id, node.project_id, trashed=True)
            numbers = {n.id: n.number for n in rows}
            parents = {str(n.id): as_str(n.parent_id) for n in rows}
            parents[str(node.id)] = as_str(parent_id)
            try:
                check_parent(str(node.id), parents[str(node.id)], parents)
            except ValueError as exc:
                raise Invalid(str(exc)) from exc
            before = node.parent_id
            node.parent_id = parent_id
            bump(node)
            await self._record(
                db,
                user_id,
                project,
                "node.moved",
                node=node,
                payload={"parent": [numbers.get(before), numbers.get(parent_id)]},
            )
            await db.commit()
            return node

    async def answer_question(
        self, user_id: uuid.UUID, node_id: uuid.UUID, answer: str
    ) -> ProjectNode:
        """The person answers a question (a report of kind ``question``)."""
        async with self._sessionmaker() as db:
            node = await get_node(db, user_id, node_id)
            if node.kind != "report" or node.report_kind != "question":
                raise Invalid("only a question can be answered")
            project = await get_project(db, user_id, node.project_id, lock=True)
            node.answer = answer
            node.answered_at = clock.hub_now()
            bump(node)
            await self._record(
                db, user_id, project, "question.answered", node=node, payload={"answer": answer}
            )
            await db.commit()
            return node

    async def supersede_report(
        self, user_id: uuid.UUID, old_id: uuid.UUID, *, by_id: uuid.UUID
    ) -> ProjectNode:
        """A newer report replaces an older one of the same project: the old one stays, marked as no
        longer current, and the new one says what it replaces. Facts are superseded, never
        overwritten."""
        async with self._sessionmaker() as db:
            old = await get_node(db, user_id, old_id)
            new = await get_node(db, user_id, by_id)
            if old.kind != "report" or new.kind != "report" or old.project_id != new.project_id:
                raise Invalid("a report is replaced by a report of the same project")
            if old.id == new.id or not old.current or new.supersedes_id is not None:
                raise Invalid("this report cannot be replaced by that one")
            project = await get_project(db, user_id, old.project_id, lock=True)
            old.current = False
            new.supersedes_id = old.id
            bump(old)
            bump(new)
            await self._record(
                db,
                user_id,
                project,
                "report.superseded",
                node=new,
                payload={"replaces": f"{project.title} {old.number}"},
            )
            await db.commit()
            return new

    async def search_nodes(
        self,
        user_id: uuid.UUID,
        *,
        filter_text: str = "",
        sort: str = "",
        project_id: uuid.UUID | None = None,
        limit: int = 25,
        after: int = 0,
        zone: str | None = None,
    ) -> SearchResult:
        """The person's nodes that match a filter (see :mod:`project_filter`), sorted by a rule (see
        :mod:`project_sort`), one page. The filter is ANDed with the scoping by person and
        application, and nodes of the trash are left out unless the filter asks for ``trashed``."""
        keys = parse_sort(sort)
        condition, wants_trash = (
            compile_filter(filter_text, zone=zone or self.default_zone)
            if filter_text.strip()
            else (true(), False)
        )
        async with self._sessionmaker() as db:
            statement = (
                select(ProjectNode, Project.title)
                .join(Project, Project.id == ProjectNode.project_id)
                .where(owned(ProjectNode, user_id), owned(Project, user_id), condition)
            )
            if project_id is not None:
                await get_project(db, user_id, project_id)
                statement = statement.where(ProjectNode.project_id == project_id)
            if not wants_trash:
                statement = statement.where(
                    ProjectNode.trashed_at.is_(None), Project.trashed_at.is_(None)
                )
            found = (await db.execute(statement.limit(SEARCH_CAP + 1))).all()
            total = len(found)
            found = found[:SEARCH_CAP]
            titles = {n.id: t for n, t in found}
            nodes = [n for n, _ in found]
            await attach_blockers(db, user_id, nodes)
            # progress and the number of children need the whole tree of each project involved
            everything: list[ProjectNode] = []
            for pid in {n.project_id for n in nodes}:
                everything.extend(await get_nodes(db, user_id, pid))
            done = progress(everything)
            ordered = sort_nodes(nodes, keys, counts=child_counts(everything))
            page = ordered[max(0, after) : max(0, after) + limit]
            rows = await db.execute(
                select(ProjectNodeLabelRow.node_id, ProjectLabel)
                .join(ProjectLabel, ProjectNodeLabelRow.label_id == ProjectLabel.id)
                .where(ProjectNodeLabelRow.node_id.in_([n.id for n in page] or [uuid.uuid4()]))
                .order_by(ProjectLabel.title)
            )
            labels: dict[uuid.UUID, list[ProjectLabel]] = {n.id: [] for n in page}
            for node_id, label in rows:
                labels[node_id].append(label)
        nxt = after + limit if after + limit < len(ordered) else None
        return SearchResult(
            rows=[(n, titles[n.id], done.get(n.id), labels[n.id]) for n in page],
            total=min(total, len(ordered)),
            next_after=nxt,
        )

    async def latest_events(
        self, user_id: uuid.UUID, project_id: uuid.UUID, n: int = 10
    ) -> list[ProjectEvent]:
        """The last ``n`` lines of the project's Akte, oldest of them first."""
        async with self._sessionmaker() as db:
            await get_project(db, user_id, project_id)
            rows = await db.scalars(
                select(ProjectEvent)
                .where(owned(ProjectEvent, user_id), ProjectEvent.project_id == project_id)
                .order_by(ProjectEvent.seq.desc())
                .limit(n)
            )
            return list(reversed(list(rows)))

    # --- the Akte -------------------------------------------------------------------------------

    async def timeline(
        self,
        user_id: uuid.UUID,
        project_id: uuid.UUID,
        *,
        after: int = 0,
        kinds: list[str] | None = None,
        node_id: uuid.UUID | None = None,
        limit: int = 50,
    ) -> list[ProjectEvent]:
        async with self._sessionmaker() as db:
            await get_project(db, user_id, project_id)
            return await EventLog.timeline(
                db, user_id, project_id, after=after, kinds=kinds, node_id=node_id, limit=limit
            )

    async def verify_chain(self, user_id: uuid.UUID, project_id: uuid.UUID) -> list[str]:
        """What is wrong with the project's chain of events; empty when it holds."""
        async with self._sessionmaker() as db:
            await get_project(db, user_id, project_id)
            return verify(await EventLog.chain(db, user_id, project_id))

    async def node_history(
        self, user_id: uuid.UUID, node_id: uuid.UUID
    ) -> list[tuple[ProjectNodeVersion, Any]]:
        """The versions of a node's title and body, oldest first, with the time of each."""
        async with self._sessionmaker() as db:
            await get_node(db, user_id, node_id)
            versions = await VersionLog.versions(db, user_id, node_id)
            ids = [v.event_id for v in versions if v.event_id]
            at = {}
            if ids:
                rows = await db.execute(
                    select(ProjectEvent.id, ProjectEvent.at).where(ProjectEvent.id.in_(ids))
                )
                at = {row.id: row.at for row in rows}
            return [(v, at.get(v.event_id)) for v in versions]

    async def node_at(self, user_id: uuid.UUID, node_id: uuid.UUID, rev: int) -> dict[str, Any]:
        """Title and body as they were at revision ``rev``."""
        async with self._sessionmaker() as db:
            await get_node(db, user_id, node_id)
            state = VersionLog.state_at(await VersionLog.versions(db, user_id, node_id), rev)
        if state is None:
            raise NotFound("version")
        return state

    async def node_diff(
        self, user_id: uuid.UUID, node_id: uuid.UUID, from_rev: int, to_rev: int
    ) -> dict[str, Any]:
        """What changed in a node's title and body between two revisions."""
        old = await self.node_at(user_id, node_id, from_rev)
        new = await self.node_at(user_id, node_id, to_rev)
        return {
            "from": old["rev"],
            "to": new["rev"],
            "title": [old["title"], new["title"]],
            "body": display_diff(old["body"], new["body"]),
        }

    async def size_report(self) -> dict[str, Any]:
        """How big the Akte is (all people together): rows and bytes of the two tables. The hub
        compares it with a soft limit; nothing is ever deleted because of it."""
        async with self._sessionmaker() as db:
            events = await db.scalar(select(func.count()).select_from(ProjectEvent)) or 0
            versions = await db.scalar(select(func.count()).select_from(ProjectNodeVersion)) or 0
            sizes = {}
            for table in ("project_events", "project_node_versions"):
                sizes[table] = (
                    await db.scalar(
                        text("SELECT pg_total_relation_size(CAST(:t AS regclass))"), {"t": table}
                    )
                    or 0
                )
        return {
            "events": events,
            "versions": versions,
            "events_bytes": sizes["project_events"],
            "versions_bytes": sizes["project_node_versions"],
        }

    # --- internals ------------------------------------------------------------------------------

    @staticmethod
    def _fields(kind: str, fields: Mapping[str, Any]) -> dict[str, Any]:
        try:
            return normalize(kind, fields)
        except FieldError as exc:
            raise Invalid(str(exc)) from exc

    async def _insert_node(
        self,
        db: AsyncSession,
        user_id: uuid.UUID,
        project_id: uuid.UUID,
        *,
        kind: str,
        title: str,
        body: str,
        parent_id: uuid.UUID | None,
        report_kind: str | None,
        fields: dict[str, Any],
        event_kind: str | None = None,
        event_payload: dict[str, Any] | None = None,
        actor: Actor | None = None,
    ) -> ProjectNode:
        """Add a node with the next number of its parent, inside the caller's transaction. The
        parent and the project are locked, so two writers cannot take the same number. The node's
        first version and its event are written with it; a message names its own kind of event
        (``event_kind``, ``event_payload``) and the actor of the chain it belongs to."""
        check_title(title)
        project = await get_project(db, user_id, project_id, lock=True)
        parent = None
        if parent_id is not None:
            parent = await get_node(db, user_id, parent_id, lock=True)
            if parent.project_id != project.id:
                raise Invalid("parent is not in this project")
            if parent.kind == "report":
                raise Invalid("a report cannot have children")
        rows = await get_nodes(db, user_id, project_id, trashed=True)
        node_id = uuid.uuid4()
        parents = {str(n.id): as_str(n.parent_id) for n in rows}
        parents[str(node_id)] = as_str(parent_id)
        try:
            check_parent(str(node_id), parents[str(node_id)], parents)
        except ValueError as exc:
            raise Invalid(str(exc)) from exc
        if parent is None:
            project.next_child += 1
            number = str(project.next_child)
        else:
            parent.next_child += 1
            number = f"{parent.number}.{parent.next_child}"
        node = ProjectNode(
            id=node_id,
            project_id=project_id,
            user_id=user_id,
            application=APPLICATION,
            number=number,
            parent_id=parent_id,
            kind=kind,
            report_kind=report_kind,
            title=title,
            body=body,
            status="todo" if kind == "task" else None,
            next_child=0,
            revision=1,
            **fields,
        )
        if kind == "task":
            try:
                check_dates(
                    node.start_at, node.due_at, node.end_at, milestone=bool(node.is_milestone)
                )
            except FieldError as exc:
                raise Invalid(str(exc)) from exc
        self._settle_rule(node)
        db.add(node)
        await db.flush()
        payload = event_payload or {
            "kind": kind,
            "report_kind": report_kind,
            "parent": None if parent is None else parent.number,
            "fields": {k: _plain(v) for k, v in fields.items()},
        }
        recorder = self._record if actor is None else self.with_actor(actor)._record
        event = await recorder(
            db,
            user_id,
            project,
            event_kind or ("report.added" if kind == "report" else "node.created"),
            node=node,
            payload=payload,
        )
        await VersionLog.record(db, node, event_id=event.id, snapshot=True)
        return node
