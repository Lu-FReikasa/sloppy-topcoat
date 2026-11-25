"""The reading tools of the project agent (PPH-04, 04d). None of them changes anything, so a chat
runs them directly (the writing tools are only prepared there). All answers are compact and paged: a
list gives refs, titles and the few fields that decide what to look at next; the text of a node
comes only from ``project.node.get``. Text inside nodes is data, and every answer says where the
agent should call the help when a filter was wrong.

The tools see only the person's own rows: they go through :class:`ProjectStore`, which filters by
person and application, and a filter can narrow the result but never widen it.
"""

import uuid
from typing import Any

from topcoat_hub import project_filter
from topcoat_hub.project_events import KINDS
from topcoat_hub.project_events import view as event_view
from topcoat_hub.project_filter import FilterError
from topcoat_hub.project_progress import progress
from topcoat_hub.project_sort import SortError
from topcoat_hub.project_store import Invalid, ProjectStore
from topcoat_hub.project_versions import version_view
from topcoat_hub.project_views import (
    label_view,
    node_view,
    project_view,
    relation_view,
    reminder_view,
)

_UUID = {
    "type": "string",
    "pattern": "^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$",
}
MAX_LIMIT = 25


def _schema(properties: dict[str, Any], required: list[str]) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": properties,
        "required": required,
        "additionalProperties": False,
    }


_FILTER = {"type": "string", "maxLength": 1000}
_SORT = {"type": "string", "maxLength": 200}
_LIMIT = {"type": "integer", "minimum": 1, "maximum": MAX_LIMIT}
_AFTER = {"type": "integer", "minimum": 0}

READ_SPECS: dict[str, dict[str, Any]] = {
    "project.node.search": {
        "description": "Search the person's nodes across all projects. filter is a small query "
        "language, for example `status = in_review && priority >= 3` (call project.filter.help for "
        "the syntax); sort is a rule like `dueDate:asc,priority:desc`; limit is at most 25, after "
        "continues a list. Returns compact rows: ref, title, status, due date, priority, progress, "
        'labels. Filter fields: q (words in the title or text, `q = "release"`: there is no '
        "`title` field), status, kind, priority, dueDate, labels, project, done, blocked. "
        "Sort keys: number, status, dueDate, priority, title, created, updated (not `ref`). "
        "Search before you create anything.",
        "schema": _schema(
            {
                "filter": _FILTER,
                "sort": _SORT,
                "project_id": _UUID,
                "limit": _LIMIT,
                "after": _AFTER,
            },
            [],
        ),
    },
    "project.node.get": {
        "description": "One node in full: its text, dates, labels, relations (also to other "
        "projects), reminders and revision. Read a node before you change it.",
        "schema": _schema({"node_id": _UUID}, ["node_id"]),
    },
    "project.agenda": {
        "description": "What needs attention across all projects: overdue, due today and due in "
        "the next days (open tasks only), compact.",
        "schema": _schema({"days": {"type": "integer", "minimum": 1, "maximum": 60}}, []),
    },
    "project.summary": {
        "description": "A project in a few lines: progress, what is overdue, blocked or in review, "
        "open questions and milestones, the latest decisions and facts and the latest changes.",
        "schema": _schema({"project_id": _UUID}, ["project_id"]),
    },
    "project.timeline": {
        "description": "The Akte of a project: what happened to it, oldest first, in pages. kinds "
        "limits it to event kinds (like node.completed, body.changed), node to one node.",
        "schema": _schema(
            {
                "project_id": _UUID,
                "after": _AFTER,
                "kinds": {"type": "array", "items": {"type": "string"}, "maxItems": 10},
                "node_id": _UUID,
                "limit": _LIMIT,
            },
            ["project_id"],
        ),
    },
    "project.node.history": {
        "description": "The versions of a node's title and body (revision, time).",
        "schema": _schema({"node_id": _UUID}, ["node_id"]),
    },
    "project.node.at": {
        "description": "A node's title and body as they were at a revision.",
        "schema": _schema(
            {"node_id": _UUID, "rev": {"type": "integer", "minimum": 1}}, ["node_id", "rev"]
        ),
    },
    "project.node.diff": {
        "description": "What changed in a node's title and body between two revisions.",
        "schema": _schema(
            {
                "node_id": _UUID,
                "from_rev": {"type": "integer", "minimum": 1},
                "to_rev": {"type": "integer", "minimum": 1},
            },
            ["node_id", "from_rev", "to_rev"],
        ),
    },
    "project.label.list": {
        "description": "The person's labels.",
        "schema": _schema({}, []),
    },
    "project.view.list": {
        "description": "The person's saved views (a filter, a sort and a layout under a title), "
        "also the seeded ones like Today, Overdue and Ready.",
        "schema": _schema({"project_id": _UUID}, []),
    },
    "project.trash.list": {
        "description": "What is in the trash (projects and branches). Only the person empties or "
        "restores it; you may propose a restore as a change.",
        "schema": _schema({}, []),
    },
    "project.filter.help": {
        "description": "The syntax of the filter language, with examples. Call it when a filter "
        "was refused.",
        "schema": _schema({}, []),
    },
}
READ_NAMES = frozenset({"project.list", "project.get", *READ_SPECS})


