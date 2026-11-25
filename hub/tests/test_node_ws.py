import json

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from starlette.websockets import WebSocketDisconnect

from tests.conftest import AUTH
from tests.fakenode import INVENTORY, auth_header, eventually, msg, pair


def _node(client: TestClient, node_id: str) -> dict:
    r = client.get(f"/api/nodes/{node_id}", headers=AUTH)
    assert r.status_code == 200, r.text
    return r.json()


def test_session_hello_config_inventory_heartbeat(client: TestClient, connect) -> None:
    node_id, cred = pair(client, "node-a", os_name="linux")
    node = connect(node_id, cred)
    config = node.handshake()
    assert config["body"]["config"]["runner"]["type"] == "llamaserver"
    assert len(config["body"]["config_hash"]) == 64

    def ready() -> dict:
        detail = _node(client, node_id)
        assert detail["ram_used_mb"] == 1000
        return detail

    detail = eventually(ready)
    assert detail["connected"] is True
    assert detail["state"] == "available"
    assert detail["agent_version"] == "test"
    assert detail["gpus"] == [
        {
            "index": 0,
            "name": "Fake GPU",
            "vendor": "amd",
            "vram_total_mb": 16000,
            "vram_used_mb": 500,
        }
    ]
    assert detail["ram_used_mb"] == 1000
    assert [m["loaded"] for m in detail["models"]] == [False, False]
    assert detail["runner"] == {"type": "fake", "available": True}
    assert "node.benchmark" in detail["config"]["allowed_actions"]
    assert detail["benchmarks"] == []
    kinds = [e["kind"] for e in detail["events"]]
    assert "connected" in kinds
    assert "paired" in kinds

    node.close()

    def offline() -> dict:
        detail = _node(client, node_id)
        assert detail["events"][0]["kind"] == "disconnected"
        return detail

    detail = eventually(offline)
    assert detail["state"] == "offline"
    assert detail["connected"] is False


def test_heartbeat_state_busy_gaming(client: TestClient, connect) -> None:
    node_id, cred = pair(client, "node-a")
    node = connect(node_id, cred)
    node.handshake()
    node.heartbeat(state="busy_gaming")

    def gaming() -> None:
        assert _node(client, node_id)["state"] == "busy_gaming"

    eventually(gaming)
    node.close()


def test_queued_events_are_stored_once(client: TestClient, connect) -> None:
    node_id, cred = pair(client, "node-a")
    node = connect(node_id, cred)
    node.handshake()
    ev = msg("event", {"kind": "game_detected", "data": {"process": "game.exe"}})
    ev["ts"] = "2026-01-01T10:00:00Z"
    node.send(msg("queued_events", {"events": [ev]}))
    node.send(msg("queued_events", {"events": [ev]}))  # replay after a lost ack
    node.send(msg("event", {"kind": "game_ended", "data": {}}))

    def all_stored() -> list:
        events = client.get(f"/api/nodes/{node_id}/events", headers=AUTH).json()
        assert "game_ended" in [e["kind"] for e in events]
        return events

    games = [e for e in eventually(all_stored) if e["kind"] == "game_detected"]
    assert len(games) == 1
    assert games[0]["ts"].startswith("2026-01-01T10:00:00")
    assert games[0]["data"] == {"process": "game.exe"}
    node.close()


def test_invalid_message_gets_error_reply(client: TestClient, connect) -> None:
    node_id, cred = pair(client, "node-a")
    node = connect(node_id, cred)
    node.handshake()
    bad = msg("heartbeat", {"state": "offline", "ram_used_mb": 1, "gpus": [], "loaded": []})
    node.send(bad)
    reply = node.recv()
    assert reply["type"] == "error"
    assert reply["body"]["code"] == "invalid_message"
    assert reply["re"] == bad["id"]
    node.ws.send_text("{not json")
    assert node.recv()["body"]["code"] == "invalid_message"
    # Nodes may not send hub->node messages.
    node.send(msg("action", {"name": "models.list", "args": {}}))
    assert node.recv()["body"]["code"] == "invalid_message"
    node.close()


