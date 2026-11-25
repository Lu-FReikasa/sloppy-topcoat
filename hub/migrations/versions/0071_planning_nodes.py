"""planning: projects by title, one numbered tree (topics, tasks, reports), scope per project

Revision ID: 0071
Revises: 0070
Create Date: 2026-10-27

The Projekte rework (decided with the maintainer, 2026-10-27):

- a project has a unique title per person and application; it has a free description and no
  kind and no goal/state fields. Its scope says what the planning agent may do in it.
- ``planning_tasks`` becomes ``planning_nodes``: a topic (a folder), a task, or a report. Every
  node has a number that is its identity (``1``, ``1.2``, ``1.2.1``) and is
  never reused. Notes and questions become reports.
- nothing is hard-deleted; the table that held notes is dropped after its rows moved.

Existing rows are numbered in the order of their position and creation. Two projects of the same
person with the same title get " (2)", " (3)" ... on the later ones. Reverting this is not
possible: restore the database from a backup instead.
"""

import uuid
from collections import defaultdict
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0071"
down_revision: str | None = "0070"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


UUID = postgresql.UUID(as_uuid=True)


def upgrade() -> None:
    bind = op.get_bind()

    # --- projects -----------------------------------------------------------------------------
    op.add_column(
        "planning_projects",
        sa.Column("description", sa.Text, nullable=False, server_default=""),
    )
    op.add_column(
        "planning_projects",
        sa.Column("scope", postgresql.JSONB, nullable=False, server_default="{}"),
    )
    op.add_column(
        "planning_projects",
        sa.Column("next_child", sa.Integer, nullable=False, server_default="0"),
    )
    bind.execute(
        sa.text(
            "UPDATE planning_projects SET description = trim(both E'\\n' FROM concat_ws("
            "E'\\n\\n', nullif(goal, ''), nullif(current_state, ''), nullif(target_state, '')))"
        )
    )
    _make_titles_unique(bind)
    op.create_unique_constraint(
        "uq_planning_projects_title", "planning_projects", ["user_id", "application", "title"]
    )
    op.drop_column("planning_projects", "kind")
    op.drop_column("planning_projects", "goal")
    op.drop_column("planning_projects", "current_state")
    op.drop_column("planning_projects", "target_state")

    # --- nodes: the task tree becomes a numbered node tree ------------------------------------
    op.rename_table("planning_tasks", "planning_nodes")
    op.alter_column("planning_nodes", "description", new_column_name="body")
    op.add_column("planning_nodes", sa.Column("number", sa.String(40), nullable=True))
    op.add_column(
        "planning_nodes",
        sa.Column("kind", sa.String(8), nullable=False, server_default="task"),
    )
    op.add_column("planning_nodes", sa.Column("report_kind", sa.String(10), nullable=True))
    op.add_column(
        "planning_nodes",
        sa.Column("next_child", sa.Integer, nullable=False, server_default="0"),
    )
    op.add_column("planning_nodes", sa.Column("answer", sa.Text, nullable=True))
    op.add_column(
        "planning_nodes", sa.Column("answered_at", sa.DateTime(timezone=True), nullable=True)
    )
    # a topic and a report have no status; a task keeps its own
    op.alter_column("planning_nodes", "status", nullable=True, server_default=None)

    _number_tasks(bind)
    _move_notes(bind)

    op.alter_column("planning_nodes", "number", nullable=False)
    op.create_unique_constraint(
        "uq_planning_nodes_number", "planning_nodes", ["project_id", "number"]
    )
    op.create_check_constraint(
        "ck_planning_nodes_kind", "planning_nodes", "kind IN ('topic', 'task', 'report')"
    )
    op.drop_column("planning_nodes", "position")
    op.drop_table("planning_notes")


