"""Migration 0085: the columns of a message and the table of chains (PPH-05, 05a).

The reports that exist get their type, the requests of the old flow become ``project.request``
messages with a state and a payload, and the downgrade takes everything away again. The session's
database is brought back to head at the end."""

import uuid

from alembic import command
from sqlalchemy import create_engine, inspect, text

from tests.conftest import TEST_DB_URL, alembic_config
from topcoat_hub.models import DEV_ADMIN_ID

NEW = {
    "message_type",
    "source_app",
    "target_app",
    "chain_id",
    "in_reply_to",
    "round",
    "msg_state",
    "task_id",
    "payload",
    "schema_version",
    "trust",
    "stats",
    "verification",
}


def _seed(engine) -> dict[str, uuid.UUID]:  # type: ignore[no-untyped-def]
    ids = {
        k: uuid.uuid4()
        for k in ("project", "note", "fact", "awaiting", "sent", "queued", "no", "task")
    }
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO projects (id, user_id, application, title, scope)"
                " VALUES (:id, :u, 'project', 'Msg', '{}')"
            ),
            {"id": ids["project"], "u": DEV_ADMIN_ID},
        )
        conn.execute(
            text("INSERT INTO agent_tasks (id, user_id, prompt, title) VALUES (:id, :u, 'p', 't')"),
            {"id": ids["task"], "u": DEV_ADMIN_ID},
        )
        rows = [
            ("note", "1", "note", None, None, None, "A note", ""),
            ("fact", "2", "fact", None, None, None, "A fact", ""),
            (
                "awaiting",
                "3",
                "request",
                "research",
                "awaiting",
                None,
                "Ask about plants",
                "Which ones?",
            ),
            ("sent", "4", "request", "dev", "sent", None, "A branch", ""),
            ("queued", "5", "request", "admin", "sent", ids["task"], "State of host", "Disk?"),
            ("no", "6", "request", "research", "declined", None, "Declined one", ""),
        ]
        for key, number, kind, target, state, task, title, body in rows:
            conn.execute(
                text(
                    "INSERT INTO project_nodes (id, project_id, user_id, application, number, kind,"
                    " report_kind, title, body, request_target, request_action, request_state,"
                    " request_task_id) VALUES (:id, :p, :u, 'project', :n, 'report', :rk, :t, :b,"
                    " :target, :action, :state, :task)"
                ),
                {
                    "id": ids[key], "p": ids["project"], "u": DEV_ADMIN_ID, "n": number, "rk": kind,
                    "t": title, "b": body, "target": target,
                    "action": "request_report" if target else None, "state": state, "task": task,
                },
            )  # fmt: skip
    return ids


def test_0085_gives_the_reports_their_type_and_turns_requests_into_messages(
    migrated_db: str,
) -> None:
    cfg = alembic_config()
    engine = create_engine(TEST_DB_URL)
    try:
        command.downgrade(cfg, "0084")
        inspector = inspect(engine)
        assert "project_chains" not in inspector.get_table_names()
        assert NEW.isdisjoint(c["name"] for c in inspector.get_columns("project_nodes"))
        ids = _seed(engine)

        command.upgrade(cfg, "0085")
        inspector = inspect(engine)
        assert "project_chains" in inspector.get_table_names()
        assert {c["name"] for c in inspector.get_columns("project_nodes")} >= NEW
        with engine.connect() as conn:
            got = {
                row.id: row
                for row in conn.execute(
                    text(
                        "SELECT id, message_type, source_app, target_app, msg_state, trust,"
                        " task_id, payload, round FROM project_nodes WHERE project_id = :p"
                    ),
                    {"p": ids["project"]},
                )
            }
        note, fact = got[ids["note"]], got[ids["fact"]]
        assert (note.message_type, note.source_app, note.target_app, note.trust) == (
            "project.note", "project", "project", "own",
        )  # fmt: skip
        assert fact.message_type == "project.fact" and fact.msg_state == "accepted"
        awaiting, sent, queued, no = (got[ids[k]] for k in ("awaiting", "sent", "queued", "no"))
        assert awaiting.message_type == "project.request" and awaiting.target_app == "research"
        assert awaiting.msg_state == "awaiting_click"
        assert awaiting.payload["topic"] == "Ask about plants"
        assert awaiting.payload["questions"] == ["Which ones?"]
        assert awaiting.payload["action"] == "request_report"
        assert sent.msg_state == "sent" and sent.target_app == "dev"
        assert sent.payload["questions"] == []
        assert queued.msg_state == "queued" and queued.task_id == ids["task"]
        assert no.msg_state == "rejected"
        assert all(r.round == 1 for r in got.values())

        command.downgrade(cfg, "0084")
        inspector = inspect(engine)
        assert "project_chains" not in inspector.get_table_names()
        assert NEW.isdisjoint(c["name"] for c in inspector.get_columns("project_nodes"))
        with engine.begin() as conn:  # the old columns kept what they held
            states = {
                r.request_state
                for r in conn.execute(
                    text("SELECT request_state FROM project_nodes WHERE report_kind = 'request'")
                )
            }
            conn.execute(text("DELETE FROM projects WHERE id = :p"), {"p": ids["project"]})
            conn.execute(text("DELETE FROM agent_tasks WHERE id = :t"), {"t": ids["task"]})
        assert states == {"awaiting", "sent", "declined"}
    finally:
        command.upgrade(cfg, "head")
        engine.dispose()