def test_first_message_must_be_matching_hello(client: TestClient, connect) -> None:
    node_id, cred = pair(client, "node-a")
    other_id, _ = pair(client, "node-b")
    with client.websocket_connect("/api/node/ws", headers=auth_header(client, node_id, cred)) as ws:
        hello = msg(
            "hello",
            {"node_id": other_id, "agent_version": "x", "protocol_version": 0, "config_hash": None},
        )
        ws.send_text(json.dumps(hello))
        reply = json.loads(ws.receive_text())
        assert reply["body"]["code"] == "invalid_message"
        with pytest.raises(WebSocketDisconnect) as exc:
            ws.receive_text()
        assert exc.value.code == 4400
    assert _node(client, node_id)["connected"] is False


def test_unsupported_version_is_read_only(client: TestClient, connect) -> None:
    node_id, cred = pair(client, "node-a")
    with client.websocket_connect("/api/node/ws", headers=auth_header(client, node_id, cred)) as ws:
        ws.send_text(json.dumps(msg("hello", {"node_id": node_id}, v=7)))
        reply = json.loads(ws.receive_text())
        assert reply["type"] == "error"
        assert reply["body"]["code"] == "upgrade_required"
        detail = _node(client, node_id)
        assert detail["connected"] is True
        assert detail["state"] == "degraded"
        r = client.post(
            f"/api/nodes/{node_id}/actions", headers=AUTH, json={"name": "models.list", "args": {}}
        )
        assert r.status_code == 409
        assert r.json()["detail"]["code"] == "upgrade_required"


def test_action_roundtrip(client: TestClient, connect) -> None:
    node_id, cred = pair(client, "node-a")
    node = connect(node_id, cred)
    node.handshake()
    node.serve_actions(1)
    r = client.post(
        f"/api/nodes/{node_id}/actions",
        headers=AUTH,
        json={"name": "models.load", "args": {"model": "big"}},
    )
    node.join()
    assert r.status_code == 200, r.text
    assert r.json() == {"ok": True, "result": {"model": "big"}}
    action = node.received[0]
    assert action["type"] == "action"
    assert action["body"] == {"name": "models.load", "args": {"model": "big"}}

    def loaded() -> dict:
        detail = _node(client, node_id)
        assert detail["gpus"][0]["vram_used_mb"] == 4500
        return detail

    detail = eventually(loaded)
    big = next(m for m in detail["models"] if m["name"] == "big")
    assert big == {
        "name": "big",
        "size_bytes": 9000,
        "format": "gguf",
        "loaded": True,
        "vram_used_mb": 4000,
    }
    assert detail["gpus"][0]["vram_used_mb"] == 4500
    audit = next(e for e in detail["events"] if e["kind"] == "action")
    assert audit["data"]["user_id"] == "00000000-0000-4000-8000-000000000001"
    assert audit["data"]["ok"] is True
    node.close()


def test_action_error_result_is_passed_through(client: TestClient, connect) -> None:
    node_id, cred = pair(client, "node-a")
    node = connect(node_id, cred)
    node.handshake()
    node.serve_actions(1)
    r = client.post(
        f"/api/nodes/{node_id}/actions", headers=AUTH, json={"name": "models.unload", "args": {}}
    )
    node.join()
    assert r.status_code == 200
    assert r.json()["ok"] is False
    assert r.json()["error"]["code"] == "invalid_args"
    node.close()


def test_action_timeout(client: TestClient, connect) -> None:
    node_id, cred = pair(client, "node-a")
    node = connect(node_id, cred)
    node.handshake()
    node.serve_actions(1, answer=False)
    r = client.post(
        f"/api/nodes/{node_id}/actions", headers=AUTH, json={"name": "models.list", "args": {}}
    )
    assert r.status_code == 504
    assert r.json()["detail"]["code"] == "timeout"
    node.close()


def test_action_offline_unknown_and_not_allowed(client: TestClient, connect) -> None:
    node_id, _ = pair(client, "node-a")
    url = f"/api/nodes/{node_id}/actions"
    r = client.post(url, headers=AUTH, json={"name": "models.list", "args": {}})
    assert r.status_code == 409
    assert r.json()["detail"]["code"] == "node_offline"

    r = client.post(url, headers=AUTH, json={"name": "shell.exec", "args": {"cmd": "id"}})
    assert r.status_code == 403
    assert r.json()["detail"]["code"] == "not_allowed"

    r = client.post(url, headers=AUTH, json={"name": "not a name", "args": {}})
    assert r.status_code == 422

    r = client.post(
        "/api/nodes/00000000-0000-4000-8000-00000000abcd/actions",
        headers=AUTH,
        json={"name": "models.list", "args": {}},
    )
    assert r.status_code == 404


