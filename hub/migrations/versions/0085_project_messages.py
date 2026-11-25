"""messages between the applications: chains and the columns of a message (PPH-05, 05a)

Revision ID: 0085
Revises: 0084
Create Date: 2026-10-29

A message is a node of kind ``report`` in the receiving project, with a type from the registry
(``proto/messages/registry.json``), the sending and receiving application, the chain, the round, its
state, the payload, how far to trust it, and what the code made of it. The reports that exist get
their type (``project.note`` for a note, and so on) and the requests of the old flow are turned into
``project.request`` messages; the four ``request_*`` columns stay until 05b has moved the flow.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0085"
down_revision: str | None = "0084"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "project_chains",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("application", sa.String(40), nullable=False, server_default="project"),
        sa.Column(
            "project_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("projects.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("title", sa.String(200), nullable=False, server_default=""),
        sa.Column("status", sa.String(10), nullable=False, server_default="active"),
        sa.Column("rounds", sa.Integer, nullable=False, server_default="1"),
        sa.Column("flagged_at", sa.DateTime(timezone=True)),
        sa.Column("flag_reason", sa.String(40)),
        sa.Column("stopped_at", sa.DateTime(timezone=True)),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.CheckConstraint(
            "status IN ('active', 'done', 'stopped')", name="ck_project_chains_status"
        ),
    )
    op.create_index("ix_project_chains_user_id", "project_chains", ["user_id"])
    op.create_index("ix_project_chains_project_id", "project_chains", ["project_id"])

    op.add_column("project_nodes", sa.Column("message_type", sa.String(40)))
    op.add_column("project_nodes", sa.Column("source_app", sa.String(10)))
    op.add_column("project_nodes", sa.Column("target_app", sa.String(10)))
    op.add_column(
        "project_nodes",
        sa.Column(
            "chain_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("project_chains.id", ondelete="SET NULL"),
        ),
    )
    op.add_column(
        "project_nodes",
        sa.Column(
            "in_reply_to",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("project_nodes.id", ondelete="SET NULL"),
        ),
    )
    op.add_column(
        "project_nodes", sa.Column("round", sa.Integer, nullable=False, server_default="1")
    )
    op.add_column("project_nodes", sa.Column("msg_state", sa.String(20)))
    op.add_column(
        "project_nodes",
        sa.Column(
            "task_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("agent_tasks.id", ondelete="SET NULL"),
        ),
    )
    op.add_column("project_nodes", sa.Column("payload", postgresql.JSONB))
    op.add_column("project_nodes", sa.Column("schema_version", sa.SmallInteger))
    op.add_column("project_nodes", sa.Column("trust", sa.String(10)))
    op.add_column("project_nodes", sa.Column("stats", postgresql.JSONB))
    op.add_column("project_nodes", sa.Column("verification", postgresql.JSONB))
    op.create_index("ix_project_nodes_message_type", "project_nodes", ["message_type"])
    op.create_index("ix_project_nodes_chain_id", "project_nodes", ["chain_id"])
    op.create_index("ix_project_nodes_task_id", "project_nodes", ["task_id"])

    # the reports that exist are messages of the project to itself ...
    op.execute(
        """
        UPDATE project_nodes
           SET message_type = 'project.' || report_kind, source_app = 'project',
               target_app = 'project', trust = 'own', schema_version = 1, msg_state = 'accepted'
         WHERE kind = 'report'
           AND report_kind IN ('note', 'question', 'decision', 'document', 'fact')
        """
    )
    # ... and the requests of the old flow are ``project.request`` messages to their target
    op.execute(
        """
        UPDATE project_nodes
           SET message_type = 'project.request', source_app = 'project',
               target_app = request_target, trust = 'own', schema_version = 1,
               task_id = request_task_id,
               msg_state = CASE
                   WHEN request_state = 'awaiting' THEN 'awaiting_click'
                   WHEN request_state = 'declined' THEN 'rejected'
                   WHEN request_task_id IS NOT NULL THEN 'queued'
                   ELSE 'sent' END,
               payload = jsonb_strip_nulls(jsonb_build_object(
                   'topic', left(title, 300),
                   'action', request_action,
                   'questions', CASE WHEN body = '' THEN '[]'::jsonb
                                     ELSE jsonb_build_array(left(body, 500)) END))
         WHERE kind = 'report' AND report_kind = 'request'
        """
    )


def downgrade() -> None:
    op.drop_index("ix_project_nodes_task_id", table_name="project_nodes")
    op.drop_index("ix_project_nodes_chain_id", table_name="project_nodes")
    op.drop_index("ix_project_nodes_message_type", table_name="project_nodes")
    for column in (
        "verification",
        "stats",
        "trust",
        "schema_version",
        "payload",
        "task_id",
        "msg_state",
        "round",
        "in_reply_to",
        "chain_id",
        "target_app",
        "source_app",
        "message_type",
    ):
        op.drop_column("project_nodes", column)
    op.drop_table("project_chains")
