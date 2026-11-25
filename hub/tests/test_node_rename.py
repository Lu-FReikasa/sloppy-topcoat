"""Renaming a node (2026-11-09 plan, problem 7): the living settings follow the new name, the
history keeps the old one, and the old name still leads to the node."""

import json
from typing import Any

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text

from tests.conftest import AUTH, TEST_DB_URL
from tests.fakenode import pair, public_key_b64
from tests.test_chat import new_conv
from topcoat_hub import agent_prompts, check_order, node_rename
from topcoat_hub.gateway import split_model


def _sql(query: str, **params: Any) -> list[Any]:
    engine = create_engine(TEST_DB_URL)
    with engine.begin() as conn:
        result = conn.execute(text(query), params)
        rows = list(result) if result.returns_rows else []
    engine.dispose()
    return rows


def rename(client: TestClient, node_id: str, name: str, who: dict[str, str] = AUTH) -> Any:
    return client.post(f"/api/nodes/{node_id}/rename", headers=who, json={"name": name})


# --- the pieces ----------------------------------------------------------------------------


def test_a_model_choice_and_a_scope_follow_the_new_name() -> None:
    assert node_rename.rewrite_model("qwen@node-a,node-b", "node-a", "pc-1") == "qwen@pc-1,node-b"
    assert node_rename.rewrite_model("qwen", "node-a", "pc-1") == "qwen"
    assert node_rename.rewrite_model("qwen@node-ab", "node-a", "pc-1") == "qwen@node-ab"
    pools = {"pools": {"node-a": {"available": ["m"]}, "node-b": {}}, "order": ["node-a", "x"]}
    assert node_rename.rewrite_json(pools, "node-a", "pc-1") == {
        "pools": {"pc-1": {"available": ["m"]}, "node-b": {}},
        "order": ["pc-1", "x"],
    }


# --- the whole way -------------------------------------------------------------------------


def test_a_rename_rewrites_the_living_settings_and_keeps_the_history(client: TestClient) -> None:
    node_id, _ = pair(client, "node-a")
    conv = new_conv(
        client,
        model="small@node-a",
        node="node-a",
        tool_scope={"nodes": ["node-a", "topcoat"], "groups": None, "internet": False},
    )
    _sql(
        "INSERT INTO hub_settings (key, value) VALUES ('node_policy', CAST(:v AS jsonb)) "
        "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value",
        v=json.dumps({"pools": {"node-a": {"available": ["small"]}}}),
    )
    r = rename(client, node_id, "pc-1")
    assert r.status_code == 200, r.text
    assert r.json()["from"] == "node-a" and r.json()["to"] == "pc-1"
    assert r.json()["rewritten"]["conversations"] == 1

    got = client.get(f"/api/conversations/{conv}", headers=AUTH).json()
    assert got["model"] == "small@pc-1" and got["node"] == "pc-1"
    assert got["tool_scope"]["nodes"] == ["pc-1", "topcoat"]
    policy = _sql("SELECT value FROM hub_settings WHERE key = 'node_policy'")[0][0]
    assert list(policy["pools"]) == ["pc-1"]
    names = {n["name"] for n in client.get("/api/nodes", headers=AUTH).json()}
    assert "pc-1" in names and "node-a" not in names
    events = _sql("SELECT kind, data FROM node_events WHERE node_id = :n", n=node_id)
    assert any(kind == "renamed" and data["from"] == "node-a" for kind, data in events)

    # the old name still leads to the node: the gateway, the agent, a question
    assert split_model("small@node-a") == ("small", "pc-1")
    tools = [{"node": "pc-1", "capability": "system.info"}, {"node": "other", "capability": "x"}]
    assert agent_prompts.resolve_node("node-a", tools) == (
        "pc-1",
        "node 'node-a' is now called 'pc-1'",
    )
    assert check_order.named_machine("Was ist mit node-a los?", ["pc-1", "other"]) == "pc-1"

    # nobody may pair a new node under the old name; the node may take its old name back
    code = client.post("/api/pairing-codes", headers=AUTH).json()["code"]
    taken = client.post(
        "/api/node/pair",
        json={
            "code": code,
            "name": "node-a",
            "os": "linux",
            "arch": "amd64",
            "public_key": public_key_b64(Ed25519PrivateKey.generate()),
        },
    )
    assert taken.status_code == 409 and taken.json()["detail"]["code"] == "name_taken"
    back = rename(client, node_id, "node-a")
    assert back.status_code == 200 and node_rename.current_name("node-a") == "node-a"
    assert node_rename.current_name("pc-1") == "node-a"


def test_a_rename_is_refused_for_bad_or_taken_names(client: TestClient) -> None:
    a, _ = pair(client, "node-a")
    pair(client, "node-b")
    assert rename(client, a, "Office-PC").json()["detail"]["code"] == "invalid_name"
    assert rename(client, a, "node-b").json()["detail"]["code"] == "name_taken"
    assert rename(client, a, "node-a").json()["detail"]["code"] == "same_name"
    assert rename(client, "00000000-0000-4000-8000-000000000000", "x1").status_code == 404
