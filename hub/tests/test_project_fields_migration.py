"""Migration 0080: the old ``depends_on`` lists become ``blocked_by`` relations, the column goes,
and
the downgrade rebuilds the lists from the open relations inside one project. The session's database
is brought back to head at the end."""

import json
import uuid

from alembic import command
from sqlalchemy import create_engine, text

from tests.conftest import TEST_DB_URL, alembic_config
from topcoat_hub.models import DEV_ADMIN_ID


def test_0080_turns_depends_on_into_relations_and_back(migrated_db: str) -> None:
    cfg = alembic_config()
    engine = create_engine(TEST_DB_URL)
    p1, p2 = uuid.uuid4(), uuid.uuid4()
    a, b, c, ghost = (uuid.uuid4() for _ in range(4))
    tag = p1.hex[:8]
    command.downgrade(cfg, "0079")
    try:
        with engine.begin() as conn:
            for pid, title in ((p1, f"M1 {tag}"), (p2, f"M2 {tag}")):
                conn.execute(
                    text(
                        "INSERT INTO projects (id, user_id, application, title, scope, status)"
                        " VALUES (:id, :u, 'project', :t, '{}', 'active')"
                    ),
                    {"id": pid, "u": DEV_ADMIN_ID, "t": title},
                )
            for nid, pid, number, depends in (
                (a, p1, "1", []),
                (b, p1, "2", [str(a), str(ghost)]),  # one dependency that does not exist
                (c, p2, "1", [str(a)]),  # across projects: old lists never had it, new ones may
            ):
                conn.execute(
                    text(
                        "INSERT INTO project_nodes (id, project_id, user_id, application, number,"
                        " kind, title, status, depends_on)"
                        " VALUES (:id, :p, :u, 'project', :n, 'task', :n, 'todo',"
                        " CAST(:d AS jsonb))"
                    ),
                    {"id": nid, "p": pid, "u": DEV_ADMIN_ID, "n": number, "d": json.dumps(depends)},
                )
        command.upgrade(cfg, "0080")
        with engine.connect() as conn:
            rows = conn.execute(
                text(
                    "SELECT node_id, other_id, kind, valid_to FROM project_relations"
                    " WHERE node_id IN (:b, :c)"
                ),
                {"b": b, "c": c},
            ).all()
            assert sorted((r.node_id == b, r.other_id == a, r.kind, r.valid_to) for r in rows) == [
                (False, True, "blocked_by", None),
                (True, True, "blocked_by", None),
            ]  # the ghost is dropped; the other two stand
            columns = {
                r[0]
                for r in conn.execute(
                    text(
                        "SELECT column_name FROM information_schema.columns"
                        " WHERE table_name = 'project_nodes'"
                    )
                )
            }
            assert "depends_on" not in columns and {"due_at", "priority", "is_milestone"} <= columns
        command.downgrade(cfg, "0079")
        with engine.connect() as conn:
            lists = dict(
                conn.execute(
                    text("SELECT id, depends_on FROM project_nodes WHERE id IN (:a, :b, :c)"),
                    {"a": a, "b": b, "c": c},
                ).all()
            )
            assert lists[a] == [] and lists[b] == [str(a)]
            assert lists[c] == []  # a dependency across projects does not fit the old column
    finally:
        command.upgrade(cfg, "head")
        with engine.begin() as conn:
            conn.execute(text("DELETE FROM projects WHERE id IN (:a, :b)"), {"a": p1, "b": p2})
            conn.execute(
                text("DELETE FROM project_events WHERE project_id IN (:a, :b)"), {"a": p1, "b": p2}
            )
        engine.dispose()
