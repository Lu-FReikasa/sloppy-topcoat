"""Resident models and VRAM pressure on the hub side (E1-06)."""

from typing import Any

from fastapi.testclient import TestClient

from tests.conftest import AUTH
from tests.fakenode import FakeNode, eventually, msg, pair


def _detail(client: TestClient, node_id: str) -> dict[str, Any]:
    r = client.get(f"/api/nodes/{node_id}", headers=AUTH)
    assert r.status_code == 200, r.text
    return r.json()


def _config_events(client: TestClient, node_id: str) -> list[dict[str, Any]]:
    events = _detail(client, node_id)["events"]
    return [e["data"] for e in reversed(events) if e["kind"] == "config_changed"]


def _action(client: TestClient, node: FakeNode, name: str, args: dict[str, Any]) -> dict:
    node.serve_actions(1)
    r = client.post(
        f"/api/nodes/{node.node_id}/actions", headers=AUTH, json={"name": name, "args": args}
    )
    node.join()
    assert r.status_code == 200, r.text
    return r.json()


def _residents(client: TestClient, node_id: str) -> dict[str, dict[str, Any]]:
    return {r["model"]: r for r in _detail(client, node_id)["config"]["residents"]}


def test_put_and_delete_resident(client: TestClient) -> None:
    node_id, _ = pair(client, "node-a")
    url = f"/api/nodes/{node_id}/residents/big"

    r = client.put(url, headers=AUTH, json={})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["changed"] is True
    assert body["config"]["residents"] == [{"model": "big", "priority": 50, "auto_reload": True}]
    assert _detail(client, node_id)["config_hash"] == body["config_hash"]

    # Same entry again: nothing changes, no event.
    r = client.put(url, headers=AUTH, json={"priority": 50, "auto_reload": True})
    assert r.json()["changed"] is False
    assert r.json()["config_hash"] == body["config_hash"]

    # PUT replaces the whole entry and keeps its position in the list.
    client.put(f"/api/nodes/{node_id}/residents/small", headers=AUTH, json={"priority": 10})
    r = client.put(url, headers=AUTH, json={"priority": 80, "context_length": 8192})
    assert r.json()["config"]["residents"] == [
        {"model": "big", "priority": 80, "auto_reload": True, "context_length": 8192},
        {"model": "small", "priority": 10, "auto_reload": True},
    ]

    r = client.delete(url, headers=AUTH)
    assert r.status_code == 200, r.text
    assert r.json()["changed"] is True
    assert [m for m in _residents(client, node_id)] == ["small"]
    # Deleting is idempotent: the wanted state already holds.
    r = client.delete(url, headers=AUTH)
    assert r.status_code == 200
    assert r.json()["changed"] is False

    events = _config_events(client, node_id)
    assert [e["source"] for e in events] == ["residents"] * 4
    assert events[0]["model"] == "big"
    assert events[0]["resident"] == {"model": "big", "priority": 50, "auto_reload": True}
    assert events[-1] == {
        "source": "residents",
        "model": "big",
        "resident": None,
        "user_id": "00000000-0000-4000-8000-000000000001",
    }


def test_resident_validation(client: TestClient) -> None:
    node_id, _ = pair(client, "node-a")
    url = f"/api/nodes/{node_id}/residents/big"
    for bad in ({"priority": 101}, {"priority": -1}, {"context_length": 0}, {"unknown": 1}):
        r = client.put(url, headers=AUTH, json=bad)
        assert r.status_code == 422, bad
    assert client.put(url, json={}).status_code == 401

    unknown = "/api/nodes/00000000-0000-4000-8000-00000000abcd/residents/big"
    r = client.put(unknown, headers=AUTH, json={})
    assert r.status_code == 404
    assert r.json()["detail"]["code"] == "not_found"
    assert client.delete(unknown, headers=AUTH).status_code == 404
    assert (
        client.put(f"/api/nodes/{node_id}/residents/{'x' * 256}", headers=AUTH, json={}).status_code
        == 422
    )
    assert _residents(client, node_id) == {}


def test_resident_change_is_pushed_to_connected_node(client: TestClient, connect) -> None:
    node_id, key = pair(client, "node-a")
    node = connect(node_id, key)
    node.handshake()
    r = client.put(f"/api/nodes/{node_id}/residents/big", headers=AUTH, json={"priority": 70})
    pushed = node.recv()
    assert pushed["type"] == "config"
    assert pushed["body"]["config_hash"] == r.json()["config_hash"]
    assert pushed["body"]["config"]["residents"] == [
        {"model": "big", "priority": 70, "auto_reload": True}
    ]


