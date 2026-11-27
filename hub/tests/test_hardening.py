"""Limits on what a caller can try before it is known, and clicks only the person can make."""

import uuid

import pytest
from fastapi.testclient import TestClient

from tests.conftest import AUTH
from topcoat_hub import serve
from topcoat_hub.api.node import (
    CHALLENGES_PER_MINUTE,
    MAX_UNAUTHENTICATED_BODY,
    PAIRINGS_PER_MINUTE,
)
from topcoat_hub.auth import FAILED_SIGN_INS_PER_MINUTE, dev_token_problem
from topcoat_hub.config import Settings
from topcoat_hub.ratelimit import AddressLimiter

# --- the admin token ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("token", "refused"),
    [
        ("", False),  # GUI locked, as before
        ("change-me-too", True),
        ("Change-Me", True),
        ("x" * 31, True),
        ("x" * 32, False),
    ],
)
def test_the_admin_token_must_be_long_and_not_the_example(token: str, refused: bool) -> None:
    assert (dev_token_problem(token) is not None) is refused


def test_the_hub_does_not_start_with_a_weak_token(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(serve, "get_settings", lambda: Settings(dev_admin_token="change-me-too"))
    started: list[bool] = []
    monkeypatch.setattr(serve, "check_known", lambda *_: started.append(True))
    with pytest.raises(SystemExit) as exc:
        serve.main([])
    assert exc.value.code == 1
    assert started == []


# --- failed sign-ins -----------------------------------------------------------------------------


def test_failed_sign_ins_from_one_address_are_slowed_down(client: TestClient) -> None:
    wrong = {"Authorization": "Bearer wrong-token"}
    for _ in range(FAILED_SIGN_INS_PER_MINUTE):
        assert client.get("/api/core/proposals", headers=wrong).status_code == 401
    r = client.get("/api/core/proposals", headers=wrong)
    assert r.status_code == 429
    assert int(r.headers["retry-after"]) >= 1
    # the right token waits as well: the address is held, not the guess
    assert client.get("/api/core/proposals", headers=AUTH).status_code == 429


def test_signed_in_calls_do_not_count(client: TestClient) -> None:
    for _ in range(FAILED_SIGN_INS_PER_MINUTE + 5):
        assert client.get("/api/core/proposals", headers=AUTH).status_code == 200


def test_the_limiter_forgets_after_the_window() -> None:
    now = [0.0]
    limiter = AddressLimiter(2, 60.0, clock=lambda: now[0])
    assert limiter.take("a") and limiter.take("a")
    assert not limiter.take("a")
    assert limiter.take("b")  # another address has its own count
    assert limiter.retry_after("a") == 60
    now[0] = 61.0
    assert limiter.take("a")


def test_the_limiter_keeps_a_bounded_number_of_addresses() -> None:
    limiter = AddressLimiter(1, 60.0, max_addresses=3, clock=lambda: 0.0)
    for address in "abcd":
        limiter.record(address)
    assert not limiter.blocked("a")  # forgotten first
    assert limiter.blocked("d")


# --- the endpoints a node reaches before it is known ------------------------------------------


def test_a_large_body_is_refused_before_it_is_read(client: TestClient) -> None:
    big = b'{"node_id": "' + b"x" * (MAX_UNAUTHENTICATED_BODY + 1) + b'"}'
    for path in ("/api/node/challenge", "/api/node/pair"):
        r = client.post(path, content=big, headers={"Content-Type": "application/json"})
        assert r.status_code == 413, path


def test_a_large_body_without_a_length_is_refused(client: TestClient) -> None:
    def chunks():
        yield b'{"node_id": "'
        for _ in range(MAX_UNAUTHENTICATED_BODY // 1024 + 2):
            yield b"x" * 1024
        yield b'"}'

    r = client.post("/api/node/challenge", content=chunks())
    assert r.status_code == 413


def test_challenges_per_address_are_limited(client: TestClient) -> None:
    node_id = str(uuid.uuid4())
    for _ in range(CHALLENGES_PER_MINUTE):
        assert client.post("/api/node/challenge", json={"node_id": node_id}).status_code == 200
    r = client.post("/api/node/challenge", json={"node_id": node_id})
    assert r.status_code == 429
    assert "retry-after" in r.headers


def test_pairing_attempts_per_address_are_limited(client: TestClient) -> None:
    body = {"code": "AAAA-AAAA", "name": "node-x", "os": "linux", "arch": "amd64"}
    for _ in range(PAIRINGS_PER_MINUTE):
        r = client.post("/api/node/pair", json={**body, "public_key": "A" * 43})
        assert r.status_code in (400, 404)
    r = client.post("/api/node/pair", json={**body, "public_key": "A" * 43})
    assert r.status_code == 429


# fixed, not random: the xdist workers must collect the same tests
_SOME_ID = "00000000-0000-4000-8000-000000000001"


# --- clicks only the person makes -------------------------------------------------------------


@pytest.mark.parametrize(
    "path",
    [
        f"/api/handovers/{_SOME_ID}/send",
        f"/api/core/proposals/{_SOME_ID}/accept",
        f"/api/project/nodes/{_SOME_ID}/confirm",
    ],
)
def test_an_api_key_cannot_click_for_the_person(client: TestClient, path: str) -> None:
    key = client.post("/api/keys", headers=AUTH, json={"name": "script"}).json()["key"]
    body = {"hash": "x" * 64, "works": True}
    r = client.post(path, headers={"Authorization": f"Bearer {key}"}, json=body)
    assert r.status_code == 403
    assert r.json()["detail"]["code"] == "person_only"
