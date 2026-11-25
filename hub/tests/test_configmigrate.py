"""D14: migrations add new default permissions and config sections to existing node configs."""

import json

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.exc import IntegrityError

from tests.conftest import AUTH
from tests.fakenode import pair
from topcoat_hub.config import REPO_ROOT
from topcoat_hub.configmigrate import add_default_actions, add_default_capabilities
from topcoat_hub.protocol import config_hash, load_schemas

SCHEMAS = load_schemas(REPO_ROOT / "proto" / "v1")


def test_adds_missing_actions_once(client: TestClient, clean_db: str) -> None:
    old_id, _ = pair(client, "node-old")
    new_id, _ = pair(client, "node-new")
    engine = create_engine(clean_db)
    with engine.begin() as conn:
        # node-old looks like it was paired with an E1-01 hub.
        conn.execute(
            text(
                "UPDATE nodes SET config = jsonb_set(config, '{allowed_actions}', "
                '\'["models.list", "models.load", "models.unload"]\') WHERE id = :id'
            ),
            {"id": old_id},
        )
        actions = ["models.download", "models.delete", "node.benchmark"]
        assert add_default_actions(conn, actions, "test") == 1
        assert add_default_actions(conn, actions, "test") == 0  # idempotent
        cfg, stored_hash = conn.execute(
            text("SELECT config, config_hash FROM nodes WHERE id = :id"), {"id": old_id}
        ).one()
    engine.dispose()
    assert cfg["allowed_actions"] == [
        "models.list",
        "models.load",
        "models.unload",
        "models.download",
        "models.delete",
        "node.benchmark",
    ]
    assert stored_hash == config_hash(cfg)

    old = client.get(f"/api/nodes/{old_id}", headers=AUTH).json()
    changed = [e for e in old["events"] if e["kind"] == "config_changed"]
    assert changed and changed[0]["data"] == {"source": "test", "added": actions}
    new = client.get(f"/api/nodes/{new_id}", headers=AUTH).json()
    assert not [e for e in new["events"] if e["kind"] == "config_changed"]


def test_config_defaults_by_inventory(client: TestClient, clean_db: str) -> None:
    """Migration 0005: pressure on for nodes with a discrete GPU (or no inventory yet)."""
    import importlib.util

    from topcoat_hub.configmigrate import add_config_defaults

    spec = importlib.util.spec_from_file_location(
        "m0005", REPO_ROOT / "hub" / "migrations" / "versions" / "0005_residents_pressure.py"
    )
    assert spec and spec.loader
    m0005 = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m0005)

    ids = {name: pair(client, name)[0] for name in ("dgpu", "igpu", "cpu", "fresh", "custom")}
    inventories = {
        "dgpu": {"hardware": {"gpus": [{"index": 0, "integrated": False}]}},
        "igpu": {"hardware": {"gpus": [{"index": 0, "integrated": True}]}},
        "cpu": {"hardware": {"gpus": []}},
    }
    engine = create_engine(clean_db)
    with engine.begin() as conn:
        # Configs as an E1-05 hub stored them: no residents / pressure yet.
        conn.execute(text("UPDATE nodes SET config = config - 'residents' - 'pressure'"))
        conn.execute(
            text(
                "UPDATE nodes SET config = jsonb_set(config, '{pressure}', "
                "'{\"enabled\": false}') WHERE id = :id"
            ),
            {"id": ids["custom"]},
        )
        for name, inv in inventories.items():
            conn.execute(
                text(
                    "INSERT INTO node_inventory (node_id, inventory) "
                    "VALUES (:id, CAST(:inv AS jsonb))"
                ),
                {"id": ids[name], "inv": json.dumps(inv)},
            )
        changed = add_config_defaults(
            conn,
            lambda inv: {
                "residents": [],
                "pressure": m0005.default_pressure(m0005.has_discrete_gpu(inv)),
            },
            source="test",
        )
        rows = dict(conn.execute(text("SELECT name, config FROM nodes")).all())
    engine.dispose()
    assert changed == 5
    assert rows["dgpu"]["pressure"]["enabled"] is True
    assert rows["fresh"]["pressure"]["enabled"] is True
    assert rows["igpu"]["pressure"]["enabled"] is False
    assert rows["cpu"]["pressure"]["enabled"] is False
    assert rows["custom"]["pressure"] == {"enabled": False}  # existing value kept
    assert all(cfg["residents"] == [] for cfg in rows.values())
    for cfg in rows.values():
        SCHEMAS.validate("node_config", cfg)


