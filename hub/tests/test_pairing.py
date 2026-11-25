import re
from datetime import UTC, datetime, timedelta

from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text

from tests.conftest import AUTH
from tests.fakenode import PUBLIC_KEY, pair


def _pair_body(code: str, name: str = "node-a") -> dict[str, str]:
    return {"code": code, "name": name, "os": "windows", "arch": "amd64", "public_key": PUBLIC_KEY}


def test_create_pairing_code(client: TestClient) -> None:
    r = client.post("/api/pairing-codes", headers=AUTH)
    assert r.status_code == 201
    body = r.json()
    assert re.fullmatch(r"[A-Z0-9]{4}-[A-Z0-9]{4}", body["code"])
    expires = datetime.fromisoformat(body["expires_at"])
    assert timedelta(minutes=14) < expires - datetime.now(UTC) <= timedelta(minutes=15)


def test_pairing_code_requires_auth(client: TestClient) -> None:
    assert client.post("/api/pairing-codes").status_code == 401


def test_pair_success_and_code_is_burned(client: TestClient, clean_db: str) -> None:
    code = client.post("/api/pairing-codes", headers=AUTH).json()["code"]
    r = client.post("/api/node/pair", json=_pair_body(code))
    assert r.status_code == 200
    body = r.json()
    assert set(body) == {"node_id"}  # no bearer credential any more (D12)

    again = client.post("/api/node/pair", json=_pair_body(code, "node-b"))
    assert again.status_code == 410

    engine = create_engine(clean_db)
    with engine.connect() as conn:
        row = conn.execute(
            text("SELECT public_key, config, state FROM nodes WHERE id = :id"),
            {"id": body["node_id"]},
        ).one()
        code_row = conn.execute(text("SELECT user_id, used_at FROM pairing_codes")).one()
    engine.dispose()
    # The hub stores only the node's public key.
    assert row.public_key == PUBLIC_KEY
    assert row.config["runner"]["type"] == "llamaserver"
    assert row.state == "offline"
    assert str(code_row.user_id) == "00000000-0000-4000-8000-000000000001"
    assert code_row.used_at is not None

    nodes = client.get("/api/nodes", headers=AUTH).json()
    assert [n["name"] for n in nodes] == ["node-a"]
    assert nodes[0]["state"] == "offline"


def test_pair_unknown_code(client: TestClient) -> None:
    r = client.post("/api/node/pair", json=_pair_body("ZZZZ-ZZZZ"))
    assert r.status_code == 404
    assert r.json()["detail"]["code"] == "invalid_code"


def test_pair_expired_code(client: TestClient, clean_db: str) -> None:
    code = client.post("/api/pairing-codes", headers=AUTH).json()["code"]
    engine = create_engine(clean_db)
    with engine.begin() as conn:
        conn.execute(text("UPDATE pairing_codes SET expires_at = now() - interval '1 second'"))
    engine.dispose()
    assert client.post("/api/node/pair", json=_pair_body(code)).status_code == 410


def test_pair_invalid_body(client: TestClient) -> None:
    r = client.post("/api/node/pair", json={"code": "ABCD-1234", "name": "Bad Name!"})
    assert r.status_code == 400
    assert r.json()["detail"]["code"] == "invalid_request"
    assert client.post("/api/node/pair", content=b"not json").status_code == 400


def test_pair_name_taken(client: TestClient) -> None:
    pair(client, "node-a")
    code = client.post("/api/pairing-codes", headers=AUTH).json()["code"]
    r = client.post("/api/node/pair", json=_pair_body(code, "node-a"))
    assert r.status_code == 409