def test_manual_unload_clears_and_manual_load_sets_auto_reload(client: TestClient, connect) -> None:
    node_id, key = pair(client, "node-a")
    node = connect(node_id, key)
    node.handshake()
    client.put(f"/api/nodes/{node_id}/residents/big", headers=AUTH, json={})
    client.put(f"/api/nodes/{node_id}/residents/small", headers=AUTH, json={"auto_reload": False})

    # Loading a resident that already has auto_reload changes nothing.
    assert _action(client, node, "models.load", {"model": "big"})["ok"] is True
    assert _residents(client, node_id)["big"]["auto_reload"] is True
    assert [e["source"] for e in _config_events(client, node_id)] == ["residents", "residents"]

    # A manual unload clears auto_reload: the node must not bring the model back.
    assert _action(client, node, "models.unload", {"model": "big"})["ok"] is True
    assert _residents(client, node_id)["big"]["auto_reload"] is False
    last = _config_events(client, node_id)[-1]
    assert last == {
        "source": "manual_unload",
        "models": ["big"],
        "auto_reload": False,
        "user_id": "00000000-0000-4000-8000-000000000001",
    }

    # A manual load of a resident sets it again.
    assert _action(client, node, "models.load", {"model": "small"})["ok"] is True
    assert _residents(client, node_id)["small"]["auto_reload"] is True
    last = _config_events(client, node_id)[-1]
    assert (last["source"], last["models"], last["auto_reload"]) == ("manual_load", ["small"], True)

    # "Unload all" clears it for every resident the node reports as unloaded.
    assert _action(client, node, "models.load", {"model": "big"})["ok"] is True
    assert _residents(client, node_id)["big"]["auto_reload"] is True
    result = _action(client, node, "models.unload", {"all": True})
    assert sorted(result["result"]["unloaded"]) == ["big", "small"]
    residents = _residents(client, node_id)
    assert residents["big"]["auto_reload"] is False
    assert residents["small"]["auto_reload"] is False
    assert sorted(_config_events(client, node_id)[-1]["models"]) == ["big", "small"]

    # The node received every change.
    def pushed() -> None:
        assert node.configs, "no config pushed"

    eventually(pushed)


def test_failed_or_foreign_actions_leave_residents_alone(client: TestClient, connect) -> None:
    node_id, key = pair(client, "node-a")
    node = connect(node_id, key)
    node.handshake()
    client.put(f"/api/nodes/{node_id}/residents/big", headers=AUTH, json={})
    before = _detail(client, node_id)["config_hash"]

    assert _action(client, node, "models.unload", {})["ok"] is False  # invalid args
    assert _action(client, node, "models.load", {"model": "small"})["ok"] is True  # not resident
    assert _action(client, node, "models.unload", {"model": "small"})["ok"] is True
    assert _detail(client, node_id)["config_hash"] == before
    assert [e["source"] for e in _config_events(client, node_id)] == ["residents"]


def test_heartbeat_yielded_state_and_foreign_usage_reach_the_gui(
    client: TestClient, connect
) -> None:
    node_id, key = pair(client, "node-a")
    node = connect(node_id, key)
    node.handshake()
    gpu = {
        "index": 0,
        "vram_used_mb": 9000,
        "foreign_vram_used_mb": 8500,
        "foreign_gpu_util": 97.5,
        "foreign_shared_max_mb": 7743,
    }
    node.send(
        msg("heartbeat", {"state": "yielded", "ram_used_mb": 1000, "gpus": [gpu], "loaded": []})
    )

    def yielded() -> dict[str, Any]:
        detail = _detail(client, node_id)
        assert detail["state"] == "yielded"
        return detail

    detail = eventually(yielded)
    g = detail["gpus"][0]
    assert (g["vram_used_mb"], g["foreign_vram_used_mb"], g["foreign_gpu_util"]) == (
        9000,
        8500,
        97.5,
    )
    assert g["foreign_shared_max_mb"] == 7743
    assert "shared_used_mb" not in g

    # Nodes that cannot measure other programs simply leave the fields out.
    node.heartbeat()
    eventually(lambda: _check_no_foreign(client, node_id))


def _check_no_foreign(client: TestClient, node_id: str) -> None:
    g = _detail(client, node_id)["gpus"][0]
    assert "foreign_vram_used_mb" not in g
    assert "foreign_gpu_util" not in g
    assert "foreign_shared_max_mb" not in g
