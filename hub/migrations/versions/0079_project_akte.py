"""the Akte of a project: events and versions of titles and bodies (PPH-04, 04a)

Revision ID: 0079
Revises: 0078
Create Date: 2026-10-29

``project_events`` is append-only and hash-chained per project; ``project_node_versions`` holds the
history of a node's title and body as snapshots and line diffs. Projects and nodes that exist now
get one ``baseline`` line each ("history starts here") and a snapshot, so the timeline never starts
with a change to something it has not heard of.

The hash functions are a frozen copy of ``topcoat_hub.project_events``: a migration must keep
producing the same lines even when the application code moves on.
"""

import hashlib
import json
import uuid
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0079"
down_revision: str | None = "0078"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

GENESIS = "0" * 64


def _canonical(value: Any) -> bytes:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str
    ).encode()


def _payload_hash(payload: Any, reason: str) -> str:
    return hashlib.sha256(_canonical({"payload": payload, "reason": reason})).hexdigest()


def upgrade() -> None:
    op.create_table(
        "project_events",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("application", sa.String(40), nullable=False, server_default="project"),
        sa.Column("seq", sa.BigInteger, nullable=False),
        sa.Column("at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("project_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("node_id", postgresql.UUID(as_uuid=True)),
        sa.Column("project_title", sa.String(200), nullable=False),
        sa.Column("node_ref", sa.String(260)),
        sa.Column("node_title", sa.String(200)),
        sa.Column("actor_kind", sa.String(10), nullable=False),
        sa.Column("actor_app", sa.String(40)),
        sa.Column("agent_task_id", postgresql.UUID(as_uuid=True)),
        sa.Column("chain_id", postgresql.UUID(as_uuid=True)),
        sa.Column("change_set_id", postgresql.UUID(as_uuid=True)),
        sa.Column("group_id", postgresql.UUID(as_uuid=True)),
        sa.Column("kind", sa.String(40), nullable=False),
        sa.Column("payload", postgresql.JSONB),
        sa.Column("reason", sa.Text, nullable=False, server_default=""),
        sa.Column("payload_hash", sa.String(64), nullable=False),
        sa.Column("prev_hash", sa.String(64), nullable=False),
        sa.Column("hash", sa.String(64), nullable=False),
        sa.UniqueConstraint("project_id", "seq", name="uq_project_events_seq"),
    )
    op.create_index("ix_project_events_user_id", "project_events", ["user_id"])
    op.create_index("ix_project_events_node", "project_events", ["project_id", "node_id"])
    op.create_table(
        "project_node_versions",
        sa.Column(
            "node_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("project_nodes.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("rev", sa.Integer, primary_key=True),
        sa.Column(
            "user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("application", sa.String(40), nullable=False, server_default="project"),
        sa.Column("kind", sa.String(8), nullable=False),
        sa.Column("data", postgresql.JSONB, nullable=False),
        sa.Column("event_id", postgresql.UUID(as_uuid=True)),
    )
    op.create_index("ix_project_node_versions_user_id", "project_node_versions", ["user_id"])
    _baseline()


_EVENT_COLUMNS = (
    "id", "user_id", "application", "seq", "at", "project_id", "node_id", "project_title",
    "node_ref", "node_title", "actor_kind", "actor_app", "agent_task_id", "chain_id",
    "change_set_id", "group_id", "kind", "payload", "reason", "payload_hash", "prev_hash", "hash",
)  # fmt: skip
_VERSION_COLUMNS = ("node_id", "rev", "user_id", "application", "kind", "data", "event_id")


class _Chain:
    """The baseline lines of one project, chained like the application writes them."""

    def __init__(self, bind: Any, events: Any, project: Any) -> None:
        self.bind, self.events, self.project = bind, events, project
        self.seq, self.prev = 0, GENESIS

    def add(self, node: Any, payload: dict[str, Any]) -> uuid.UUID:
        p = self.project
        self.seq += 1
        at = datetime.now(UTC)
        node_ref = None if node is None else f"{p.title} {node.number}"
        node_title = None if node is None else node.title
        phash = _payload_hash(payload, "")
        core = {
            "user_id": str(p.user_id),
            "application": p.application,
            "seq": self.seq,
            "at": at.astimezone(UTC).isoformat(),
            "project_id": str(p.id),
            "node_id": None if node is None else str(node.id),
            "project_title": p.title,
            "node_ref": node_ref,
            "node_title": node_title,
            "actor_kind": "system",
            "actor_app": None,
            "agent_task_id": None,
            "chain_id": None,
            "change_set_id": None,
            "group_id": None,
            "kind": "baseline",
            "payload_hash": phash,
        }
        digest = hashlib.sha256(_canonical({"core": core, "prev": self.prev})).hexdigest()
        row = {
            "id": uuid.uuid4(),
            "user_id": p.user_id,
            "application": p.application,
            "seq": self.seq,
            "at": at,
            "project_id": p.id,
            "node_id": None if node is None else node.id,
            "project_title": p.title,
            "node_ref": node_ref,
            "node_title": node_title,
            "actor_kind": "system",
            "actor_app": None,
            "agent_task_id": None,
            "chain_id": None,
            "change_set_id": None,
            "group_id": None,
            "kind": "baseline",
            "payload": payload,
            "reason": "",
            "payload_hash": phash,
            "prev_hash": self.prev,
            "hash": digest,
        }
        self.bind.execute(sa.insert(self.events).values(**row))
        self.prev = digest
        return row["id"]


def _baseline() -> None:
    """One line per existing project and node, and a snapshot of each node."""
    bind = op.get_bind()
    projects = bind.execute(
        sa.text("SELECT id, user_id, application, title, status FROM projects ORDER BY created_at")
    ).all()
    jsonb = postgresql.JSONB
    events = sa.table(
        "project_events",
        *[sa.column(c, jsonb) if c == "payload" else sa.column(c) for c in _EVENT_COLUMNS],
    )
    versions = sa.table(
        "project_node_versions",
        *[sa.column(c, jsonb) if c == "data" else sa.column(c) for c in _VERSION_COLUMNS],
    )
    for p in projects:
        nodes = bind.execute(
            sa.text(
                "SELECT id, number, kind, title, body, status, revision FROM project_nodes"
                " WHERE project_id = :p ORDER BY created_at, number"
            ),
            {"p": p.id},
        ).all()
        chain = _Chain(bind, events, p)
        chain.add(None, {"title": p.title, "status": p.status, "nodes": len(nodes)})
        for n in nodes:
            event_id = chain.add(n, {"kind": n.kind, "number": n.number, "status": n.status})
            bind.execute(
                sa.insert(versions).values(
                    node_id=n.id,
                    rev=n.revision,
                    user_id=p.user_id,
                    application=p.application,
                    kind="snapshot",
                    data={"title": n.title, "body": n.body},
                    event_id=event_id,
                )
            )


def downgrade() -> None:
    op.drop_table("project_node_versions")
    op.drop_table("project_events")
