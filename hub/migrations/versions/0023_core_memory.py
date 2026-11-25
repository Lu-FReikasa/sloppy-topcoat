"""core memory (E3-06): core / scope / example on notes, proposal queue, the conversation's core

Revision ID: 0023
Revises: 0022
Create Date: 2026-10-06

``memory_items`` gain ``core`` (pinned), ``scope`` (user | household | system), ``example``
(style) and ``weak`` (a style hint of adjectives only); the kinds grow by ``style``, ``rule`` and
``episode``.
Household and system notes belong to two fixed pseudo users (so every query scoped by ``user_id``
stays as strict as before and no user's query ever sees them). ``memory_proposals`` is the
queue of changes to rule / style / core the assistant may only *propose*. ``conversations`` keep
the core block their prompt was built from (byte-identical between two questions).

Personas and prompt snapshots (point 11): ``personas`` (the library: templates owned by the
household pseudo user, personal ones by their user), ``prompt_snapshots`` (the system prompt as
sent, once per user and SHA-256), ``conversations.persona_ref``, ``messages.prompt_hash`` /
``persona_ref`` / ``feedback``, ``user_settings.persona_id`` / ``persona_wizard``.

Existing data: every user with chat settings gets their form of address and the fixed language rule
as ``style`` / ``rule`` core notes (source ``user``, ``source_ref.legacy``); a non-empty system
prompt becomes the personal persona "Eigener Text" (selected as their default and pinned to every
conversation they have, so those keep their prompt) — nothing is lost; ``ChatSettings`` stays as it
is (the old API keeps working). Downgrade: everything new is removed (the legacy settings were never
touched), the old kinds and statuses come back.
"""

import json
import uuid
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB, UUID

revision: str = "0023"
down_revision: str | None = "0022"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

HOUSEHOLD_ID = "00000000-0000-4000-8000-000000000002"
SYSTEM_ID = "00000000-0000-4000-8000-000000000003"
OLD_KINDS = "kind IN ('person', 'preference', 'operational')"
NEW_KINDS = "kind IN ('person', 'preference', 'operational', 'style', 'rule', 'episode')"

LANGUAGE_RULE = "Answer in the language of the user's last message unless asked otherwise."
ADDRESS = {
    "du": 'In German, address the user informally with "du".',
    "sie": 'In German, address the user formally with "Sie".',
}


