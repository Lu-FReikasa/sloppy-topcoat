"""A request wakes the sleeping node whose house model it needs (plan step 1.7, issue #26)."""

import threading
from typing import Any

from fastapi.testclient import TestClient

from tests.conftest import AUTH
from tests.fakenode import eventually, pair
from topcoat_hub.node_policy import NodePolicy
from topcoat_hub.wake import broadcasts

CHAT = {"model": "big@node-a", "messages": [{"role": "user", "content": "Hallo Welt"}]}


class WakeThat:
    """A fake sender; ``on_wake`` plays the machine waking up."""

    def __init__(self, on_wake: Any = None) -> None:
        self.sent: list[tuple[str, str, int]] = []
        self.on_wake = on_wake

    async def send(self, mac: str, broadcast: str, port: int) -> None:
        self.sent.append((mac, broadcast, port))
        if self.on_wake is not None and len(self.sent) == 1:
            threading.Thread(target=self.on_wake, daemon=True).start()


def _asleep(client: TestClient, connect, timeout: float) -> tuple[str, Any]:
    """node-a, house model ``big``, known to the hub and now offline."""
    client.app.state.gateway.settings.wake_timeout_seconds = timeout
    client.app.state.gateway.set_policy(NodePolicy(house={"node-a": "big"}))
    node_id, key = pair(client, "node-a")
    node = connect(node_id, key)
    node.handshake()

    def known() -> None:
        assert client.get(f"/api/nodes/{node_id}", headers=AUTH).json()["network"]

    eventually(known)
    node.close()

    def offline() -> None:
        assert client.get(f"/api/nodes/{node_id}", headers=AUTH).json()["connected"] is False

    eventually(offline)
    return node_id, key


def _events(client: TestClient, node_id: str, kind: str) -> list[dict[str, Any]]:
    events = client.get(f"/api/nodes/{node_id}", headers=AUTH).json()["events"]
    return [e["data"] for e in events if e["kind"] == kind]


def test_a_request_wakes_the_node_and_waits_for_it(client: TestClient, connect) -> None:
    node_id, key = _asleep(client, connect, timeout=20)
    seen: dict[str, Any] = {}

    def wake_up() -> None:
        # the request waits meanwhile, visible as "waking" in what the nodes do
        def waking() -> None:
            kinds = [(a["node"], a["kind"]) for a in client.app.state.gateway.activity()]
            assert ("node-a", "waking") in kinds

        eventually(waking)
        seen["waking"] = True
        node = connect(node_id, key)
        node.handshake()
        node.loaded = [{"name": "big", "vram_used_mb": 4000}]
        node.serve()
        node.heartbeat()

    client.app.state.wake = WakeThat(wake_up)
    r = client.post("/v1/chat/completions", headers=AUTH, json=CHAT)
    assert r.status_code == 200, r.text
    assert r.headers["x-topcoat-node"] == "node-a"
    assert seen.get("waking") is True
    # the wired address only, once; the activity entry is gone again
    assert client.app.state.wake.sent == [("02:00:00:00:00:01", "255.255.255.255", 9)]
    assert all(a["kind"] != "waking" for a in client.app.state.gateway.activity())
    assert _events(client, node_id, "wake_sent") == [
        {"reason": "demand", "model": "big", "addresses": 1}
    ]


def test_a_node_that_does_not_come_ends_with_a_clear_error(client: TestClient, connect) -> None:
    _asleep(client, connect, timeout=1.5)
    client.app.state.wake = WakeThat()
    r = client.post("/v1/chat/completions", headers=AUTH, json=CHAT)
    assert r.status_code == 503, r.text
    assert r.json()["error"]["code"] == "wake_timeout"
    assert "node-a did not wake within 1 s" in r.json()["error"]["message"]
    assert len(client.app.state.wake.sent) == 1


def test_only_the_house_model_wakes_a_node(client: TestClient, connect) -> None:
    _asleep(client, connect, timeout=20)
    client.app.state.wake = WakeThat()
    r = client.post("/v1/chat/completions", headers=AUTH, json={**CHAT, "model": "small@node-a"})
    assert r.status_code == 503
    assert r.json()["error"]["code"] == "model_unavailable"
    assert client.app.state.wake.sent == []


def test_waking_on_demand_can_be_switched_off(client: TestClient, connect) -> None:
    _asleep(client, connect, timeout=0)
    client.app.state.wake = WakeThat()
    r = client.post("/v1/chat/completions", headers=AUTH, json=CHAT)
    assert r.status_code == 503
    assert r.json()["error"]["code"] == "model_unavailable"
    assert client.app.state.wake.sent == []


def test_several_broadcast_addresses_are_allowed() -> None:
    assert broadcasts("10.0.0.255, 10.1.0.255") == ["10.0.0.255", "10.1.0.255"]
    assert broadcasts("") == ["255.255.255.255"]
