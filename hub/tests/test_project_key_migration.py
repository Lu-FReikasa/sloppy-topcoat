"""Migration 0078: the application key `planning` becomes `project` (PPH-04, piece 04.0).

The rows of the old key come out under the new one, the tables keep the constraints and indexes
the models expect, and the downgrade gives the old names back. The session's database is brought
back to head at the end."""

import json
import uuid

from alembic import command
from sqlalchemy import create_engine, inspect, text

from tests.conftest import TEST_DB_URL, alembic_config
from topcoat_hub.models import DEV_ADMIN_ID, Base

NEW_TABLES = ("projects", "project_nodes", "project_changes")


def _seed_old_key(engine, tag: str) -> dict[str, uuid.UUID]:  # type: ignore[no-untyped-def]
    ids = {name: uuid.uuid4() for name in ("project", "node", "change", "conversation", "tag")}
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO planning_projects (id, user_id, application, title, scope)"
                " VALUES (:id, :u, 'planning', :title, '{}')"
            ),
            {"id": ids["project"], "u": DEV_ADMIN_ID, "title": f"Key {tag}"},
        )
        conn.execute(
            text(
                "INSERT INTO planning_nodes (id, project_id, user_id, application, number, kind,"
                " title) VALUES (:id, :p, :u, 'planning', '1', 'task', 'Node')"
            ),
            {"id": ids["node"], "p": ids["project"], "u": DEV_ADMIN_ID},
        )
        conn.execute(
            text(
                "INSERT INTO planning_changes (id, user_id, capability, input, preview,"
                " content_hash) VALUES (:id, :u, :cap, '{}', '{}', :h)"
            ),
            {"id": ids["change"], "u": DEV_ADMIN_ID, "cap": "planning.project.update", "h": tag},
        )
        conn.execute(
            text(
                "INSERT INTO planning_changes (id, user_id, capability, input, preview,"
                " content_hash) VALUES (:id, :u, 'planning.node.move', '{}', '{}', :h)"
            ),
            {"id": uuid.uuid4(), "u": DEV_ADMIN_ID, "h": f"{tag}-2"},
        )
        conn.execute(
            text(
                "INSERT INTO conversations (id, user_id, application) VALUES (:id, :u, 'planning')"
            ),
            {"id": ids["conversation"], "u": DEV_ADMIN_ID},
        )
        # the root of the tag tree, and a tag of the person that already has the new name
        conn.execute(
            text(
                "INSERT INTO memory_tags (id, user_id, name, responsible, created_by)"
                " VALUES (:id, :u, 'planning', 'planning', 'seed')"
            ),
            {"id": ids["tag"], "u": DEV_ADMIN_ID},
        )
        conn.execute(
            text(
                "INSERT INTO memory_tags (id, user_id, parent_id, name, created_by)"
                " VALUES (:id, :u, :parent, 'project', 'user')"
            ),
            {"id": uuid.uuid4(), "u": DEV_ADMIN_ID, "parent": ids["tag"]},
        )
        conn.execute(
            text(
                "INSERT INTO ui_templates (key, title, title_key, modules, open, agent, builtin)"
                " VALUES ('planning', 'Projects', 'workspace.planning', :modules, :open, :agent,"
                " true)"
            ),
            {
                "modules": json.dumps(["page.planning", "page.memory"]),
                "open": json.dumps(["page.planning"]),
                "agent": json.dumps({"tool_scope": {"groups": ["planning"]}}),
            },
        )
        conn.execute(
            text(
                "INSERT INTO ui_templates (key, title, modules, agent, builtin)"
                " VALUES ('chat', 'Chat', :modules, '{}', true)"
            ),
            {"modules": json.dumps(["page.chat", "page.chat.planning"])},
        )
        prefs = {"desktop": {"open": ["chat", "planning"], "views": {"planning": "page.planning"}}}
        conn.execute(
            text("UPDATE users SET ui_prefs = CAST(:p AS jsonb) WHERE id = :u"),
            {"p": json.dumps(prefs), "u": DEV_ADMIN_ID},
        )
    return ids


def _reset(engine) -> None:  # type: ignore[no-untyped-def]
    """Other tests of the same session leave templates and tags behind: start from nothing."""
    with engine.begin() as conn:
        conn.execute(text("DELETE FROM ui_templates WHERE key IN ('planning', 'project', 'chat')"))
        conn.execute(text("DELETE FROM memory_tags WHERE user_id = :u"), {"u": DEV_ADMIN_ID})


def _application(conn, table: str, row_id: uuid.UUID) -> str:  # type: ignore[no-untyped-def]
    return conn.scalar(
        text(f"SELECT application FROM {table} WHERE id = :id"),  # noqa: S608
        {"id": row_id},
    )


def _clean(engine, tag: str) -> None:  # type: ignore[no-untyped-def]
    """Remove what the test seeded; the database is at head, so the tables have their new names."""
    with engine.begin() as conn:
        conn.execute(text("DELETE FROM projects WHERE title = :t"), {"t": f"Key {tag}"})
        conn.execute(
            text("DELETE FROM project_changes WHERE content_hash LIKE :h"), {"h": f"{tag}%"}
        )
        conn.execute(text("DELETE FROM ui_templates WHERE key IN ('planning', 'project', 'chat')"))
        conn.execute(text("DELETE FROM memory_tags WHERE user_id = :u"), {"u": DEV_ADMIN_ID})
        conn.execute(text("DELETE FROM conversations WHERE user_id = :u"), {"u": DEV_ADMIN_ID})
        conn.execute(text("UPDATE users SET ui_prefs = '{}' WHERE id = :u"), {"u": DEV_ADMIN_ID})


