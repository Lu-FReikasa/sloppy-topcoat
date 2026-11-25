"""Signature-based node authentication (D12): challenge endpoint and WebSocket upgrade."""

import base64
import uuid

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from tests.fakenode import auth_header, pair
from topcoat_hub.nodeauth import ChallengeStore, parse_authorization, signed_message


def _refused(client: TestClient, headers: dict[str, str] | None = None) -> int:
    with (
        pytest.raises(WebSocketDisconnect) as exc,
        client.websocket_connect("/api/node/ws", headers=headers or {}),
    ):
        pass
    return exc.value.code


def _header(node_id: str, nonce: str, key: Ed25519PrivateKey) -> dict[str, str]:
    sig = base64.urlsafe_b64encode(key.sign(signed_message(node_id, nonce))).rstrip(b"=").decode()
    return {"Authorization": f"Topcoat-Ed25519 {node_id}.{nonce}.{sig}"}


def _nonce(client: TestClient, node_id: str) -> str:
    return client.post("/api/node/challenge", json={"node_id": node_id}).json()["nonce"]


def test_valid_signature_is_accepted(client: TestClient) -> None:
    node_id, key = pair(client, "node-a")
    with client.websocket_connect("/api/node/ws", headers=auth_header(client, node_id, key)):
        pass


def test_missing_or_bearer_auth_is_refused(client: TestClient) -> None:
    pair(client, "node-a")
    assert _refused(client) == 4401
    assert _refused(client, {"Authorization": "Bearer something"}) == 4401


def test_wrong_key_is_refused(client: TestClient) -> None:
    node_id, _ = pair(client, "node-a")
    other = Ed25519PrivateKey.generate()
    assert _refused(client, _header(node_id, _nonce(client, node_id), other)) == 4401


def test_nonce_is_single_use(client: TestClient) -> None:
    node_id, key = pair(client, "node-a")
    headers = auth_header(client, node_id, key)
    with client.websocket_connect("/api/node/ws", headers=headers):
        pass
    assert _refused(client, headers) == 4401


def test_nonce_of_another_node_is_refused(client: TestClient) -> None:
    node_id, key = pair(client, "node-a")
    other_id, _ = pair(client, "node-b")
    assert _refused(client, _header(node_id, _nonce(client, other_id), key)) == 4401


def test_invented_nonce_is_refused(client: TestClient) -> None:
    node_id, key = pair(client, "node-a")
    assert _refused(client, _header(node_id, "x" * 43, key)) == 4401


def test_expired_nonce_is_refused(client: TestClient, app) -> None:
    node_id, key = pair(client, "node-a")
    now = [1000.0]
    app.state.challenges = ChallengeStore(ttl_seconds=60, clock=lambda: now[0])
    nonce = _nonce(client, node_id)
    now[0] += 61
    assert _refused(client, _header(node_id, nonce, key)) == 4401


def test_unknown_node_gets_a_nonce_but_no_session(client: TestClient) -> None:
    unknown = str(uuid.uuid4())
    r = client.post("/api/node/challenge", json={"node_id": unknown})
    assert r.status_code == 200  # no hint whether the node exists
    assert r.json()["expires_in"] == 60
    key = Ed25519PrivateKey.generate()
    assert _refused(client, _header(unknown, r.json()["nonce"], key)) == 4401


def test_challenge_rejects_bad_body(client: TestClient) -> None:
    r = client.post("/api/node/challenge", json={"node_id": "nope"})
    assert r.status_code == 400
    assert r.json()["detail"]["code"] == "invalid_request"
    assert client.post("/api/node/challenge", content=b"{").status_code == 400


def test_store_evicts_oldest_when_full() -> None:
    store = ChallengeStore(max_pending=2)
    a, b, c = (uuid.uuid4() for _ in range(3))
    na, nb, nc = store.issue(a), store.issue(b), store.issue(c)
    assert len(store) == 2
    assert not store.consume(a, na)
    assert store.consume(b, nb)
    assert store.consume(c, nc)
    assert not store.consume(c, nc)


# fixed: xdist needs identical test ids in every worker
_UUID = "6f1c2d3e-4a5b-4c6d-8e7f-0a1b2c3d4e5f"


@pytest.mark.parametrize(
    "value",
    [
        "",
        "Bearer x",
        "Topcoat-Ed25519",
        "Topcoat-Ed25519 a.b",
        "Topcoat-Ed25519 not-a-uuid.nonce.AAAA",
        f"Topcoat-Ed25519 {_UUID}.nonce.AAAA",  # signature too short
        f"Topcoat-Ed25519 {_UUID}..{'A' * 86}",  # empty nonce
    ],
)
def test_parse_authorization_rejects_garbage(value: str) -> None:
    assert parse_authorization({"authorization": value}) is None


def test_signature_vector_matches_node_implementation() -> None:
    # The same vector is asserted in the Go node tests (protocol_test.go).
    key = Ed25519PrivateKey.from_private_bytes(bytes([1]) * 32)
    node_id = "3f0c2a4e-8a51-4b7e-9a0e-1f2d3c4b5a60"
    nonce = "n0nce-for-the-test-vector-0123456789"
    sig = base64.urlsafe_b64encode(key.sign(signed_message(node_id, nonce))).rstrip(b"=")
    assert sig.decode() == (
        "fBJa7nZB5kTdcwIQgOa8rMS3C0lELhcH6wSpWlDt1w-lvNhZXwmqj2Q_eHXUjmkJn7hm_FlZtRBxeZ9JadFFBg"
    )
    header = f"Topcoat-Ed25519 {node_id}.{nonce}.{sig.decode()}"
    auth = parse_authorization({"authorization": header})
    assert auth is not None
    assert str(auth.node_id) == node_id
    assert auth.nonce == nonce
