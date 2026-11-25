"""The project agent's single-step writing tools (PPH-04, 04g).

Next to the change set (``project_changesets.py``, for anything with more than a couple of
changes) the agent has these tools for one change at a time. In a chat each of them is only
*prepared* and the person confirms it with a click (D15); a run started from the page runs them
directly, and every call is an event in the Akte.

None of them deletes. Taking a label off a node, ending a relation and cancelling a reminder are
named ``detach``, ``end`` and ``cancel`` on purpose: the first two keep their history, and a test
keeps the tool list free of anything called delete, remove, purge or restore. A project's scope is
not here either; that is the person's grant.
"""

import uuid
from datetime import timedelta
from typing import Any
from zoneinfo import ZoneInfo

from topcoat_hub import clock
from topcoat_hub.project_base import RELATION_KINDS, Conflict, Invalid, NotFound
from topcoat_hub.project_store import ProjectStore
from topcoat_hub.project_templates import TemplateStore
from topcoat_hub.project_view_defs import VIEW_TYPES
from topcoat_hub.project_views import label_view, node_view, project_view, reminder_view

_UUID = {
    "type": "string",
    "pattern": "^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$",
}
_REVISION = {"type": "integer", "minimum": 1}
_TIME = {"type": ["string", "null"], "maxLength": 40}
_TITLE = {"type": "string", "minLength": 1, "maxLength": 200}


def _schema(properties: dict[str, Any], required: list[str]) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": properties,
        "required": required,
        "additionalProperties": False,
    }


WRITE_SPECS: dict[str, dict[str, Any]] = {
    "project.create": {
        "description": "Create a project. Search first: a project with this title may exist. "
        "parent_id puts it below another project; identifier is an optional short code (at most "
        "10 characters).",
        "schema": _schema(
            {
                "title": _TITLE,
                "description": {"type": "string", "maxLength": 4000},
                "parent_id": _UUID,
                "identifier": {"type": "string", "maxLength": 10},
                "color": {"type": ["string", "null"], "maxLength": 20},
            },
            ["title"],
        ),
    },
    "project.duplicate": {
        "description": "Copy the structure of a project (topics, tasks, labels, open relations, "
        "dates and reminders) into a new project with its own title. It copies no reports, status, "
        "answers or history. start_at moves the dates so that the earliest one falls on it.",
        "schema": _schema(
            {
                "project_id": _UUID,
                "title": _TITLE,
                "start_at": _TIME,
                "identifier": {"type": "string", "maxLength": 10},
            },
            ["project_id", "title"],
        ),
    },
    "project.move": {
        "description": "Move a project below another project, or to the top when parent_id is "
        "null. Needs the revision you read.",
        "schema": _schema(
            {
                "project_id": _UUID,
                "revision": _REVISION,
                "parent_id": {"anyOf": [_UUID, {"type": "null"}]},
            },
            ["project_id", "revision", "parent_id"],
        ),
    },
    "project.node.complete": {
        "description": "Mark a task done. A task with a repeat rule is reopened for its next "
        "occurrence and the finished one stays in the history. Tasks the person owns are theirs "
        "to close: set 'in_review' with project.node.update unless the person asked you to "
        "close it. Only for a request that says the task is done or to be closed (done, finished, "
        "close, erledigt, abschliessen); never as an addition to another change of the same task, "
        "and never because a status was set. Needs the revision you read.",
        "schema": _schema({"node_id": _UUID, "revision": _REVISION}, ["node_id", "revision"]),
    },
    "project.node.claim": {
        "description": "Say that you start working on a task: its status goes from 'todo' to "
        "'in_progress'. Refused for any other status. Needs the revision you read.",
        "schema": _schema({"node_id": _UUID, "revision": _REVISION}, ["node_id", "revision"]),
    },
    "project.node.reschedule": {
        "description": "Move a task in time. Either give the new start_at, due_at and end_at "
        "(ISO 8601 with an offset, null clears one), or shift_days to move every date the task "
        "has by whole days (negative moves back; the clock time stays the same). Needs the "
        "revision you read. Never invent a deadline: ask in a question report instead.",
        "schema": _schema(
            {
                "node_id": _UUID,
                "revision": _REVISION,
                "start_at": _TIME,
                "due_at": _TIME,
                "end_at": _TIME,
                "shift_days": {"type": "integer", "minimum": -3660, "maximum": 3660},
            },
            ["node_id", "revision"],
        ),
    },
    "project.relation.add": {
        "description": "Relate two nodes, also across projects: " + ", ".join(RELATION_KINDS) + ". "
        "'node_id blocked_by other_id' means node_id waits for other_id. A loop of blockers is "
        "refused.",
        "schema": _schema(
            {"node_id": _UUID, "other_id": _UUID, "kind": {"enum": list(RELATION_KINDS)}},
            ["node_id", "other_id", "kind"],
        ),
    },
    "project.relation.end": {
        "description": "End a relation (the id comes from project.node.get). It stays in the "
        "history with the time it stopped to hold.",
        "schema": _schema({"relation_id": _UUID}, ["relation_id"]),
    },
    "project.label.add": {
        "description": "Put a label on a node. The label is created when the person has none "
        "with this title yet; color only matters then.",
        "schema": _schema(
            {
                "node_id": _UUID,
                "label": {"type": "string", "minLength": 1, "maxLength": 40},
                "color": {"type": ["string", "null"], "maxLength": 20},
            },
            ["node_id", "label"],
        ),
    },
    "project.label.detach": {
        "description": "Take a label off a node. The label itself stays.",
        "schema": _schema(
            {"node_id": _UUID, "label": {"type": "string", "minLength": 1, "maxLength": 40}},
            ["node_id", "label"],
        ),
    },
    "project.reminder.add": {
        "description": "Remind the person about a task: either at a fixed time (at, ISO 8601 "
        "with an offset) or relative to one of its dates (relative_to due, start or end, with "
        "offset_seconds, negative = before). The reminder arrives in the inbox.",
        "schema": _schema(
            {
                "node_id": _UUID,
                "at": {"type": "string", "maxLength": 40},
                "relative_to": {"enum": ["due", "start", "end"]},
                "offset_seconds": {"type": "integer"},
            },
            ["node_id"],
        ),
    },
    "project.reminder.cancel": {
        "description": "Cancel a reminder (the id comes from project.node.get). The Akte keeps a "
        "line that it was cancelled.",
        "schema": _schema({"reminder_id": _UUID}, ["reminder_id"]),
    },
    "project.view.save": {
        "description": "Save a view of the person: a filter and a sort rule shown as a list, "
        "table, board, calendar or gantt (project.filter.help describes the syntax). project_id "
        "limits it to one project.",
        "schema": _schema(
            {
                "title": {"type": "string", "minLength": 1, "maxLength": 80},
                "type": {"enum": list(VIEW_TYPES)},
                "filter": {"type": "string", "maxLength": 1000},
                "sort": {"type": "string", "maxLength": 200},
                "project_id": _UUID,
            },
            ["title"],
        ),
    },
}
WRITE_NAMES = frozenset(WRITE_SPECS)

