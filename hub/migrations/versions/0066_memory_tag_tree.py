"""the tag tree of the memory, the core and the program memories (ARCHITECTURE D74)

Revision ID: 0066
Revises: 0065
Create Date: 2026-10-24

Tags were a list of words on each note, set by the person only, and served one purpose: the
"deny" and "allow" rules of a person hid notes tagged ``private``, ``health`` or ``finance`` from
the applications. Now every person has a tag tree (``memory_tags``) with a fixed top level, the
notes carry tags as rows (``memory_item_tags``), and a branch says who is responsible for its topic,
whether it is sensitive and for which programs a sensitive branch is released.

The upgrade keeps what there was: every person with notes gets the tree; each old tag becomes a tag
(below the top level of the program its notes are in); a tag the rules denied is sensitive and its
"allow" list becomes ``released``; a note without a tag gets the tag of its kind and program. A
fact about a place or a day of work that sat in the shared set goes to Chat (the core is about the
person only). The downgrade gives back the list of tag names on each note; the rules are lost.
"""

import json
import re
import uuid
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0066"
down_revision: str | None = "0065"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

UUID = postgresql.UUID(as_uuid=True)
JSONB = postgresql.JSONB(astext_type=sa.Text())

# a copy of topcoat_hub.memory_tags as of this revision (a migration does not import the app)
ROOTS = (
    ("person", "person", "Who the person is: family, home, work, health, life"),
    ("preferences", "person", "How the person likes things and wants to be answered"),
    ("everyday", "chat", "Day-to-day matters, plans, errands, small talk"),
    ("knowledge", "research", "Topics, sources and what was found out"),
    ("development", "dev", "Repositories, code, tests, the workshop"),
    ("infrastructure", "admin", "Servers, devices, services, the network"),
)
SEEDED = (
    ("health", "person", "Health, illness, medication"),
    ("finance", "person", "Money, accounts, contracts"),
    ("private", "person", "What the person wants kept to themselves"),
)
DEFAULT_DENIED = ("private", "health", "finance")
ROOT_OF_PROGRAM = {"chat": "everyday", "research": "knowledge", "dev": "development"}
ROOT_OF_PROGRAM["admin"] = "infrastructure"
PSEUDO = ("00000000-0000-4000-8000-000000000002", "00000000-0000-4000-8000-000000000003")
NAME_RE = re.compile(r"^[^\W_][\w-]{0,29}$")


def _default_tag(kind: str, application: str | None) -> str:
    if application is None:
        return "person" if kind == "person" else "preferences"
    return ROOT_OF_PROGRAM.get(application, "everyday")


