"""Export of one project of the signed-in person (PPH-04, 04j): everything as one JSON document, and
as a folder of Markdown files (a zip) that reads without Topcoat.

Scoped by ``user_id`` and the application like every other query: another person's project is a
:class:`NotFound`. The trashed nodes are in the export only when asked for. The Akte is always whole
(the hash chain cannot be checked with lines missing); it holds the titles of trashed nodes as the
lines were written. The result of the chain check is part of the export, so a reader of the file
knows whether the lines it holds were intact when it was made.

There is no import (the brief wants none); :mod:`project_templates` creates a project from the
structure.
"""

import io
import json
import re
import uuid
import zipfile
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from topcoat_hub import clock
from topcoat_hub.models import (
    Project,
    ProjectLabel,
    ProjectNode,
    ProjectNodeLabel,
    ProjectRelation,
    ProjectReminder,
)
from topcoat_hub.project_base import by_number, get_nodes, get_project, owned
from topcoat_hub.project_events import KINDS, EventLog, verify
from topcoat_hub.project_events import view as event_view
from topcoat_hub.project_progress import progress
from topcoat_hub.project_views import node_view, project_view, reminder_view

FORMAT = "topcoat-project-export"
VERSION = 1


def _ref(project_title: str, node: ProjectNode) -> str:
    return f"{project_title} {node.number}"


async def build(
    db: AsyncSession,
    user_id: uuid.UUID,
    project_id: uuid.UUID,
    *,
    include_trashed: bool = False,
) -> dict[str, Any]:
    """The whole project as a JSON-ready document."""
    project = await get_project(db, user_id, project_id)
    nodes = by_number(await get_nodes(db, user_id, project_id, trashed=include_trashed))
    ids = [n.id for n in nodes]
    done = progress(nodes)
    labels = await _labels(db, user_id, ids)
    reminders = await _reminders(db, user_id, ids)
    relations = await _relations(db, user_id, project, nodes)
    chain = await EventLog.chain(db, user_id, project_id)
    problems = verify(chain)
    used = {label.id: label for found in labels.values() for label in found}
    return {
        "format": FORMAT,
        "version": VERSION,
        "exported_at": clock.iso(datetime.now(UTC)),
        "include_trashed": include_trashed,
        "project": project_view(project),
        "labels": [
            {"id": str(lb.id), "title": lb.title, "color": lb.color, "description": lb.description}
            for lb in sorted(used.values(), key=lambda lb: lb.title)
        ],
        "nodes": [
            node_view(n, project.title, progress=done.get(n.id), labels=labels.get(n.id, []))
            | {"reminders": [reminder_view(r) for r in reminders.get(n.id, [])]}
            for n in nodes
        ],
        "relations": relations,
        "akte": {
            "events": [event_view(e) for e in chain],
            "chain": {"ok": not problems, "events": len(chain), "problems": problems[:20]},
        },
    }


async def _labels(
    db: AsyncSession, user_id: uuid.UUID, node_ids: list[uuid.UUID]
) -> dict[uuid.UUID, list[ProjectLabel]]:
    out: dict[uuid.UUID, list[ProjectLabel]] = {}
    if not node_ids:
        return out
    rows = await db.execute(
        select(ProjectNodeLabel.node_id, ProjectLabel)
        .join(ProjectLabel, ProjectLabel.id == ProjectNodeLabel.label_id)
        .where(ProjectNodeLabel.user_id == user_id, ProjectNodeLabel.node_id.in_(node_ids))
        .order_by(ProjectLabel.title)
    )
    for node_id, label in rows:
        out.setdefault(node_id, []).append(label)
    return out


