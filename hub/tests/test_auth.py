import importlib

import pytest
from fastapi.testclient import TestClient

from tests.conftest import AUTH
from topcoat_hub import build_info
from topcoat_hub.build_info import BUILD


def test_health_needs_no_auth(client: TestClient) -> None:
    r = client.get("/api/health")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"
    assert r.json()["build"] == BUILD


def test_build_comes_from_the_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TOPCOAT_BUILD", "155aa03")
    assert importlib.reload(build_info).BUILD == "155aa03"
    monkeypatch.delenv("TOPCOAT_BUILD")
    assert importlib.reload(build_info).BUILD == "unknown"


def test_missing_token(client: TestClient) -> None:
    r = client.get("/api/nodes")
    assert r.status_code == 401
    assert r.json()["detail"]["code"] == "unauthorized"


def test_wrong_token(client: TestClient) -> None:
    r = client.get("/api/nodes", headers={"Authorization": "Bearer nope"})
    assert r.status_code == 401


def test_me_returns_seeded_dev_admin(client: TestClient) -> None:
    r = client.get("/api/me", headers=AUTH)
    assert r.status_code == 200
    assert r.json() == {
        "id": "00000000-0000-4000-8000-000000000001",
        "name": "dev-admin",
        "role": "admin",
        "language": None,
        "timezone": None,
        "effective_timezone": "UTC",
        "hub_timezone": "UTC",
    }


def test_empty_configured_token_rejects_everything(app, settings) -> None:
    settings.dev_admin_token = ""
    with TestClient(app) as c:
        assert c.get("/api/me", headers={"Authorization": "Bearer "}).status_code == 401
        assert c.get("/api/me", headers=AUTH).status_code == 401


def test_language_preference_is_stored_per_user(client: TestClient) -> None:
    assert client.get("/api/me", headers=AUTH).json()["language"] is None  # browser language
    r = client.put("/api/me/language", headers=AUTH, json={"language": "de"})
    assert r.status_code == 200, r.text
    assert r.json()["language"] == "de"
    assert client.get("/api/me", headers=AUTH).json()["language"] == "de"
    assert client.put("/api/me/language", headers=AUTH, json={"language": "fr"}).status_code == 422
    assert client.put("/api/me/language", json={"language": "en"}).status_code == 401
    r = client.put("/api/me/language", headers=AUTH, json={"language": None})
    assert r.json()["language"] is None


def test_there_is_no_look_to_choose_any_more(client: TestClient) -> None:
    """One look (docs/DESKTOP.md): the route of the old look switch is gone."""
    assert "look" not in client.get("/api/me", headers=AUTH).json()
    assert client.put("/api/me/look", headers=AUTH, json={"look": "glas"}).status_code in (404, 405)