def downgrade() -> None:
    """Back to the task table and the notes table. Lossy where the old schema has no place for a
    thing: a topic becomes a task (its title and body are kept), a document becomes a note, and
    the numbers are not kept (the tasks get their old order back as ``position``)."""
    bind = op.get_bind()

    op.create_table(
        "planning_notes",
        sa.Column("id", UUID, primary_key=True),
        sa.Column(
            "project_id",
            UUID,
            sa.ForeignKey("planning_projects.id", ondelete="CASCADE"),
            index=True,
        ),
        sa.Column("user_id", UUID, sa.ForeignKey("users.id", ondelete="CASCADE"), index=True),
        sa.Column("application", sa.String(40), nullable=False, server_default="planning"),
        sa.Column("kind", sa.String(10), nullable=False),
        sa.Column("text", sa.Text, nullable=False),
        sa.Column("answer", sa.Text),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("answered_at", sa.DateTime(timezone=True)),
    )
    reports = bind.execute(
        sa.text(
            "SELECT id, project_id, user_id, application, report_kind, title, body, answer, "
            "answered_at, created_at FROM planning_nodes WHERE kind = 'report'"
        )
    ).all()
    for (
        _,
        project_id,
        user_id,
        application,
        report_kind,
        title,
        body,
        answer,
        answered,
        created,
    ) in reports:
        kind = report_kind if report_kind in ("note", "question", "decision") else "note"
        bind.execute(
            sa.text(
                "INSERT INTO planning_notes (id, project_id, user_id, application, kind, text, "
                "answer, created_at, answered_at) VALUES (:id, :p, :u, :a, :k, :t, :answer, "
                ":created, :answered)"
            ),
            {
                "id": uuid.uuid4(),
                "p": project_id,
                "u": user_id,
                "a": application,
                "k": kind,
                "t": body or title,
                "answer": answer,
                "created": created,
                "answered": answered,
            },
        )
    bind.execute(sa.text("DELETE FROM planning_nodes WHERE kind = 'report'"))
    bind.execute(sa.text("UPDATE planning_nodes SET kind = 'task' WHERE kind = 'topic'"))
    bind.execute(sa.text("UPDATE planning_nodes SET status = 'todo' WHERE status IS NULL"))

    op.drop_constraint("ck_planning_nodes_kind", "planning_nodes", type_="check")
    op.drop_constraint("uq_planning_nodes_number", "planning_nodes", type_="unique")
    op.add_column(
        "planning_nodes", sa.Column("position", sa.Integer, nullable=False, server_default="0")
    )
    rows = bind.execute(
        sa.text("SELECT id, project_id, parent_id, number FROM planning_nodes")
    ).all()
    groups: dict[tuple[str, str | None], list[tuple[tuple[int, ...], str]]] = defaultdict(list)
    for node_id, project_id, parent_id, number in rows:
        key = (str(project_id), str(parent_id) if parent_id else None)
        groups[key].append((tuple(int(x) for x in number.split(".")), str(node_id)))
    for members in groups.values():
        for position, (_, node_id) in enumerate(sorted(members)):
            bind.execute(
                sa.text("UPDATE planning_nodes SET position = :p WHERE id = :id"),
                {"p": position, "id": node_id},
            )
    op.drop_column("planning_nodes", "number")
    op.drop_column("planning_nodes", "kind")
    op.drop_column("planning_nodes", "report_kind")
    op.drop_column("planning_nodes", "next_child")
    op.drop_column("planning_nodes", "answer")
    op.drop_column("planning_nodes", "answered_at")
    op.alter_column("planning_nodes", "status", nullable=False, server_default="todo")
    op.alter_column("planning_nodes", "body", new_column_name="description")
    op.rename_table("planning_nodes", "planning_tasks")

    op.add_column(
        "planning_projects",
        sa.Column("kind", sa.String(20), nullable=False, server_default="everyday"),
    )
    op.add_column(
        "planning_projects", sa.Column("goal", sa.Text, nullable=False, server_default="")
    )
    op.add_column(
        "planning_projects", sa.Column("current_state", sa.Text, nullable=False, server_default="")
    )
    op.add_column(
        "planning_projects", sa.Column("target_state", sa.Text, nullable=False, server_default="")
    )
    bind.execute(sa.text("UPDATE planning_projects SET goal = description"))
    op.drop_constraint("uq_planning_projects_title", "planning_projects", type_="unique")
    op.drop_column("planning_projects", "scope")
    op.drop_column("planning_projects", "next_child")
    op.drop_column("planning_projects", "description")