async def _reminders(
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


async def _relations(
    db: AsyncSession, user_id: uuid.UUID, project: Project, nodes: list[ProjectNode]
) -> list[dict[str, Any]]:
    """Every relation (open and ended) with at least one end in the export. The far end may lie in
    another project of the same person; it is named by reference, title and id only."""
    ids = {n.id for n in nodes}
    if not ids:
        return []
    rows = list(
        await db.scalars(
            select(ProjectRelation)
            .where(
                owned(ProjectRelation, user_id),
                ProjectRelation.node_id.in_(ids) | ProjectRelation.other_id.in_(ids),
            )
            .order_by(ProjectRelation.valid_from, ProjectRelation.id)
        )
    )
    wanted = {r.node_id for r in rows} | {r.other_id for r in rows}
    ends = {
        n.id: (n, t)
        for n, t in (
            await db.execute(
                select(ProjectNode, Project.title)
                .join(Project, Project.id == ProjectNode.project_id)
                .where(owned(ProjectNode, user_id), ProjectNode.id.in_(wanted))
            )
        ).all()
    }

    def end(node_id: uuid.UUID) -> dict[str, Any]:
        node, title = ends[node_id]
        return {
            "id": str(node.id),
            "ref": _ref(title, node),
            "title": node.title,
            "in_project": node.project_id == project.id,
        }

    return [
        {
            "id": str(r.id),
            "kind": r.kind,
            "node": end(r.node_id),
            "other": end(r.other_id),
            "valid_from": clock.iso(r.valid_from),
            "valid_to": clock.iso(r.valid_to),
        }
        for r in rows
        if r.node_id in ends and r.other_id in ends
    ]


# --- the Markdown folder ------------------------------------------------------------------------

_SLUG = re.compile(r"[^a-z0-9]+")
_NUMBER = re.compile(r"[^0-9A-Za-z.]")


def slug(text: str) -> str:
    return _SLUG.sub("-", text.lower()).strip("-")[:40] or "node"


def _file_name(node: dict[str, Any]) -> str:
    """``nodes/1.2-write-the-plan.md``: the number is digits and dots, the rest is cut to ASCII, so
    no title can make a path leave the folder."""
    return f"nodes/{_NUMBER.sub('_', node['number'])}-{slug(node['title'])}.md"


def _line(label: str, value: Any) -> str:
    return f"- **{label}:** {value}\n" if value not in (None, "", [], False) else ""


def node_markdown(doc: dict[str, Any], node: dict[str, Any]) -> str:
    kind = node["report_kind"] if node["kind"] == "report" else node["kind"]
    out = f"# {node['ref']} {node['title']}\n\n"
    out += _line("Kind", kind)
    out += _line("Status", node["status"])
    for key in ("start_at", "due_at", "end_at", "done_at", "as_of"):
        out += _line(key.replace("_", " "), node[key])
    out += _line("Priority", node["priority"])
    out += _line("Progress", f"{node['progress']} %")
    out += _line("Estimate (minutes)", node["estimate_minutes"])
    out += _line("Milestone", node["is_milestone"])
    out += _line("Repeats", node["repeat_rule"])
    out += _line("Labels", ", ".join(lb["title"] for lb in node.get("labels", [])))
    out += _line("In the trash since", node["trashed_at"])
    out += _line("Answer", node["answer"])
    message = node.get("message")
    if message:
        out += _line("Message", f"{message['type']} {message['from']} → {message['to']}")
        out += _line("Message state", message["state"])
    for r in node["reminders"]:
        when = r["at"] or f"{r['offset_seconds']} s from {r['relative_to']}"
        out += _line("Reminder", f"{when} (fires {r['fire_at']}, fired {r['fired_at']})")
    for rel in doc["relations"]:
        if node["id"] in (rel["node"]["id"], rel["other"]["id"]):
            here = rel["node"]["id"] == node["id"]
            far = rel["other"] if here else rel["node"]
            arrow = "→" if here else "←"
            ended = f", ended {rel['valid_to']}" if rel["valid_to"] else ""
            out += _line("Relation", f"{rel['kind']} {arrow} {far['ref']} {far['title']}{ended}")
    if node["parent_id"]:
        out += _line("Parent", node["parent_id"])
    return out + (f"\n{node['body']}\n" if node["body"] else "")


def timeline_markdown(doc: dict[str, Any]) -> str:
    chain = doc["akte"]["chain"]
    state = "intact" if chain["ok"] else "BROKEN: " + "; ".join(chain["problems"])
    out = f"# Timeline of {doc['project']['title']}\n\n"
    out += f"Hash chain: {state} ({chain['events']} lines)\n\n"
    for e in doc["akte"]["events"]:
        label = KINDS.get(e["kind"], {}).get("en", e["kind"])
        who = e["actor"]["kind"] + (f" ({e['actor']['app']})" if e["actor"]["app"] else "")
        what = f" — {e['node_ref']} {e['node_title']}" if e["node_ref"] else ""
        out += f"## {e['seq']}. {e['at']} · {label}{what}\n\nBy: {who}\n"
        if e["reason"]:
            out += f"\nReason: {e['reason']}\n"
        if e["payload"]:
            out += f"\n```json\n{_json(e['payload'])}\n```\n"
        out += "\n"
    return out


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True, default=str)


def overview_markdown(doc: dict[str, Any]) -> str:
    p = doc["project"]
    out = f"# {p['title']}\n\n"
    out += _line("Status", p["status"]) + _line("Identifier", p["identifier"])
    out += _line("Exported", doc["exported_at"]) + _line("Trash included", doc["include_trashed"])
    out += f"\n{p['description']}\n\n" if p["description"] else "\n"
    out += "## Nodes\n\n"
    for n in doc["nodes"]:
        depth = n["number"].count(".")
        status = f" [{n['status']}]" if n["status"] else ""
        out += f"{'  ' * depth}- [{n['ref']} {n['title']}]({_file_name(n)}){status}\n"
    return out + "\n[Timeline](timeline.md)\n"


def to_zip(doc: dict[str, Any]) -> bytes:
    """The Markdown folder as a zip: ``README.md``, ``timeline.md``, one file per node under
    ``nodes/``. The complete JSON export is in the zip too (``project.json``)."""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("README.md", overview_markdown(doc))
        zf.writestr("timeline.md", timeline_markdown(doc))
        zf.writestr("project.json", _json(doc))
        for node in doc["nodes"]:
            zf.writestr(_file_name(node), node_markdown(doc, node))
    return buffer.getvalue()


class ProjectExporter:
    def __init__(self, sessionmaker: async_sessionmaker[AsyncSession]) -> None:
        self._sessionmaker = sessionmaker

    async def document(
        self, user_id: uuid.UUID, project_id: uuid.UUID, *, include_trashed: bool = False
    ) -> dict[str, Any]:
        async with self._sessionmaker() as db:
            return await build(db, user_id, project_id, include_trashed=include_trashed)

    async def archive(
        self, user_id: uuid.UUID, project_id: uuid.UUID, *, include_trashed: bool = False
    ) -> bytes:
        return to_zip(await self.document(user_id, project_id, include_trashed=include_trashed))