def compact_row(
    node: Any, project_title: str, done: int | None, labels: list[Any]
) -> dict[str, Any]:
    """The few fields that decide what to open next."""
    return {
        "id": str(node.id),
        "ref": f"{project_title} {node.number}",
        "kind": node.kind,
        "title": node.title,
        "status": node.status,
        "due_at": node.due_at.isoformat() if node.due_at else None,
        "priority": node.priority,
        "progress": node.percent_done if done is None else done,
        "milestone": bool(node.is_milestone),
        "labels": [label.title for label in labels],
    }


async def _search(
    store: ProjectStore,
    user_id: uuid.UUID,
    zone: str,
    *,
    filter_text: str = "",
    sort: str = "",
    project_id: uuid.UUID | None = None,
    limit: int = MAX_LIMIT,
    after: int = 0,
) -> dict[str, Any]:
    try:
        found = await store.search_nodes(
            user_id,
            filter_text=filter_text,
            sort=sort,
            project_id=project_id,
            limit=limit,
            after=after,
            zone=zone,
        )
    except (FilterError, SortError) as exc:
        raise Invalid(str(exc)) from exc
    return {
        "nodes": [compact_row(n, title, done, labels) for n, title, done, labels in found.rows],
        "total": found.total,
        "next_after": found.next_after,
    }


