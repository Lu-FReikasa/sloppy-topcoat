"""Wake-on-LAN (E2-05 E): the magic packet and the wake endpoint."""

import pytest
from fastapi.testclient import TestClient

from tests.conftest import AUTH
from tests.fakenode import eventually, pair
from topcoat_hub.wake import magic_packet


def test_the_magic_packet_is_six_ff_and_the_address_sixteen_times() -> None:
    p = magic_packet("02:00:00:00:00:01")
    assert len(p) == 102
    assert p[:6] == b"\xff" * 6
    assert p[6:] == bytes.fromhex("020000000001") * 16
    assert magic_packet("02-00-00-00-00-01") == p
    for bad in ("", "02:00:00:00:00", "zz:00:00:00:00:01", "02:00:00:00:00:01:02"):
        with pytest.raises(ValueError):
            magic_packet(bad)


class FakeWake:
    def __init__(self) -> None:
        self.sent: list[tuple[str, str, int]] = []
        self.fail = False

    async def send(self, mac: str, broadcast: str, port: int) -> None:
        if self.fail:
            raise OSError("network unreachable")
        self.sent.append((mac, broadcast, port))


def _offline_node(client: TestClient, connect) -> str:
    node_id, cred = pair(client, "node-a")
    node = connect(node_id, cred)
    node.handshake()
    eventually(lambda: _assert_inventory(client, node_id))
    node.close()

    def offline() -> None:
        assert client.get(f"/api/nodes/{node_id}", headers=AUTH).json()["connected"] is False

    eventually(offline)
    return node_id


def _assert_inventory(client: TestClient, node_id: str) -> None:
    detail = client.get(f"/api/nodes/{node_id}", headers=AUTH).json()
    assert detail["network"], "the inventory has not arrived yet"


def test_the_hub_keeps_the_interfaces_and_wakes_an_offline_node(
    client: TestClient, connect
) -> None:
    wake = FakeWake()
    client.app.state.wake = wake
    node_id = _offline_node(client, connect)
    detail = client.get(f"/api/nodes/{node_id}", headers=AUTH).json()
    assert [i["mac"] for i in detail["network"]] == ["02:00:00:00:00:01", "02:00:00:00:00:02"]

    r = client.post(f"/api/nodes/{node_id}/wake", headers=AUTH)
    assert r.status_code == 200, r.text
    # only the wired address: a wireless card rarely wakes, and there is a wired one
    assert wake.sent == [("02:00:00:00:00:01", "255.255.255.255", 9)]
    assert r.json() == {"sent": 1, "broadcast": "255.255.255.255", "port": 9}
    events = client.get(f"/api/nodes/{node_id}", headers=AUTH).json()["events"]
    assert "wake_sent" in [e["kind"] for e in events]


def test_a_connected_node_needs_no_wake(client: TestClient, connect) -> None:
    client.app.state.wake = FakeWake()
    node_id, cred = pair(client, "node-a")
    node = connect(node_id, cred)
    node.handshake()
    r = client.post(f"/api/nodes/{node_id}/wake", headers=AUTH)
    assert r.status_code == 409 and r.json()["detail"]["code"] == "already_online"
    node.close()


def test_a_node_without_a_known_address_cannot_be_woken(client: TestClient) -> None:
    client.app.state.wake = FakeWake()
    node_id, _ = pair(client, "node-a")  # never connected: no inventory
    r = client.post(f"/api/nodes/{node_id}/wake", headers=AUTH)
    assert r.status_code == 409 and r.json()["detail"]["code"] == "no_mac"
    assert (
        client.post(
            "/api/nodes/00000000-0000-4000-8000-00000000abcd/wake", headers=AUTH
        ).status_code
        == 404
    )


def test_a_failing_broadcast_is_a_clear_502(client: TestClient, connect) -> None:
    wake = FakeWake()
    wake.fail = True
    client.app.state.wake = wake
    node_id = _offline_node(client, connect)
    r = client.post(f"/api/nodes/{node_id}/wake", headers=AUTH)
    assert r.status_code == 502 and r.json()["detail"]["code"] == "wake_failed"


def test_waking_needs_the_admin(client: TestClient) -> None:
    assert client.post("/api/nodes/00000000-0000-4000-8000-00000000abcd/wake").status_code == 401