def test_second_connection_replaces_first(client: TestClient, connect) -> None:
    node_id, cred = pair(client, "node-a")
    first = connect(node_id, cred)
    first.handshake()
    second = connect(node_id, cred)
    second.handshake()
    # The old socket is closed with 4409; its inventory/heartbeat may still be queued before.
    with pytest.raises(WebSocketDisconnect) as exc:
        while True:
            first.recv()
    assert exc.value.code == 4409
    assert _node(client, node_id)["connected"] is True
    second.close()

    def disconnected() -> None:
        assert _node(client, node_id)["connected"] is False

    eventually(disconnected)


def test_list_nodes_requires_auth_and_lists(client: TestClient, connect) -> None:
    assert client.get("/api/nodes").status_code == 401
    pair(client, "node-b")
    pair(client, "node-a")
    names = [n["name"] for n in client.get("/api/nodes", headers=AUTH).json()]
    assert names == ["node-a", "node-b"]
    # the role tells a compute node ("gpu") from a tool node; the models page counts by it
    roles = [n["roles"] for n in client.get("/api/nodes", headers=AUTH).json()]
    assert all(isinstance(r, list) for r in roles)
    assert client.get("/api/nodes/not-a-uuid", headers=AUTH).status_code == 422


def test_hub_startup_marks_stale_nodes_offline(app, client: TestClient, clean_db: str) -> None:
    node_id, _ = pair(client, "node-a")
    engine = create_engine(clean_db)

    def db_state() -> str:
        with engine.begin() as conn:
            return conn.execute(
                text("SELECT state FROM nodes WHERE id = :id"), {"id": node_id}
            ).scalar_one()

    # Simulate a hub crash that left the node marked as online in the DB.
    with engine.begin() as conn:
        conn.execute(text("UPDATE nodes SET state = 'available' WHERE id = :id"), {"id": node_id})
    assert db_state() == "available"

    with TestClient(app):  # runs the lifespan startup again
        assert db_state() == "offline"
    engine.dispose()


def test_v0_nodes_are_still_accepted_and_answered_in_v0(client: TestClient, connect) -> None:
    """The hub speaks v1 and keeps accepting v0 nodes for one more release (E2-01)."""
    node_id, cred = pair(client, "node-old")
    node = connect(node_id, cred, version=0)
    config = node.handshake()
    assert config["v"] == 0  # answered in the node's own version
    detail = eventually(lambda: _ready(client, node_id))
    assert detail["state"] == "available"
    connected = next(e for e in detail["events"] if e["kind"] == "connected")
    assert connected["data"] == {"read_only": False, "protocol_version": 0}

    node.serve_actions(1)
    r = client.post(
        f"/api/nodes/{node_id}/actions",
        headers=AUTH,
        json={"name": "models.load", "args": {"model": "small"}},
    )
    node.join()
    assert r.status_code == 200 and r.json()["ok"] is True
    assert node.received[0]["v"] == 0

    # A push (config change) also carries v0.
    cfg = _node(client, node_id)["config"]
    assert client.put(f"/api/nodes/{node_id}/config", headers=AUTH, json=cfg).status_code == 200
    assert node.recv()["v"] == 0
    node.close()


def test_v1_nodes_get_v1(client: TestClient, connect) -> None:
    node_id, cred = pair(client, "node-new")
    node = connect(node_id, cred)
    assert node.handshake()["v"] == 1
    detail = eventually(lambda: _ready(client, node_id))
    connected = next(e for e in detail["events"] if e["kind"] == "connected")
    assert connected["data"]["protocol_version"] == 1
    node.close()


def test_a_v0_message_on_a_v1_session_is_rejected(client: TestClient, connect) -> None:
    node_id, cred = pair(client, "node-mixed")
    node = connect(node_id, cred)
    node.handshake()
    bad = msg("heartbeat", {"state": "available", "ram_used_mb": 1, "gpus": [], "loaded": []})
    node.ws.send_text(json.dumps({**bad, "v": 0}))
    reply = node.recv()
    assert reply["type"] == "error" and reply["body"]["code"] == "invalid_message"
    node.close()


def _ready(client: TestClient, node_id: str) -> dict:
    detail = _node(client, node_id)
    assert detail["connected"] and detail["state"] == "available"
    return detail