_DATES = ("start_at", "due_at", "end_at")


async def run_write(
    store: ProjectStore, user_id: uuid.UUID, capability: str, args: dict[str, Any], *, zone: str
) -> dict[str, Any]:
    """Run one of the writing tools. The caller has validated ``args`` against the schema."""
    if capability == "project.create":
        project = await store.create_project(
            user_id,
            title=args["title"],
            description=args.get("description", ""),
            parent_id=_uuid(args.get("parent_id")),
            identifier=args.get("identifier"),
            color=args.get("color"),
        )
        return project_view(project)
    if capability == "project.duplicate":
        templates = TemplateStore(store._sessionmaker, store.actor, store.default_zone)
        new_id = await templates.duplicate(
            user_id,
            uuid.UUID(args["project_id"]),
            title=args["title"],
            start_at=_time(args.get("start_at")),
            identifier=args.get("identifier"),
        )
        return project_view(await store.get_project(user_id, new_id))
    if capability == "project.move":
        project = await store.update_project(
            user_id,
            uuid.UUID(args["project_id"]),
            revision=args["revision"],
            parent_id=_uuid(args["parent_id"]),
        )
        return project_view(project)
    if capability == "project.node.complete":
        node = await store.complete_node(
            user_id, uuid.UUID(args["node_id"]), revision=args["revision"]
        )
        return await _node(store, user_id, node)
    if capability == "project.node.claim":
        node = await store.get_node(user_id, uuid.UUID(args["node_id"]))
        if node.kind != "task" or node.status != "todo":
            raise Invalid("only a task that is 'todo' can be claimed")
        node = await store.update_node(
            user_id, node.id, revision=args["revision"], status="in_progress"
        )
        return await _node(store, user_id, node)
    if capability == "project.node.reschedule":
        return await _reschedule(store, user_id, args, zone)
    if capability == "project.relation.add":
        relation = await store.add_relation(
            user_id, uuid.UUID(args["node_id"]), uuid.UUID(args["other_id"]), args["kind"]
        )
        return {"id": str(relation.id), "kind": relation.kind}
    if capability == "project.relation.end":
        relation = await store.end_relation(user_id, uuid.UUID(args["relation_id"]))
        return {"id": str(relation.id), "ended": True}
    if capability == "project.label.add":
        node_id = uuid.UUID(args["node_id"])
        label = await _label(store, user_id, args["label"], create=True, color=args.get("color"))
        labels = await store.set_node_labels(user_id, node_id, add=[label.id])
        return {"labels": [label_view(item) for item in labels]}
    if capability == "project.label.detach":
        node_id = uuid.UUID(args["node_id"])
        label = await _label(store, user_id, args["label"], create=False)
        labels = await store.set_node_labels(user_id, node_id, remove=[label.id])
        return {"labels": [label_view(item) for item in labels]}
    if capability == "project.reminder.add":
        reminder = await store.add_reminder(
            user_id,
            uuid.UUID(args["node_id"]),
            at=_time(args.get("at")),
            relative_to=args.get("relative_to"),
            offset_seconds=args.get("offset_seconds"),
        )
        return reminder_view(reminder)
    if capability == "project.reminder.cancel":
        await store.remove_reminder(user_id, uuid.UUID(args["reminder_id"]))
        return {"cancelled": True}
    # project.view.save
    view = await store.create_view(
        user_id,
        title=args["title"],
        type=args.get("type", "list"),
        filter=args.get("filter", ""),
        sort=args.get("sort", ""),
        project_id=_uuid(args.get("project_id")),
    )
    return {"id": str(view.id), "title": view.title, "type": view.type, "revision": view.revision}