def test_db_accepts_every_schema_node_state(clean_db: str) -> None:
    """The nodes.state CHECK constraint must allow every state a node may report, or the
    hub drops the session on the first heartbeat with a new state."""
    engine = create_engine(clean_db)
    with engine.begin() as conn:
        definition = conn.scalar(
            text(
                "SELECT pg_get_constraintdef(oid) FROM pg_constraint "
                "WHERE conname = 'nodes_state_check'"
            )
        )
    engine.dispose()
    states = SCHEMAS.validator("common").schema["$defs"]["node_state"]["enum"]
    for state in [*states, "offline"]:
        assert f"'{state}'" in definition, state


def test_state_yielded_migration_roundtrip(clean_db: str) -> None:
    """Migration 0005 allows state 'yielded'; its downgrade maps it back to 'available'."""
    from alembic import command

    from tests.conftest import alembic_config

    engine = create_engine(clean_db)
    with engine.begin() as conn:
        node_id = conn.scalar(
            text(
                "INSERT INTO nodes (id, name, os, arch, public_key, state, config, config_hash) "
                "VALUES (gen_random_uuid(), 'node-y', 'linux', 'amd64', 'k', 'yielded', "
                "'{}', 'h') RETURNING id"
            )
        )
    cfg = alembic_config()
    try:
        command.downgrade(cfg, "0004")
        with engine.begin() as conn:
            state = conn.scalar(text("SELECT state FROM nodes WHERE id = :id"), {"id": node_id})
        assert state == "available"
        with engine.connect() as conn, pytest.raises(IntegrityError):
            conn.execute(text("UPDATE nodes SET state = 'yielded'"))
    finally:
        command.upgrade(cfg, "head")
        engine.dispose()


def test_new_capabilities_go_to_dev_nodes_only(client: TestClient, clean_db: str) -> None:
    """Migration 0052: the capabilities of background test runs are added to dev nodes."""
    dev_id, _ = pair(client, "dev-box", role="dev")
    tools_id, _ = pair(client, "tools-box", role="tools")
    names = ["repo.test_start", "repo.test_status", "repo.test_cancel"]
    engine = create_engine(clean_db)
    with engine.begin() as conn:
        conn.execute(
            text(
                "UPDATE nodes SET config = jsonb_set(config, '{capabilities}', "
                '\'["repo.read", "repo.test"]\') WHERE id = :id'
            ),
            {"id": dev_id},
        )
        assert add_default_capabilities(conn, names, "test", only_role="dev") == 1
        assert add_default_capabilities(conn, names, "test", only_role="dev") == 0  # idempotent
        dev = conn.execute(text("SELECT config FROM nodes WHERE id = :id"), {"id": dev_id}).scalar()
        tools = conn.execute(
            text("SELECT config FROM nodes WHERE id = :id"), {"id": tools_id}
        ).scalar()
    engine.dispose()
    assert dev["capabilities"] == ["repo.read", "repo.test", *names]
    assert not set(names) & set(tools["capabilities"])


def test_a_role_gets_its_new_actions_once(client: TestClient, clean_db: str) -> None:
    """D76: a dev node that takes capability calls gets workshop.manage; other roles do not."""
    from topcoat_hub.configmigrate import add_role_actions

    dev_id, _ = pair(client, "dev-old", role="dev")
    tools_id, _ = pair(client, "tools-a", role="tools")
    engine = create_engine(clean_db)
    with engine.begin() as conn:
        conn.execute(  # paired before D76
            text(
                "UPDATE nodes SET config = jsonb_set(config, '{allowed_actions}', "
                '\'["capability.invoke", "capability.execute"]\') WHERE id = :id'
            ),
            {"id": dev_id},
        )
        assert add_role_actions(conn, ["workshop.manage"], "test", role="dev") == 1
        assert add_role_actions(conn, ["workshop.manage"], "test", role="dev") == 0
        rows = dict(conn.execute(text("SELECT id, config FROM nodes")).all())
    engine.dispose()
    by_id = {str(k): v for k, v in rows.items()}
    assert by_id[dev_id]["allowed_actions"][-1] == "workshop.manage"
    assert "workshop.manage" not in by_id[tools_id]["allowed_actions"]
