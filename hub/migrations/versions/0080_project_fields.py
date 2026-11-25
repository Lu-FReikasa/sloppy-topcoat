"""project fields: dates, labels, relations across projects, reminders, trash (PPH-04, 04b)
Revision ID: 0080 Revises: 0079 Create Date: 2026-10-29

Tasks get dates (start, due, end), a priority, a percentage, a colour, an estimate, a repeat rule, a
milestone flag, a favourite flag and a place in the trash; reports get the date their content
describes, the application they came from and a "replaced by" link; projects get a parent, a colour,
a favourite flag, an identifier and a trash flag. Labels, relations and reminders are new tables.
``depends_on`` (a JSON list of task ids inside one project) becomes ``blocked_by`` relations, which
may cross project borders and are never deleted (they end). The column is dropped; the downgrade
rebuilds it from the open relations inside one project. The relations that came from the old lists
carry no event: the Akte starts them at the time of the migration.

The people's time zone (``users.timezone``) is new too (decision D4-4).
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0080"
down_revision: str | None = "0079"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

NODE_COLUMNS = (
    "start_at", "due_at", "end_at", "priority", "percent_done", "done_at", "color",
    "estimate_minutes", "repeat_rule", "is_milestone", "favorite", "trashed_at", "as_of",
    "origin_app", "current", "supersedes_id",
)  # fmt: skip


def upgrade() -> None:
    op.add_column("users", sa.Column("timezone", sa.String(64)))

    op.add_column(
        "projects",
        sa.Column(
            "parent_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("projects.id", ondelete="SET NULL"),
        ),
    )
    op.add_column(
        "projects", sa.Column("favorite", sa.Boolean, nullable=False, server_default="false")
    )
    op.add_column("projects", sa.Column("color", sa.String(20)))
    op.add_column("projects", sa.Column("identifier", sa.String(10)))
    op.add_column("projects", sa.Column("trashed_at", sa.DateTime(timezone=True)))
    op.create_index(
        "uq_projects_identifier",
        "projects",
        ["user_id", "identifier"],
        unique=True,
        postgresql_where=sa.text("identifier IS NOT NULL"),
    )

    for name in ("start_at", "due_at", "end_at", "done_at", "trashed_at", "as_of"):
        op.add_column("project_nodes", sa.Column(name, sa.DateTime(timezone=True)))
    op.add_column(
        "project_nodes", sa.Column("priority", sa.SmallInteger, nullable=False, server_default="0")
    )
    op.add_column(
        "project_nodes",
        sa.Column("percent_done", sa.SmallInteger, nullable=False, server_default="0"),
    )
    op.add_column("project_nodes", sa.Column("color", sa.String(20)))
    op.add_column("project_nodes", sa.Column("estimate_minutes", sa.Integer))
    op.add_column("project_nodes", sa.Column("repeat_rule", postgresql.JSONB))
    op.add_column(
        "project_nodes",
        sa.Column("is_milestone", sa.Boolean, nullable=False, server_default="false"),
    )
    op.add_column(
        "project_nodes", sa.Column("favorite", sa.Boolean, nullable=False, server_default="false")
    )
    op.add_column("project_nodes", sa.Column("origin_app", sa.String(40)))
    op.add_column(
        "project_nodes", sa.Column("current", sa.Boolean, nullable=False, server_default="true")
    )
    op.add_column(
        "project_nodes",
        sa.Column(
            "supersedes_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("project_nodes.id", ondelete="SET NULL"),
        ),
    )
    op.create_check_constraint(
        "ck_project_nodes_priority", "project_nodes", "priority BETWEEN 0 AND 5"
    )
    op.create_check_constraint(
        "ck_project_nodes_percent", "project_nodes", "percent_done BETWEEN 0 AND 100"
    )

    op.create_table(
        "project_labels",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("application", sa.String(40), nullable=False, server_default="project"),
        sa.Column("title", sa.String(60), nullable=False),
        sa.Column("color", sa.String(20)),
        sa.Column("description", sa.String(200), nullable=False, server_default=""),
        sa.UniqueConstraint("user_id", "application", "title", name="uq_project_labels_title"),
    )
    op.create_index("ix_project_labels_user_id", "project_labels", ["user_id"])
    op.create_table(
        "project_node_labels",
        sa.Column(
            "node_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("project_nodes.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column(
            "label_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("project_labels.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column(
            "user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
    )
    op.create_index("ix_project_node_labels_user_id", "project_node_labels", ["user_id"])

    op.create_table(
        "project_relations",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("application", sa.String(40), nullable=False, server_default="project"),
        sa.Column(
            "node_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("project_nodes.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "other_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("project_nodes.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("kind", sa.String(14), nullable=False),
        sa.Column("valid_from", sa.DateTime(timezone=True), nullable=False),
        sa.Column("valid_to", sa.DateTime(timezone=True)),
        sa.CheckConstraint(
            "kind IN ('blocked_by', 'related', 'duplicate_of', 'precedes', 'copied_from',"
            " 'supersedes')",
            name="ck_project_relations_kind",
        ),
        sa.CheckConstraint("node_id <> other_id", name="ck_project_relations_self"),
    )
    op.create_index("ix_project_relations_user_id", "project_relations", ["user_id"])
    op.create_index("ix_project_relations_node_id", "project_relations", ["node_id"])
    op.create_index("ix_project_relations_other_id", "project_relations", ["other_id"])
    op.create_index(
        "uq_project_relations_open",
        "project_relations",
        ["node_id", "other_id", "kind"],
        unique=True,
        postgresql_where=sa.text("valid_to IS NULL"),
    )

    op.create_table(
        "project_reminders",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("application", sa.String(40), nullable=False, server_default="project"),
        sa.Column(
            "node_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("project_nodes.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("at", sa.DateTime(timezone=True)),
        sa.Column("relative_to", sa.String(5)),
        sa.Column("offset_seconds", sa.Integer),
        sa.Column("fire_at", sa.DateTime(timezone=True)),
        sa.Column("fired_at", sa.DateTime(timezone=True)),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.CheckConstraint(
            "(at IS NOT NULL) <> (relative_to IS NOT NULL)", name="ck_project_reminders_kind"
        ),
        sa.CheckConstraint(
            "relative_to IS NULL OR relative_to IN ('due', 'start', 'end')",
            name="ck_project_reminders_base",
        ),
    )
    op.create_index("ix_project_reminders_user_id", "project_reminders", ["user_id"])
    op.create_index("ix_project_reminders_node_id", "project_reminders", ["node_id"])
    op.create_index(
        "ix_project_reminders_due",
        "project_reminders",
        ["fire_at"],
        postgresql_where=sa.text("fired_at IS NULL"),
    )

    # depends_on -> blocked_by relations (a dependency that no longer exists is dropped)
    op.execute(
        """
        INSERT INTO project_relations (id, user_id, application, node_id, other_id, kind,
        valid_from)
        SELECT gen_random_uuid(), n.user_id, n.application, n.id, d.other::uuid, 'blocked_by', now()
        FROM project_nodes n, jsonb_array_elements_text(n.depends_on) AS d(other)
        WHERE EXISTS (SELECT 1 FROM project_nodes o WHERE o.id = d.other::uuid AND o.id <> n.id)
        """
    )
    op.drop_column("project_nodes", "depends_on")


def downgrade() -> None:
    op.add_column(
        "project_nodes",
        sa.Column("depends_on", postgresql.JSONB, nullable=False, server_default="[]"),
    )
    # only the open blocked_by relations inside one project fit into the old column
    op.execute(
        """
        UPDATE project_nodes n SET depends_on = COALESCE((
            SELECT jsonb_agg(r.other_id::text)
            FROM project_relations r JOIN project_nodes o ON o.id = r.other_id
            WHERE r.node_id = n.id AND r.kind = 'blocked_by' AND r.valid_to IS NULL
              AND o.project_id = n.project_id
        ), '[]'::jsonb)
        """
    )
    op.drop_table("project_reminders")
    op.drop_table("project_relations")
    op.drop_table("project_node_labels")
    op.drop_table("project_labels")
    op.drop_constraint("ck_project_nodes_percent", "project_nodes", type_="check")
    op.drop_constraint("ck_project_nodes_priority", "project_nodes", type_="check")
    for name in reversed(NODE_COLUMNS):
        op.drop_column("project_nodes", name)
    op.drop_index("uq_projects_identifier", "projects")
    for name in ("trashed_at", "identifier", "color", "favorite", "parent_id"):
        op.drop_column("projects", name)
    op.drop_column("users", "timezone")