async def run_read(
    store: ProjectStore, user_id: uuid.UUID, capability: str, args: dict[str, Any], *, zone: str
) -> Any:
    """Run one reading tool. Raises the store's errors; the caller maps them to tool errors."""
    if capability == "project.filter.help":
        return {"help": project_filter.help_text()}
    if capability == "project.node.search":
        pid = args.get("project_id")
        return await _search(
            store, user_id, zone, filter_text=args.get("filter", ""), sort=args.get("sort", ""),
            project_id=None if pid is None else uuid.UUID(pid), limit=args.get("limit", MAX_LIMIT),
            after=args.get("after", 0),
        )  # fmt: skip
    if capability == "project.agenda":
        days = args.get("days", 7)
        groups = {
            "overdue": "done = false && kind = task && dueDate < now/d",
            "today": "done = false && kind = task && dueDate >= now/d && dueDate < now/d+1d",
            "upcoming": "done = false && kind = task && dueDate >= now/d+1d "
            f"&& dueDate < now/d+{days + 1}d",
        }
        return {
            name: await _search(
                store, user_id, zone, filter_text=f, sort="dueDate,priority:desc", limit=MAX_LIMIT
            )
            for name, f in groups.items()
        }
    if capability == "project.node.get":
        node = await store.get_node(user_id, uuid.UUID(args["node_id"]))
        project = await store.get_project(user_id, node.project_id)
        labels = await store.labels_of(user_id, node.id)
        relations = []
        titles = {project.id: project.title}
        for relation, far in await store.relations_of(user_id, node.id):
            if far.project_id not in titles:
                titles[far.project_id] = (await store.get_project(user_id, far.project_id)).title
            relations.append(relation_view(relation, far, titles[far.project_id], node.id))
        siblings = await store.tree(user_id, node.project_id)
        done = progress(siblings).get(node.id)
        out: dict[str, Any] = {
            "node": node_view(node, project.title, progress=done, labels=labels),
            "relations": relations,
            "reminders": [reminder_view(r) for r in await store.reminders_of(user_id, node.id)],
        }
        # why the status is what it is (live 2026-11-10: "why is 2.1 blocked?" had no answer,
        # the hub had blocked it after a run's result check, which only the timeline said)
        changes = await store.timeline(
            user_id, node.project_id, after=0, kinds=["node.status_changed"], node_id=node.id,
            limit=MAX_LIMIT,
        )  # fmt: skip
        if changes:
            last = changes[-1]
            payload = last.payload or {}
            out["last_status_change"] = {
                "at": last.at.isoformat() if last.at else None,
                "from": payload.get("from"),
                "to": payload.get("to"),
                "by": last.actor_kind,
                "reason": payload.get("reason") or last.reason,
                "run": payload.get("task_id"),
            }
        return out
    if capability == "project.summary":
        from topcoat_hub.project_digest import project_digest

        return {
            "summary": await project_digest(
                store, user_id, uuid.UUID(args["project_id"]), zone=zone
            )
        }
    if capability == "project.timeline":
        kinds = args.get("kinds") or None
        if kinds and any(k not in KINDS for k in kinds):
            raise Invalid("unknown event kind; the kinds are " + ", ".join(KINDS))
        node = args.get("node_id")
        limit = args.get("limit", MAX_LIMIT)
        events = await store.timeline(
            user_id, uuid.UUID(args["project_id"]), after=args.get("after", 0), kinds=kinds,
            node_id=None if node is None else uuid.UUID(node), limit=limit,
        )  # fmt: skip
        return {
            "events": [_compact_event(e) for e in events],
            "next_after": events[-1].seq if len(events) == limit else None,
        }
    if capability == "project.node.history":
        rows = await store.node_history(user_id, uuid.UUID(args["node_id"]))
        return {"versions": [version_view(v, at) for v, at in rows]}
    if capability == "project.node.at":
        return await store.node_at(user_id, uuid.UUID(args["node_id"]), args["rev"])
    if capability == "project.node.diff":
        diff = await store.node_diff(
            user_id, uuid.UUID(args["node_id"]), args["from_rev"], args["to_rev"]
        )
        diff["body"] = [d for d in diff["body"] if d["tag"] != "equal"]  # only what changed
        return diff
    if capability == "project.label.list":
        return {"labels": [label_view(label) for label in await store.list_labels(user_id)]}
    if capability == "project.view.list":
        pid = args.get("project_id")
        views = await store.list_views(user_id, None if pid is None else uuid.UUID(pid))
        return {
            "views": [
                {
                    "id": str(v.id),
                    "title": v.title,
                    "type": v.type,
                    "filter": v.filter,
                    "sort": v.sort,
                }
                for v in views
            ]
        }
    if capability == "project.trash.list":
        projects, nodes = await store.list_trash(user_id)
        return {
            "projects": [project_view(p) | {"scope": None} for p in projects],
            "nodes": [compact_row(n, p.title, None, []) for n, p in nodes],
        }
    raise KeyError(capability)


def _compact_event(e: Any) -> dict[str, Any]:
    """An event for the agent: what, who, where, and the payload."""
    out = event_view(e)
    return {
        "seq": out["seq"],
        "at": out["at"],
        "kind": out["kind"],
        "actor": out["actor"]["kind"],
        "node": out["node_ref"],
        "payload": out["payload"],
    }