def upgrade() -> None:
    op.add_column(
        "memory_items", sa.Column("core", sa.Boolean(), nullable=False, server_default=sa.false())
    )
    op.add_column(
        "memory_items",
        sa.Column("scope", sa.String(10), nullable=False, server_default="user"),
    )
    op.add_column("memory_items", sa.Column("example", sa.Text()))
    op.add_column(
        "memory_items", sa.Column("weak", sa.Boolean(), nullable=False, server_default=sa.false())
    )
    op.drop_constraint("memory_items_kind_check", "memory_items", type_="check")
    op.create_check_constraint("memory_items_kind_check", "memory_items", NEW_KINDS)
    op.create_check_constraint(
        "memory_items_scope_check", "memory_items", "scope IN ('user', 'household', 'system')"
    )
    op.create_index("ix_memory_items_core", "memory_items", ["user_id", "core"])

    op.create_table(
        "memory_proposals",
        sa.Column(
            "id", UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")
        ),
        sa.Column(
            "user_id",
            UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("action", sa.String(12), nullable=False),
        sa.Column("scope", sa.String(10), nullable=False, server_default="user"),
        sa.Column("target_id", UUID(as_uuid=True)),
        sa.Column("kind", sa.String(12), nullable=False),
        sa.Column("text", sa.Text(), nullable=False, server_default=""),
        sa.Column("example", sa.Text()),
        sa.Column("core", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("weak", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("reason", sa.Text(), nullable=False, server_default=""),
        sa.Column("evidence", JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("diff", JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("previous", JSONB()),
        sa.Column("origin", sa.String(16), nullable=False),
        sa.Column("status", sa.String(10), nullable=False, server_default="proposed"),
        sa.Column("fingerprint", sa.String(40), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("decided_at", sa.DateTime(timezone=True)),
        sa.Column("decision", JSONB()),
        sa.CheckConstraint(
            "status IN ('proposed', 'accepted', 'rejected')", name="memory_proposals_status_check"
        ),
    )
    op.create_index("ix_memory_proposals_user", "memory_proposals", ["user_id", "status"])
    op.create_index(
        "ix_memory_proposals_fingerprint", "memory_proposals", ["user_id", "fingerprint"]
    )

    op.add_column("conversations", sa.Column("core_stamp", sa.String(64)))
    op.add_column("conversations", sa.Column("core_text", sa.Text()))
    op.add_column("conversations", sa.Column("persona_ref", sa.String(40)))

    op.create_table(
        "personas",
        sa.Column(
            "id", UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")
        ),
        sa.Column(
            "user_id",
            UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("scope", sa.String(10), nullable=False, server_default="user"),
        sa.Column("key", sa.String(40)),
        sa.Column("name", sa.String(80), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("components", JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("example", JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("defaults", JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("status", sa.String(10), nullable=False, server_default="active"),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.CheckConstraint("scope IN ('household', 'user')", name="personas_scope_check"),
        sa.CheckConstraint("status IN ('active', 'archived')", name="personas_status_check"),
    )
    op.create_index("ix_personas_user", "personas", ["user_id", "status"])
    op.create_index("ix_personas_key", "personas", ["user_id", "key"])

    op.create_table(
        "prompt_snapshots",
        sa.Column(
            "user_id",
            UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("hash", sa.String(64), primary_key=True),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("persona_ref", sa.String(80)),
        sa.Column("core_version", sa.String(64)),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
    )

    op.add_column("messages", sa.Column("prompt_hash", sa.String(64)))
    op.add_column("messages", sa.Column("persona_ref", sa.String(80)))
    op.add_column("messages", sa.Column("feedback", sa.SmallInteger()))
    op.add_column(
        "user_settings",
        sa.Column(
            "persona_id",
            UUID(as_uuid=True),
            sa.ForeignKey("personas.id", ondelete="SET NULL"),
        ),
    )
    op.add_column("user_settings", sa.Column("persona_wizard", sa.String(10)))

    # the two actors that own what belongs to nobody in particular (no key, never signs in)
    for ident, name in ((HOUSEHOLD_ID, "household"), (SYSTEM_ID, "system-rules")):
        op.execute(
            sa.text(
                "INSERT INTO users (id, name, role) VALUES (CAST(:i AS uuid), :n, 'user') "
                "ON CONFLICT DO NOTHING"
            ).bindparams(i=ident, n=name)
        )

    # existing settings become core notes: nothing a user wrote is lost
    conn = op.get_bind()
    rows = conn.execute(sa.text("SELECT user_id, system_prompt, address FROM user_settings")).all()
    for user_id, system_prompt, address in rows:
        legacy = [("language", "rule", LANGUAGE_RULE)]
        legacy.append(("address", "style", ADDRESS.get(address or "du", ADDRESS["du"])))
        if system_prompt and system_prompt.strip():
            persona_id = str(uuid.uuid4())
            conn.execute(
                sa.text(
                    "INSERT INTO personas (id, user_id, scope, key, name, version, components, "
                    "example, defaults, status) VALUES (CAST(:id AS uuid), CAST(:u AS uuid), "
                    "'user', 'legacy', :n, 1, CAST(:c AS jsonb), '{}'::jsonb, '{}'::jsonb, "
                    "'active')"
                ).bindparams(
                    id=persona_id,
                    u=str(user_id),
                    n="Eigener Text",
                    c=json.dumps({"free_text": system_prompt.strip()}),
                )
            )
            conn.execute(
                sa.text(
                    "UPDATE user_settings SET persona_id = CAST(:p AS uuid) "
                    "WHERE user_id = CAST(:u AS uuid)"
                ).bindparams(p=persona_id, u=str(user_id))
            )
            conn.execute(
                sa.text(
                    "UPDATE conversations SET persona_ref = :p WHERE user_id = CAST(:u AS uuid)"
                ).bindparams(p=persona_id, u=str(user_id))
            )
        for key, kind, text in legacy:
            item_id = str(uuid.uuid4())
            conn.execute(
                sa.text(
                    "INSERT INTO memory_items (id, user_id, text, status, confidence, evidence, "
                    "source_type, source_ref, version, kind, core, scope) VALUES "
                    "(CAST(:id AS uuid), CAST(:u AS uuid), :t, 'active', 1.0, 'doc', 'user', "
                    "CAST(:ref AS jsonb), 1, :k, true, 'user')"
                ).bindparams(
                    id=item_id,
                    u=str(user_id),
                    t=text,
                    ref=json.dumps({"legacy": key}),
                    k=kind,
                )
            )
            conn.execute(
                sa.text(
                    "INSERT INTO memory_versions (item_id, user_id, version, text, status, "
                    "evidence, confidence, reason, actor, source) VALUES (CAST(:id AS uuid), "
                    "CAST(:u AS uuid), 1, :t, 'active', 'doc', 1.0, "
                    "'taken over from the chat settings', 'user', CAST(:ref AS jsonb))"
                ).bindparams(id=item_id, u=str(user_id), t=text, ref=json.dumps({"legacy": key}))
            )


def downgrade() -> None:
    op.drop_column("user_settings", "persona_wizard")
    op.drop_column("user_settings", "persona_id")
    op.drop_column("messages", "feedback")
    op.drop_column("messages", "persona_ref")
    op.drop_column("messages", "prompt_hash")
    op.drop_table("prompt_snapshots")
    op.drop_table("personas")
    op.drop_column("conversations", "persona_ref")
    # pseudo users take their notes along (cascade); notes of real users of the new kinds go too
    op.execute(
        sa.text("DELETE FROM users WHERE id IN (CAST(:h AS uuid), CAST(:s AS uuid))").bindparams(
            h=HOUSEHOLD_ID, s=SYSTEM_ID
        )
    )
    op.execute("DELETE FROM memory_items WHERE kind IN ('style', 'rule', 'episode')")
    op.drop_column("conversations", "core_text")
    op.drop_column("conversations", "core_stamp")
    op.drop_table("memory_proposals")
    op.drop_index("ix_memory_items_core", table_name="memory_items")
    op.drop_constraint("memory_items_scope_check", "memory_items", type_="check")
    op.drop_constraint("memory_items_kind_check", "memory_items", type_="check")
    op.create_check_constraint("memory_items_kind_check", "memory_items", OLD_KINDS)
    op.drop_column("memory_items", "weak")
    op.drop_column("memory_items", "example")
    op.drop_column("memory_items", "scope")
    op.drop_column("memory_items", "core")
