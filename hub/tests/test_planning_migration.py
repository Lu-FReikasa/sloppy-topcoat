"""Migration 0071 (Projekte): the rows of the old schema come out as numbered nodes and reports,
and the downgrade gives the task and note tables back (brief PPH-01-planning-app; rework
2026-10-27). The session's database is brought back to head at the end."""

import uuid

from alembic import command
from sqlalchemy import create_engine, text

from tests.conftest import TEST_DB_URL, alembic_config
from topcoat_hub.models import DEV_ADMIN_ID


def _seed_old_schema(engine, project_id: uuid.UUID, title: str) -> None:  # type: ignore[no-untyped-def]
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO planning_projects (id, user_id, application, title, kind, goal,"
                " current_state, target_state, status, revision) VALUES (:id, :u, 'planning',"
                " :title, 'development', 'Ship it', 'v1', 'v2', 'active', 3)"
            ),
            {"id": project_id, "u": DEV_ADMIN_ID, "title": title},
        )
        rows = [
            ("a", None, 1, "Repo", "", "todo"),
            ("b", "a", 0, "Check", "look at it", "done"),
            ("c", None, 2, "Live", "", "in_progress"),
        ]
        ids = {name: uuid.uuid4() for name, *_ in rows}
        for name, parent, position, title, description, status in rows:
            conn.execute(
                text(
                    "INSERT INTO planning_tasks (id, project_id, user_id, application,"
                    " parent_id, position, title, description, status, depends_on, revision)"
                    " VALUES (:id, :p, :u, 'planning', :parent, :pos, :title, :desc, :status,"
                    " '[]', 1)"
                ),
                {
                    "id": ids[name],
                    "p": project_id,
                    "u": DEV_ADMIN_ID,
                    "parent": ids[parent] if parent else None,
                    "pos": position,
                    "title": title,
                    "desc": description,
                    "status": status,
                },
            )
        conn.execute(
            text(
                "INSERT INTO planning_notes (id, project_id, user_id, application, kind, text,"
                " answer) VALUES (:id, :p, :u, 'planning', 'question', 'Which node?', 'node-amd')"
            ),
            {"id": uuid.uuid4(), "p": project_id, "u": DEV_ADMIN_ID},
        )


def test_0071_numbers_the_tree_and_moves_notes_to_reports_and_back(
    migrated_db: str,
) -> None:
    cfg = alembic_config()
    engine = create_engine(TEST_DB_URL)
    project_id = uuid.uuid4()
    title = f"Migration {project_id.hex[:8]}"  # unique: other tests use the same person
    command.downgrade(cfg, "0070")
    try:
        _seed_old_schema(engine, project_id, title)
        command.upgrade(cfg, "0071")
        with engine.connect() as conn:
            project = conn.execute(
                text(
                    "SELECT title, description, scope, next_child FROM planning_projects"
                    " WHERE id = :id"
                ),
                {"id": project_id},
            ).one()
            assert project.title == title
            assert project.description == "Ship it\n\nv1\n\nv2"
            assert project.scope == {}
            assert project.next_child == 3  # Repo, Live and the moved note
            nodes = conn.execute(
                text(
                    "SELECT number, kind, report_kind, title, status FROM planning_nodes"
                    " WHERE project_id = :id ORDER BY number"
                ),
                {"id": project_id},
            ).all()
            assert [(n.number, n.kind, n.title) for n in nodes] == [
                ("1", "task", "Repo"),
                ("1.1", "task", "Check"),
                ("2", "task", "Live"),
                ("3", "report", "Which node?"),
            ]
            assert nodes[3].report_kind == "question"
            assert nodes[1].status == "done" and nodes[3].status is None
        command.downgrade(cfg, "0070")
        with engine.connect() as conn:
            tasks = conn.execute(
                text(
                    "SELECT title, position FROM planning_tasks WHERE project_id = :id"
                    " ORDER BY title"
                ),
                {"id": project_id},
            ).all()
            assert [t.title for t in tasks] == ["Check", "Live", "Repo"]
            note = conn.execute(
                text("SELECT kind, text, answer FROM planning_notes WHERE project_id = :id"),
                {"id": project_id},
            ).one()
            assert (note.kind, note.text, note.answer) == ("question", "Which node?", "node-amd")
    finally:
        with engine.begin() as conn:
            conn.execute(text("DELETE FROM planning_projects WHERE id = :id"), {"id": project_id})
        command.upgrade(cfg, "head")
        engine.dispose()
