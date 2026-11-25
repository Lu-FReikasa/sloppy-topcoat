"""Migration 0086: the ``request_*`` columns go, and come back from the messages on a downgrade."""

import uuid

from alembic import command
from sqlalchemy import create_engine, inspect, text

from tests.conftest import TEST_DB_URL, alembic_config
from topcoat_hub.models import DEV_ADMIN_ID

OLD = {"request_target", "request_action", "request_state", "request_task_id"}


def test_0086_drops_the_old_columns_and_a_downgrade_restores_them(migrated_db: str) -> None:
    cfg = alembic_config()
    engine = create_engine(TEST_DB_URL)
    project, node, other = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    try:
        assert OLD.isdisjoint(c["name"] for c in inspect(engine).get_columns("project_nodes"))
        with engine.begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO projects (id, user_id, application, title, scope)"
                    " VALUES (:id, :u, 'project', 'Req', '{}')"
                ),
                {"id": project, "u": DEV_ADMIN_ID},
            )
            for nid, number, state in ((node, "1", "awaiting_click"), (other, "2", "stopped")):
                conn.execute(
                    text(
                        "INSERT INTO project_nodes (id, project_id, user_id, application, number,"
                        " kind, report_kind, title, message_type, source_app, target_app,"
                        " msg_state, payload) VALUES (:id, :p, :u, 'project', :n, 'report',"
                        " 'request', 'Ask', 'project.request', 'project', 'admin', :s,"
                        ' \'{"topic": "Ask"}\')'
                    ),
                    {"id": nid, "p": project, "u": DEV_ADMIN_ID, "n": number, "s": state},
                )
        command.downgrade(cfg, "0085")
        assert {c["name"] for c in inspect(engine).get_columns("project_nodes")} >= OLD
        with engine.connect() as conn:
            rows = {
                r.id: r
                for r in conn.execute(
                    text(
                        "SELECT id, request_target, request_action, request_state"
                        " FROM project_nodes WHERE project_id = :p"
                    ),
                    {"p": project},
                )
            }
        assert (rows[node].request_target, rows[node].request_action) == ("admin", "read_released")
        assert rows[node].request_state == "awaiting"
        assert rows[other].request_state == "declined"
        command.upgrade(cfg, "0086")
        assert OLD.isdisjoint(c["name"] for c in inspect(engine).get_columns("project_nodes"))
        with engine.begin() as conn:
            conn.execute(text("DELETE FROM projects WHERE id = :p"), {"p": project})
    finally:
        command.upgrade(cfg, "head")
        engine.dispose()


def test_0087_adds_the_capabilities_column_and_removes_it_again(migrated_db: str) -> None:
    cfg = alembic_config()
    engine = create_engine(TEST_DB_URL)
    try:
        assert "capabilities" in {c["name"] for c in inspect(engine).get_columns("agent_tasks")}
        command.downgrade(cfg, "0086")
        assert "capabilities" not in {c["name"] for c in inspect(engine).get_columns("agent_tasks")}
    finally:
        command.upgrade(cfg, "head")
        engine.dispose()


def test_0088_creates_the_mandates_table_with_the_branch_rule(migrated_db: str) -> None:
    cfg = alembic_config()
    engine = create_engine(TEST_DB_URL)
    try:
        assert "project_mandates" in inspect(engine).get_table_names()
        command.downgrade(cfg, "0087")
        assert "project_mandates" not in inspect(engine).get_table_names()
    finally:
        command.upgrade(cfg, "head")
        engine.dispose()