def upgrade() -> None:
    op.create_table(
        "memory_tags",
        sa.Column("id", UUID, primary_key=True),
        sa.Column("user_id", UUID, sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("parent_id", UUID, sa.ForeignKey("memory_tags.id", ondelete="CASCADE")),
        sa.Column("name", sa.String(30), nullable=False),
        sa.Column("description", sa.Text(), nullable=False, server_default=""),
        sa.Column("responsible", sa.String(40)),
        sa.Column("sensitive", sa.Boolean()),
        sa.Column("released", JSONB),
        sa.Column("created_by", sa.String(16), nullable=False),
        sa.Column("reviewed", sa.Boolean(), nullable=False, server_default="true"),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.UniqueConstraint("user_id", "name", name="uq_memory_tags_user_name"),
    )
    op.create_index("ix_memory_tags_user_id", "memory_tags", ["user_id"])
    op.create_table(
        "memory_item_tags",
        sa.Column(
            "item_id",
            UUID,
            sa.ForeignKey("memory_items.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column(
            "tag_id", UUID, sa.ForeignKey("memory_tags.id", ondelete="CASCADE"), primary_key=True
        ),
        sa.Column("user_id", UUID, sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
    )
    op.create_index("ix_memory_item_tags_tag_id", "memory_item_tags", ["tag_id"])
    op.create_index("ix_memory_item_tags_user_id", "memory_item_tags", ["user_id"])

    bind = op.get_bind()
    # the core is about the person: a fact about a place or a day of work goes to Chat
    bind.execute(
        sa.text(
            "UPDATE memory_items SET application = 'chat' WHERE application IS NULL "
            "AND scope = 'user' AND kind IN ('operational', 'episode')"
        )
    )
    rules = {
        str(r.user_id): r.memory_tag_rules
        for r in bind.execute(
            sa.text(
                "SELECT user_id, memory_tag_rules FROM user_settings "
                "WHERE memory_tag_rules IS NOT NULL"
            )
        )
    }
    notes: dict[str, list[tuple[str, str, str | None, list[str]]]] = {}
    for r in bind.execute(
        sa.text(
            "SELECT id, user_id, kind, application, tags FROM memory_items "
            "WHERE scope = 'user' AND CAST(user_id AS text) NOT IN :pseudo"
        ).bindparams(sa.bindparam("pseudo", expanding=True)),
        {"pseudo": list(PSEUDO)},
    ):
        tags = r.tags if isinstance(r.tags, list) else json.loads(r.tags or "[]")
        notes.setdefault(str(r.user_id), []).append(
            (str(r.id), r.kind, r.application, [str(t) for t in tags])
        )
    tag_rows: list[dict[str, object]] = []
    link_rows: list[dict[str, object]] = []
    for user_id in sorted(set(notes) | set(rules)):
        rule = rules.get(user_id) or {}
        deny = set(rule.get("deny", DEFAULT_DENIED)) if isinstance(rule, dict) else set()
        allow = rule.get("allow", {}) if isinstance(rule, dict) else {}
        ids: dict[str, uuid.UUID] = {}

        def add(name: str, parent: str | None, responsible: str | None, description: str,
                by: str, user_id: str = user_id, ids: dict[str, uuid.UUID] = ids,
                deny: set[str] = deny, allow: dict = allow) -> None:  # fmt: skip
            new = uuid.uuid4()
            ids[name] = new
            released = sorted(p for p, tags in allow.items() if name in (tags or []))
            tag_rows.append(
                {
                    "id": new,
                    "user_id": uuid.UUID(user_id),
                    "parent_id": ids[parent] if parent else None,
                    "name": name,
                    "description": description,
                    "responsible": responsible,
                    "sensitive": True if name in deny else (False if parent is None else None),
                    "released": json.dumps(released) if name in deny and released else None,
                    "created_by": by,
                }
            )

        for name, responsible, description in ROOTS:
            add(name, None, responsible, description, "seed")
        for name, parent, description in SEEDED:
            add(name, parent, None, description, "seed")
        for name in sorted(deny - set(ids)):
            if NAME_RE.match(name):
                add(name, "person", None, "", "migration")
        for item_id, kind, application, tags in notes.get(user_id, []):
            mine = []
            for raw in tags:
                name = raw.strip().lower()
                if not NAME_RE.match(name):
                    continue
                if name not in ids:
                    add(name, _default_tag(kind, application), None, "", "migration")
                mine.append(ids[name])
            if not mine:
                mine.append(ids[_default_tag(kind, application)])
            for tag_id in dict.fromkeys(mine):
                link_rows.append(
                    {"item_id": uuid.UUID(item_id), "tag_id": tag_id, "user_id": uuid.UUID(user_id)}
                )
    tags_table = sa.table(
        "memory_tags",
        sa.column("id", UUID),
        sa.column("user_id", UUID),
        sa.column("parent_id", UUID),
        sa.column("name", sa.String),
        sa.column("description", sa.Text),
        sa.column("responsible", sa.String),
        sa.column("sensitive", sa.Boolean),
        sa.column("released", JSONB),
        sa.column("created_by", sa.String),
    )
    links_table = sa.table(
        "memory_item_tags",
        sa.column("item_id", UUID),
        sa.column("tag_id", UUID),
        sa.column("user_id", UUID),
    )
    # parents before children: the rows were made in that order
    for row in tag_rows:
        if row["released"] is not None:
            row["released"] = json.loads(str(row["released"]))
    if tag_rows:
        op.bulk_insert(tags_table, tag_rows)
    if link_rows:
        op.bulk_insert(links_table, link_rows)
    op.drop_column("memory_items", "tags")
    op.drop_column("user_settings", "memory_tag_rules")


def downgrade() -> None:
    op.add_column("user_settings", sa.Column("memory_tag_rules", JSONB))
    op.add_column("memory_items", sa.Column("tags", JSONB, nullable=False, server_default="[]"))
    op.execute(
        "UPDATE memory_items m SET tags = sub.names FROM ("
        "SELECT l.item_id, jsonb_agg(t.name ORDER BY t.name) AS names "
        "FROM memory_item_tags l JOIN memory_tags t ON t.id = l.tag_id GROUP BY l.item_id"
        ") sub WHERE sub.item_id = m.id"
    )
    op.drop_table("memory_item_tags")
    op.drop_table("memory_tags")
