"""How a Projekte row looks to the web page and to the project agent (PPH-01b, rework 2026-10-27).

One place for the shape, so the REST routes and the agent's tools answer with the same fields,
and both carry ``revision`` for the optimistic lock. A node's ``ref`` is the way to name it in
the chat: the project's title and its number, such as ``Topcoat 1.3.2``. Times leave the hub with an
offset (ISO 8601).
"""

from datetime import datetime
from typing import Any

from topcoat_hub.models import Project, ProjectLabel, ProjectNode, ProjectRelation, ProjectReminder


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


def project_view(p: Project) -> dict[str, Any]:
    return {
        "id": str(p.id),
        "title": p.title,
        "description": p.description,
        "status": p.status,
        "scope": p.scope or {},
        "parent_id": str(p.parent_id) if p.parent_id else None,
        "favorite": bool(p.favorite),
        "color": p.color,
        "identifier": p.identifier,
        "trashed_at": _iso(p.trashed_at),
        "revision": p.revision,
        "created_at": _iso(p.created_at),
        "updated_at": _iso(p.updated_at),
    }


def node_view(
    n: ProjectNode,
    project_title: str,
    *,
    progress: int | None = None,
    labels: list[ProjectLabel] | None = None,
) -> dict[str, Any]:
    out = {
        "id": str(n.id),
        "project_id": str(n.project_id),
        "ref": f"{project_title} {n.number}",
        "number": n.number,
        "parent_id": str(n.parent_id) if n.parent_id else None,
        "kind": n.kind,
        "report_kind": n.report_kind,
        "title": n.title,
        "body": n.body,
        "status": n.status,
        "depends_on": [str(d) for d in n.depends_on],
        "start_at": _iso(n.start_at),
        "due_at": _iso(n.due_at),
        "end_at": _iso(n.end_at),
        "priority": n.priority,
        "percent_done": n.percent_done,
        "progress": n.percent_done if progress is None else progress,
        "done_at": _iso(n.done_at),
        "color": n.color,
        "estimate_minutes": n.estimate_minutes,
        "repeat_rule": n.repeat_rule,
        "is_milestone": bool(n.is_milestone),
        "favorite": bool(n.favorite),
        "trashed_at": _iso(n.trashed_at),
        "as_of": _iso(n.as_of),
        "origin_app": n.origin_app,
        "current": bool(n.current),
        "supersedes_id": str(n.supersedes_id) if n.supersedes_id else None,
        "answer": n.answer,
        "answered_at": _iso(n.answered_at),
        "revision": n.revision,
        "updated_at": _iso(n.updated_at),
    }
    if n.kind == "report" and n.iteration is not None:  # a result of a task (project_iterations)
        out["iteration"] = n.iteration
        out["based_on_id"] = str(n.based_on_id) if n.based_on_id else None
        out["feedback"] = n.feedback
    if n.kind == "task":
        out["requirements"] = list(n.requirements or [])
        out["iterating"] = n.next_iteration is not None
        # the application the task works in and whether it may propose changes (project_runs.py)
        from topcoat_hub.project_runs import mode_of, target_of

        out["application"] = target_of(n.body or "")
        out["mode"] = mode_of(n.body or "")
    if labels is not None:
        out["labels"] = [label_view(label) for label in labels]
    if n.message_type is not None:
        out["message"] = message_view(n)
    return out


def message_view(n: ProjectNode) -> dict[str, Any]:
    """What a message adds to its node (PPH-05): type, the two applications, the chain, the round,
    the state, the payload the registry checked, how far to trust it, and what the code made of
    it."""
    return {
        "type": n.message_type,
        "from": n.source_app,
        "to": n.target_app,
        "chain_id": str(n.chain_id) if n.chain_id else None,
        "in_reply_to": str(n.in_reply_to) if n.in_reply_to else None,
        "round": n.round,
        "state": n.msg_state,
        "task_id": str(n.task_id) if n.task_id else None,
        "payload": n.payload,
        "schema_version": n.schema_version,
        "trust": n.trust,
        "stats": n.stats,
        "verification": n.verification,
    }


def label_view(label: ProjectLabel) -> dict[str, Any]:
    return {
        "id": str(label.id),
        "title": label.title,
        "color": label.color,
        "description": label.description,
    }


def relation_view(
    r: ProjectRelation, far: ProjectNode, far_project_title: str, node_id: Any
) -> dict[str, Any]:
    """A relation as seen from the node ``node_id``: ``direction`` says which side it is on."""
    return {
        "id": str(r.id),
        "kind": r.kind,
        "direction": "out" if r.node_id == node_id else "in",
        "other": {
            "id": str(far.id),
            "ref": f"{far_project_title} {far.number}",
            "title": far.title,
            "project_id": str(far.project_id),
        },
        "valid_from": _iso(r.valid_from),
        "valid_to": _iso(r.valid_to),
    }


def reminder_view(r: ProjectReminder) -> dict[str, Any]:
    return {
        "id": str(r.id),
        "node_id": str(r.node_id),
        "at": _iso(r.at),
        "relative_to": r.relative_to,
        "offset_seconds": r.offset_seconds,
        "fire_at": _iso(r.fire_at),
        "fired_at": _iso(r.fired_at),
    }
