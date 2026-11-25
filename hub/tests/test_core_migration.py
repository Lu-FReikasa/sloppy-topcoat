"""Migration 0023 (E3-06) on a database that already holds data: up, down, up — nothing is lost."""

import json
import uuid

from alembic import command
from sqlalchemy import create_engine, text

from tests.conftest import alembic_config

U1, U2, U3 = (str(uuid.uuid4()) for _ in range(3))
CONV1, CONV2, CONV3 = (str(uuid.uuid4()) for _ in range(3))
FACT = str(uuid.uuid4())


def scalar(conn, sql: str, **params):
    return conn.execute(text(sql), params).scalar()


def test_0023_up_down_up_keeps_prompts_conversations_and_notes(clean_db: str) -> None:
    cfg = alembic_config()
    command.downgrade(cfg, "0022")
    engine = create_engine(clean_db)
    with engine.begin() as conn:
        for uid, name in ((U1, "mig-1"), (U2, "mig-2"), (U3, "mig-3")):
            conn.execute(
                text("INSERT INTO users (id, name, role) VALUES (:i, :n, 'user')"),
                {"i": uid, "n": name},
            )
        conn.execute(
            text(
                "INSERT INTO user_settings (user_id, system_prompt, address) VALUES "
                "(:a, 'Antworte immer in Reimen.', 'sie'), (:b, NULL, NULL), (:c, '   ', 'du')"
            ),
            {"a": U1, "b": U2, "c": U3},
        )
        for cid, uid in ((CONV1, U1), (CONV2, U1), (CONV3, U2)):
            conn.execute(
                text("INSERT INTO conversations (id, user_id, title) VALUES (:c, :u, 't')"),
                {"c": cid, "u": uid},
            )
        conn.execute(
            text(
                "INSERT INTO messages (conversation_id, user_id, role, content) "
                "VALUES (:c, :u, 'user', 'Hallo')"
            ),
            {"c": CONV1, "u": U1},
        )
        conn.execute(
            text(
                "INSERT INTO memory_items (id, user_id, text, status, confidence, evidence, "
                "source_type, source_ref, version, kind) VALUES (:i, :u, 'Wohnt in Hamburg', "
                "'active', 0.9, 'doc', 'chat', '{}', 1, 'person')"
            ),
            {"i": FACT, "u": U1},
        )

    command.upgrade(cfg, "0023")
    with engine.connect() as conn:
        # the old system prompt is the personal persona "Eigener Text", the user's default and the
        # persona of every conversation they had: those keep their prompt
        persona = conn.execute(
            text(
                "SELECT id, name, key, scope, version, components FROM personas WHERE user_id = :u"
            ),
            {"u": U1},
        ).one()
        assert (persona.name, persona.key, persona.scope, persona.version) == (
            "Eigener Text",
            "legacy",
            "user",
            1,
        )
        assert persona.components == {"free_text": "Antworte immer in Reimen."}
        assert str(
            scalar(conn, "SELECT persona_id FROM user_settings WHERE user_id = :u", u=U1)
        ) == str(persona.id)
        refs = conn.execute(
            text("SELECT id, persona_ref FROM conversations WHERE user_id = :u"), {"u": U1}
        ).all()
        assert {str(r.persona_ref) for r in refs} == {str(persona.id)} and len(refs) == 2
        # a user without a prompt gets no persona, and their conversations none
        assert (
            scalar(conn, "SELECT count(*) FROM personas WHERE user_id IN (:a, :b)", a=U2, b=U3) == 0
        )
        assert scalar(conn, "SELECT persona_ref FROM conversations WHERE id = :c", c=CONV3) is None
        # form of address and language rule: notes of the user's own, with a first version
        notes = conn.execute(
            text(
                "SELECT kind, text, core, scope, source_ref->>'legacy' AS legacy, version "
                "FROM memory_items WHERE user_id = :u AND kind IN ('rule', 'style') ORDER BY kind"
            ),
            {"u": U1},
        ).all()
        assert [(n.kind, n.legacy) for n in notes] == [("rule", "language"), ("style", "address")]
        assert all(n.core and n.scope == "user" and n.version == 1 for n in notes)
        assert 'formally with "Sie"' in notes[1].text
        assert (
            scalar(
                conn,
                "SELECT count(*) FROM memory_versions v JOIN memory_items i ON i.id = v.item_id "
                "WHERE i.user_id = :u AND i.kind IN ('rule', 'style')",
                u=U1,
            )
            == 2
        )
        # no rule note for the system prompt any more: the persona carries it
        assert (
            scalar(
                conn,
                "SELECT count(*) FROM memory_items WHERE source_ref->>'legacy' = 'system_prompt'",
            )
            == 0
        )
        # the ordinary fact is untouched and the two pseudo users exist
        assert conn.execute(
            text("SELECT text, core, scope FROM memory_items WHERE id = :i"), {"i": FACT}
        ).one() == (
            "Wohnt in Hamburg",
            False,
            "user",
        )
        assert (
            scalar(conn, "SELECT count(*) FROM users WHERE name IN ('household', 'system-rules')")
            == 2
        )
        # the settings themselves are as they were: the old API keeps working
        assert (
            scalar(conn, "SELECT system_prompt FROM user_settings WHERE user_id = :u", u=U1)
            == "Antworte immer in Reimen."
        )

    command.downgrade(cfg, "0022")
    with engine.connect() as conn:
        assert (
            scalar(conn, "SELECT system_prompt FROM user_settings WHERE user_id = :u", u=U1)
            == "Antworte immer in Reimen."
        )
        assert scalar(conn, "SELECT address FROM user_settings WHERE user_id = :u", u=U1) == "sie"
        assert scalar(conn, "SELECT count(*) FROM conversations") == 3
        assert scalar(conn, "SELECT count(*) FROM messages") == 1
        assert (
            scalar(conn, "SELECT text FROM memory_items WHERE id = :i", i=FACT)
            == "Wohnt in Hamburg"
        )
        assert (
            scalar(
                conn,
                "SELECT count(*) FROM memory_items "
                "WHERE kind NOT IN ('person', 'preference', 'operational')",
            )
            == 0
        )
        tables = {
            r[0]
            for r in conn.execute(
                text("SELECT tablename FROM pg_tables WHERE schemaname = 'public'")
            )
        }
        assert not tables & {"personas", "prompt_snapshots", "memory_proposals"}

    command.upgrade(cfg, "head")  # and up again, on the same data
    with engine.connect() as conn:
        assert scalar(conn, "SELECT count(*) FROM personas WHERE user_id = :u", u=U1) == 1
        assert json.loads(
            json.dumps(scalar(conn, "SELECT components FROM personas WHERE user_id = :u", u=U1))
        ) == {"free_text": "Antworte immer in Reimen."}
    engine.dispose()
