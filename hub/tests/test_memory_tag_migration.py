"""Migration 0066 (D74) on a database that already holds notes: the old tags and the person's
deny / allow rules become the tag tree, every note gets a tag, a fact about a place leaves the
core — and down again, the notes get their list of tag names back."""

import json
import uuid

from alembic import command
from sqlalchemy import create_engine, text

from tests.conftest import alembic_config

U1, U2 = str(uuid.uuid4()), str(uuid.uuid4())
N_HEALTH, N_PLAIN, N_OPS, N_WORK = (str(uuid.uuid4()) for _ in range(4))


def insert_note(conn, item: str, user: str, kind: str, app: str | None, tags: list[str]) -> None:
    conn.execute(
        text(
            "INSERT INTO memory_items (id, user_id, text, status, confidence, evidence, "
            "source_type, source_ref, version, kind, application, tags) VALUES (:i, :u, :t, "
            "'active', 0.9, 'doc', 'chat', '{}', 1, :k, :a, CAST(:tags AS jsonb))"
        ),
        {
            "i": item,
            "u": user,
            "t": f"note {item[:4]}",
            "k": kind,
            "a": app,
            "tags": json.dumps(tags),
        },
    )


def test_0066_turns_tags_and_rules_into_the_tree_and_back(clean_db: str) -> None:
    cfg = alembic_config()
    command.downgrade(cfg, "0065")
    engine = create_engine(clean_db)
    try:
        with engine.begin() as conn:
            for uid, name in ((U1, "mig-1"), (U2, "mig-2")):
                conn.execute(
                    text("INSERT INTO users (id, name, role) VALUES (:i, :n, 'user')"),
                    {"i": uid, "n": name},
                )
            conn.execute(
                text(
                    "INSERT INTO user_settings (user_id, memory_tag_rules) VALUES "
                    "(:u, CAST(:r AS jsonb))"
                ),
                {
                    "u": U1,
                    "r": json.dumps({"deny": ["health", "money"], "allow": {"admin": ["money"]}}),
                },
            )
            insert_note(conn, N_HEALTH, U1, "person", None, ["health"])
            insert_note(conn, N_PLAIN, U1, "preference", None, [])
            insert_note(conn, N_OPS, U1, "operational", None, ["Proxmox"])  # in the old shared set
            insert_note(conn, N_WORK, U2, "operational", "dev", ["ci", "bad tag!"])

        command.upgrade(cfg, "0066")
        with engine.connect() as conn:
            tags = {
                (str(r.user_id), r.name): r for r in conn.execute(text("SELECT * FROM memory_tags"))
            }
            by_id = {r.id: r for r in tags.values()}
            assert (U1, "infrastructure") in tags and (U2, "infrastructure") in tags
            # the rules: health stays sensitive, money is a sensitive tag below person, released
            # for Administration as the allow list said; finance and private were not denied by
            # this person any more, so they are ordinary tags
            assert tags[(U1, "health")].sensitive is True
            money = tags[(U1, "money")]
            assert money.sensitive is True and money.released == ["admin"]
            assert by_id[money.parent_id].name == "person"
            assert tags[(U1, "finance")].sensitive is None
            assert tags[(U2, "finance")].sensitive is True  # no rules: the old defaults
            links = {
                str(r.item_id): by_id[r.tag_id].name
                for r in conn.execute(text("SELECT * FROM memory_item_tags"))
            }
            assert links[N_HEALTH] == "health"
            assert links[N_PLAIN] == "preferences"  # no tag: the tag of its kind
            assert links[N_WORK] == "ci"  # the unusable "bad tag!" is dropped
            assert by_id[tags[(U2, "ci")].parent_id].name == "development"
            # a fact about a place left the core for Chat, with its tag below Chat's field
            ops = conn.execute(
                text("SELECT application FROM memory_items WHERE id = :i"), {"i": N_OPS}
            ).scalar()
            assert ops == "chat" and links[N_OPS] == "proxmox"

        command.downgrade(cfg, "0065")
        with engine.connect() as conn:
            back = {
                str(r.id): r.tags for r in conn.execute(text("SELECT id, tags FROM memory_items"))
            }
            assert back[N_HEALTH] == ["health"] and back[N_WORK] == ["ci"]
            assert back[N_PLAIN] == ["preferences"]
    finally:
        command.upgrade(cfg, "head")
        engine.dispose()
