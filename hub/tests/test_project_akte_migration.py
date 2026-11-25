"""Migration 0079 (the Akte): projects and nodes that exist get a ``baseline`` line each and a
snapshot, the chain of those lines verifies like any other, and the downgrade drops the tables.
The session's database is brought back to head at the end."""

import uuid

from alembic import command
from sqlalchemy import create_engine, text

from tests.conftest import TEST_DB_URL, alembic_config
from topcoat_hub.models import DEV_ADMIN_ID, ProjectEvent
from topcoat_hub.project_events import verify


def test_0079_writes_a_baseline_for_what_exists(migrated_db: str) -> None:
    cfg = alembic_config()
    engine = create_engine(TEST_DB_URL)
    project_id, node_id, child_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    title = f"Baseline {project_id.hex[:8]}"
    command.downgrade(cfg, "0078")
    try:
        with engine.begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO projects (id, user_id, application, title, scope, status)"
                    " VALUES (:id, :u, 'project', :t, '{}', 'active')"
                ),
                {"id": project_id, "u": DEV_ADMIN_ID, "t": title},
            )
            for nid, number, kind, parent, body, rev in (
                (node_id, "1", "topic", None, "first\nsecond\n", 3),
                (child_id, "1.1", "task", node_id, "", 1),
            ):
                conn.execute(
                    text(
                        "INSERT INTO project_nodes (id, project_id, user_id, application, number,"
                        " kind, parent_id, title, body, status, revision)"
                        " VALUES (:id, :p, :u, 'project', :n, :k, :parent, :title, :body,"
                        " :status, :rev)"
                    ),
                    {
                        "id": nid, "p": project_id, "u": DEV_ADMIN_ID, "n": number, "k": kind,
                        "parent": parent, "title": f"Node {number}", "body": body, "rev": rev,
                        "status": "todo" if kind == "task" else None,
                    },
                )  # fmt: skip
        command.upgrade(cfg, "0079")
        with engine.connect() as conn:
            rows = (
                conn.execute(
                    text("SELECT * FROM project_events WHERE project_id = :p ORDER BY seq"),
                    {"p": project_id},
                )
                .mappings()
                .all()
            )
            assert [r["kind"] for r in rows] == ["baseline"] * 3
            assert [r["seq"] for r in rows] == [1, 2, 3]
            assert rows[0]["node_id"] is None and rows[0]["project_title"] == title
            assert rows[1]["node_ref"] == f"{title} 1" and rows[1]["actor_kind"] == "system"
            assert rows[0]["prev_hash"] == "0" * 64 and rows[1]["prev_hash"] == rows[0]["hash"]
            events = [ProjectEvent(**dict(r)) for r in rows]
            assert verify(events) == []
            versions = (
                conn.execute(
                    text("SELECT node_id, rev, kind, data FROM project_node_versions ORDER BY rev"),
                )
                .mappings()
                .all()
            )
            mine = {v["node_id"]: v for v in versions if v["node_id"] in (node_id, child_id)}
            assert mine[node_id]["rev"] == 3 and mine[node_id]["kind"] == "snapshot"
            assert mine[node_id]["data"] == {"title": "Node 1", "body": "first\nsecond\n"}
            assert mine[child_id]["rev"] == 1
        command.downgrade(cfg, "0078")
        with engine.connect() as conn:
            tables = {
                r[0]
                for r in conn.execute(
                    text("SELECT tablename FROM pg_tables WHERE schemaname = 'public'")
                )
            }
            assert "project_events" not in tables and "project_node_versions" not in tables
    finally:
        command.upgrade(cfg, "head")
        with engine.begin() as conn:
            conn.execute(text("DELETE FROM projects WHERE id = :p"), {"p": project_id})
            conn.execute(
                text("DELETE FROM project_events WHERE project_id = :p"), {"p": project_id}
            )
        engine.dispose()