def _make_titles_unique(bind: sa.Connection) -> None:
    rows = bind.execute(
        sa.text(
            "SELECT id, user_id, application, title FROM planning_projects ORDER BY created_at, id"
        )
    ).all()
    seen: dict[tuple[str, str, str], int] = defaultdict(int)
    for project_id, user_id, application, title in rows:
        key = (str(user_id), application, title)
        seen[key] += 1
        if seen[key] > 1:
            bind.execute(
                sa.text("UPDATE planning_projects SET title = :t WHERE id = :id"),
                {"t": f"{title[:190]} ({seen[key]})", "id": project_id},
            )


def _number_tasks(bind: sa.Connection) -> None:
    """Number the existing tasks: top level 1, 2, ...; below a task 1.1, 1.2 ... in the order of
    the old ``position`` and creation within each parent."""
    rows = bind.execute(
        sa.text(
            "SELECT id, project_id, parent_id FROM planning_nodes "
            "ORDER BY project_id, position, created_at, id"
        )
    ).all()
    children: dict[tuple[str, str | None], list[str]] = defaultdict(list)
    for node_id, project_id, parent_id in rows:
        children[(str(project_id), str(parent_id) if parent_id else None)].append(str(node_id))
    projects = [str(r[0]) for r in bind.execute(sa.text("SELECT id FROM planning_projects"))]
    for project_id in projects:
        _number_children(bind, project_id, None, "", children)


def _number_children(
    bind: sa.Connection,
    project_id: str,
    parent_id: str | None,
    prefix: str,
    children: dict[tuple[str, str | None], list[str]],
) -> None:
    kids = children.get((project_id, parent_id), [])
    for index, node_id in enumerate(kids, start=1):
        number = f"{prefix}{index}"
        bind.execute(
            sa.text("UPDATE planning_nodes SET number = :n WHERE id = :id"),
            {"n": number, "id": node_id},
        )
        _number_children(bind, project_id, node_id, f"{number}.", children)
    if parent_id is None:
        bind.execute(
            sa.text("UPDATE planning_projects SET next_child = :n WHERE id = :id"),
            {"n": len(kids), "id": project_id},
        )
    else:
        bind.execute(
            sa.text("UPDATE planning_nodes SET next_child = :n WHERE id = :id"),
            {"n": len(kids), "id": parent_id},
        )


def _move_notes(bind: sa.Connection) -> None:
    """Every note becomes a top-level report of its project, numbered after the topics and tasks
    there, with its answer. The rows are moved, not dropped with the table."""
    notes = bind.execute(
        sa.text(
            "SELECT id, project_id, user_id, application, kind, text, answer, created_at, "
            "answered_at FROM planning_notes ORDER BY project_id, created_at, id"
        )
    ).all()
    for note in notes:
        _, project_id, user_id, application, kind, text, answer, created, answered = note
        number = bind.execute(
            sa.text(
                "UPDATE planning_projects SET next_child = next_child + 1 "
                "WHERE id = :p RETURNING next_child"
            ),
            {"p": project_id},
        ).scalar_one()
        bind.execute(
            sa.text(
                "INSERT INTO planning_nodes (id, project_id, user_id, application, number, "
                "kind, report_kind, title, body, status, depends_on, next_child, answer, "
                "answered_at, revision, created_at, updated_at) VALUES (:id, :p, :u, :a, :n, "
                "'report', :rk, :title, :body, NULL, '[]', 0, :answer, :answered, 1, :created, "
                ":created)"
            ),
            {
                "id": uuid.uuid4(),
                "p": project_id,
                "u": user_id,
                "a": application,
                "n": str(number),
                "rk": kind,
                "title": text[:200],
                "body": text,
                "answer": answer,
                "answered": answered,
                "created": created,
            },
        )