def test_the_space_of_the_model_drive_is_shown_and_a_benchmark_error_is_an_event(
    client: TestClient, connect
) -> None:
    from tests.fakenode import INVENTORY

    node_id, cred = pair(client, "node-a")
    node = connect(node_id, cred)
    node.handshake()
    hardware = {**INVENTORY["hardware"], "disk_total_mb": 953869, "disk_free_mb": 412345}
    node.send(msg("inventory", {**INVENTORY, "hardware": hardware}))
    node.send(
        msg(
            "event",
            {
                "kind": "benchmark_error",
                "data": {
                    "benchmark_id": "7b1e9f00-0000-4000-8000-0000000000aa",
                    "model": "big",
                    "backend": "vulkan",
                    "threads": 4,
                    "error": "ErrorOutOfDeviceMemory",
                },
            },
        )
    )

    def shown() -> dict:
        # the inventory and the event are two messages handled one after the other: wait for
        # both, not only for the first
        detail = _node(client, node_id)
        assert detail["disk_free_mb"] == 412345
        assert "benchmark_error" in [e["kind"] for e in detail["events"]]
        return detail

    detail = eventually(shown)
    assert detail["disk_total_mb"] == 953869
    assert detail["benchmarks"] == []  # an error is no measurement
    # a node that reports nothing about its disk shows nothing
    other_id, other_cred = pair(client, "node-b")
    other = connect(other_id, other_cred)
    other.handshake()

    def other_ready() -> dict:
        detail = _node(client, other_id)
        assert detail["connected"] is True and detail["runner"] is not None
        return detail

    assert eventually(other_ready)["disk_free_mb"] is None
    node.close()
    other.close()


def test_a_late_action_result_is_stored_as_an_event(client: TestClient, connect) -> None:
    # The hub answered 504 long ago; the node finishes the action a minute later (a model.load
    # waits for the gateway's load time, so the test uses an action with the short timeout).
    node_id, cred = pair(client, "node-a")
    node = connect(node_id, cred)
    node.handshake()
    node.serve_actions(1, answer=False)
    r = client.post(
        f"/api/nodes/{node_id}/actions",
        headers=AUTH,
        json={"name": "models.list", "args": {}},
    )
    node.join()
    assert r.status_code == 504
    error = {"code": "runner_error", "message": "list models: runner unavailable"}
    node.send(msg("action_result", {"ok": False, "error": error}, re=node.received[0]["id"]))
    # an id nobody asked for is not an event
    node.send(
        msg("action_result", {"ok": True, "result": {}}, re="7b1e9f00-0000-4000-8000-0000000000ff")
    )

    def late() -> dict:
        events = [e for e in _node(client, node_id)["events"] if e["kind"] == "action_late_result"]
        assert len(events) == 1
        return events[0]

    event = eventually(late)
    assert event["data"] == {"name": "models.list", "ok": False, "error": error}
    node.close()


def test_loading_a_model_is_given_the_gateways_load_time() -> None:
    from topcoat_hub.api.gui import _action_timeout
    from topcoat_hub.config import Settings

    settings = Settings(action_timeout_seconds=30.0, gateway_load_timeout_seconds=600.0)
    assert _action_timeout(settings, "models.load") == 600.0
    assert _action_timeout(settings, "models.list") == 30.0
    short = Settings(action_timeout_seconds=30.0, gateway_load_timeout_seconds=5.0)
    assert _action_timeout(short, "models.load") == 30.0


def test_model_own_flag_reaches_the_node_summary(client: TestClient, connect) -> None:
    node_id, cred = pair(client, "node-a")
    node = connect(node_id, cred)
    node.inventory = {
        **INVENTORY,
        "models": [
            {"name": "mine", "size_bytes": 1, "format": "gguf", "own": True},
            {"name": "shared", "size_bytes": 2, "format": "gguf", "own": False},
            {"name": "old", "size_bytes": 3, "format": "gguf"},
        ],
    }
    node.handshake()

    def models() -> dict:
        detail = _node(client, node_id)
        assert len(detail["models"]) == 3
        return {m["name"]: m for m in detail["models"]}

    got = eventually(models)
    assert got["mine"]["own"] is True
    assert got["shared"]["own"] is False
    assert "own" not in got["old"]
    node.close()