def test_0078_moves_everything_to_the_new_key_and_back(migrated_db: str) -> None:
    cfg = alembic_config()
    engine = create_engine(TEST_DB_URL)
    tag = uuid.uuid4().hex[:8]
    command.downgrade(cfg, "0077")
    try:
        _reset(engine)
        ids = _seed_old_key(engine, tag)
        command.upgrade(cfg, "0078")
        with engine.connect() as conn:
            tables = set(inspect(conn).get_table_names())
            assert set(NEW_TABLES) <= tables
            assert not {"planning_projects", "planning_nodes", "planning_changes"} & tables
            assert _application(conn, "projects", ids["project"]) == "project"
            assert _application(conn, "project_nodes", ids["node"]) == "project"
            assert _application(conn, "conversations", ids["conversation"]) == "project"
            caps = {
                r[0]
                for r in conn.execute(
                    text("SELECT capability FROM project_changes WHERE content_hash LIKE :h"),
                    {"h": f"{tag}%"},
                )
            }
            assert caps == {"project.update", "project.node.move"}
            default = conn.scalar(
                text(
                    "SELECT column_default FROM information_schema.columns WHERE table_name ="
                    " 'projects' AND column_name = 'application'"
                )
            )
            assert "project" in default and "planning" not in default
            root = conn.execute(
                text("SELECT name, responsible FROM memory_tags WHERE id = :id"),
                {"id": ids["tag"]},
            ).one()
            assert (root.name, root.responsible) == ("project", "project")
            moved = conn.scalar(
                text("SELECT name FROM memory_tags WHERE user_id = :u AND parent_id = :p"),
                {"u": DEV_ADMIN_ID, "p": ids["tag"]},
            )
            assert moved.startswith("x-")  # the person's own tag made room for the root
            template = conn.execute(
                text(
                    "SELECT title_key, modules, open, agent FROM ui_templates WHERE key = 'project'"
                )
            ).one()
            assert template.title_key == "workspace.project"
            assert template.modules == ["page.project", "page.memory"]
            assert template.open == ["page.project"]
            assert template.agent == {"tool_scope": {"groups": ["project"]}}
            assert (
                conn.scalar(text("SELECT count(*) FROM ui_templates WHERE key = 'planning'")) == 0
            )
            chat = conn.scalar(text("SELECT modules FROM ui_templates WHERE key = 'chat'"))
            assert chat == ["page.chat", "page.chat.project"]
            prefs = conn.scalar(
                text("SELECT ui_prefs FROM users WHERE id = :u"), {"u": DEV_ADMIN_ID}
            )
            assert prefs == {
                "desktop": {"open": ["chat", "project"], "views": {"project": "page.project"}}
            }
            # constraints and indexes carry the names the models expect
            insp = inspect(conn)
            for name in NEW_TABLES:
                table = Base.metadata.tables[name]
                # what later migrations added is not there yet at 0078
                want_indexes = {i.name for i in table.indexes} - {
                    "uq_projects_identifier",
                    "ix_project_nodes_message_type",
                    "ix_project_nodes_chain_id",
                    "ix_project_nodes_task_id",
                }
                have_indexes = {i["name"] for i in insp.get_indexes(name)}
                assert want_indexes <= have_indexes, (name, want_indexes - have_indexes)
                want_unique = {c.name for c in table.constraints if c.name and "uq_" in c.name}
                have_unique = {c["name"] for c in insp.get_unique_constraints(name)}
                assert want_unique <= have_unique, (name, want_unique - have_unique)
                want_checks = {c.name for c in table.constraints if c.name and "ck_" in c.name}
                want_checks -= {"ck_project_nodes_priority", "ck_project_nodes_percent"}
                have_checks = {c["name"] for c in insp.get_check_constraints(name)}
                assert want_checks <= have_checks, (name, want_checks - have_checks)
        command.downgrade(cfg, "0077")
        with engine.connect() as conn:
            tables = set(inspect(conn).get_table_names())
            assert {"planning_projects", "planning_nodes", "planning_changes"} <= tables
            assert not set(NEW_TABLES) & tables
            row = conn.execute(
                text("SELECT application FROM planning_nodes WHERE id = :id"), {"id": ids["node"]}
            ).one()
            assert row.application == "planning"
            assert (
                conn.scalar(
                    text("SELECT capability FROM planning_changes WHERE content_hash = :h"),
                    {"h": tag},
                )
                == "planning.project.update"
            )
            assert (
                conn.scalar(
                    text("SELECT capability FROM planning_changes WHERE content_hash = :h"),
                    {"h": f"{tag}-2"},
                )
                == "planning.node.move"
            )
            assert (
                conn.scalar(text("SELECT name FROM memory_tags WHERE id = :id"), {"id": ids["tag"]})
                == "planning"
            )
            assert (
                conn.scalar(text("SELECT count(*) FROM ui_templates WHERE key = 'planning'")) == 1
            )
    finally:
        command.upgrade(cfg, "head")
        _clean(engine, tag)
        engine.dispose()