async def preview(
    store: ProjectStore, user_id: uuid.UUID, capability: str, args: dict[str, Any]
) -> dict[str, Any]:
    """What a prepared call would do, as data for the card: its target and the fields it sets.
    Checks as far as it can without writing that the things named exist."""
    fields = {
        k: v
        for k, v in args.items()
        if k not in ("project_id", "node_id", "revision", "other_id", "relation_id", "reminder_id")
    }
    if "node_id" in args:
        node = await store.get_node(user_id, uuid.UUID(args["node_id"]))
        if "revision" in args and node.revision != args["revision"]:
            raise Conflict("revision")
        project = await store.get_project(user_id, node.project_id)
        if "other_id" in args:
            other = await store.get_node(user_id, uuid.UUID(args["other_id"]))
            other_project = await store.get_project(user_id, other.project_id)
            fields["other"] = node_view(other, other_project.title)["ref"]
        return {"target": node_view(node, project.title)["ref"], "fields": fields}
    if "project_id" in args:
        project = await store.get_project(user_id, uuid.UUID(args["project_id"]))
        if "revision" in args and project.revision != args["revision"]:
            raise Conflict("revision")
        return {"target": project.title, "fields": fields}
    if "relation_id" in args or "reminder_id" in args:
        # the row is checked when the person applies it; a foreign id answers like a missing one
        return {"target": capability, "fields": fields | {k: v for k, v in args.items()}}
    return {"target": capability, "fields": fields}


async def _reschedule(
    store: ProjectStore, user_id: uuid.UUID, args: dict[str, Any], zone: str
) -> dict[str, Any]:
    node = await store.get_node(user_id, uuid.UUID(args["node_id"]))
    if node.kind != "task":
        raise Invalid("only a task has dates")
    explicit = {k: args[k] for k in _DATES if k in args}
    if ("shift_days" in args) == bool(explicit):
        raise Invalid("give either shift_days or at least one of start_at, due_at, end_at")
    fields: dict[str, Any] = explicit
    if "shift_days" in args:
        tz = ZoneInfo(zone)
        for name in _DATES:
            value = getattr(node, name)
            if value is not None:
                # wall-clock arithmetic in the person's zone: 09:00 stays 09:00 over a clock change
                moved = value.astimezone(tz) + timedelta(days=args["shift_days"])
                fields[name] = clock.iso(moved)
        if not fields:
            raise Invalid("the task has no date to shift")
    moved_node = await store.update_node(user_id, node.id, revision=args["revision"], fields=fields)
    return await _node(store, user_id, moved_node)


async def _label(
    store: ProjectStore,
    user_id: uuid.UUID,
    title: str,
    *,
    create: bool,
    color: str | None = None,
) -> Any:
    wanted = title.strip().lower()
    for label in await store.list_labels(user_id):
        if label.title.lower() == wanted:
            return label
    if not create:
        raise NotFound("label")
    return await store.create_label(user_id, title=title, color=color)


async def _node(store: ProjectStore, user_id: uuid.UUID, node: Any) -> dict[str, Any]:
    project = await store.get_project(user_id, node.project_id)
    return node_view(node, project.title)


def _uuid(value: str | None) -> uuid.UUID | None:
    return None if value is None else uuid.UUID(value)


def _time(value: str | None) -> Any:
    if value is None:
        return None
    try:
        return clock.parse(value)
    except ValueError as exc:
        raise Invalid(str(exc)) from exc
