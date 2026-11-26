"""The house model stays a resident of its node (plan step 1.6, issue #25)."""

from typing import Any

from fastapi.testclient import TestClient

from tests.conftest import AUTH
from tests.fakenode import pair
from topcoat_hub.house_residents import house_mutation
from topcoat_hub.node_policy import NodePolicy


def _residents(client: TestClient, node_id: str) -> list[dict[str, Any]]:
    r = client.get(f"/api/nodes/{node_id}", headers=AUTH)
    assert r.status_code == 200, r.text
    return r.json()["config"].get("residents", [])


def _sources(client: TestClient, node_id: str) -> list[str]:
    events = client.get(f"/api/nodes/{node_id}", headers=AUTH).json()["events"]
    return [e["data"].get("source") for e in reversed(events) if e["kind"] == "config_changed"]


def test_the_mutation_adds_repairs_and_moves_the_house_resident() -> None:
    config: dict[str, Any] = {}
    assert house_mutation("big")(config) is True
    assert config["residents"] == [{"model": "big", "priority": 50, "auto_reload": True}]
    assert house_mutation("big")(config) is False  # already right: no change, no push

    # a manual unload cleared auto_reload; priority and context length set by hand stay
    config["residents"] = [
        {"model": "big", "priority": 80, "auto_reload": False, "context_length": 8192}
    ]
    assert house_mutation("big")(config) is True
    assert config["residents"] == [
        {"model": "big", "priority": 80, "auto_reload": True, "context_length": 8192}
    ]

    # a new house model replaces the old one; other residents stay
    config["residents"].append({"model": "embed", "priority": 50, "auto_reload": True})
    assert house_mutation("small", previous="big")(config) is True
    assert [r["model"] for r in config["residents"]] == ["embed", "small"]

    # no house model any more: only the old entry goes
    assert house_mutation(None, previous="small")(config) is True
    assert [r["model"] for r in config["residents"]] == ["embed"]


def test_a_reconnect_brings_the_house_model_back(client: TestClient, connect) -> None:
    """The live case of 2026-11-24/25: the resident entry of the house model was gone, so the
    node unloaded it on idle and never loaded it again, not even after reconnecting."""
    node_id, key = pair(client, "node-a")
    client.app.state.gateway.set_policy(NodePolicy(house={"node-a": "big"}))
    # someone removed the entry on the node page (or it never existed)
    assert client.delete(f"/api/nodes/{node_id}/residents/big", headers=AUTH).status_code == 200
    assert _residents(client, node_id) == []

    node = connect(node_id, key)
    config = node.handshake()
    # the very first config the node gets already keeps the house model loaded
    assert config["body"]["config"]["residents"] == [
        {"model": "big", "priority": 50, "auto_reload": True}
    ]
    assert _residents(client, node_id) == config["body"]["config"]["residents"]
    assert (
        config["body"]["config_hash"]
        == client.get(f"/api/nodes/{node_id}", headers=AUTH).json()["config_hash"]
    )
    assert _sources(client, node_id)[-1] == "house_reconnect"
    node.close()

    # a manual unload cleared auto_reload; the next reconnect sets it again, once
    client.put(f"/api/nodes/{node_id}/residents/big", headers=AUTH, json={"auto_reload": False})
    node = connect(node_id, key)
    assert node.handshake()["body"]["config"]["residents"][0]["auto_reload"] is True
    node.close()
    before = len(_sources(client, node_id))
    node = connect(node_id, key)
    node.handshake()
    assert len(_sources(client, node_id)) == before  # nothing to repair: no event, no new hash


def test_a_node_without_a_house_model_is_left_alone(client: TestClient, connect) -> None:
    node_id, key = pair(client, "node-a")
    node = connect(node_id, key)
    assert not node.handshake()["body"]["config"].get("residents")
    assert _sources(client, node_id) == []


def test_saving_the_house_moves_the_resident(client: TestClient, connect) -> None:
    a_id, a_key = pair(client, "node-a")
    b_id, _ = pair(client, "node-b")
    node = connect(a_id, a_key)
    node.handshake()

    r = client.put(
        "/api/scheduling/house", json={"house": {"node-a": "m1", "node-b": "m2"}}, headers=AUTH
    )
    assert r.status_code == 200, r.text
    assert [x["model"] for x in _residents(client, a_id)] == ["m1"]
    assert [x["model"] for x in _residents(client, b_id)] == ["m2"]
    # the connected node got the change pushed
    pushed = node.recv()
    assert pushed["type"] == "config"
    assert pushed["body"]["config"]["residents"][0]["model"] == "m1"

    r = client.put("/api/scheduling/house", json={"house": {"node-a": "m3"}}, headers=AUTH)
    assert r.status_code == 200, r.text
    assert [x["model"] for x in _residents(client, a_id)] == ["m3"]
    assert _residents(client, b_id) == []
    assert _sources(client, b_id) == ["house", "house"]
