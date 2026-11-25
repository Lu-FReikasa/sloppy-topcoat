"""A conversation knows the application (workspace) it was started in (docs/PLATFORM.md)."""

from fastapi.testclient import TestClient

from tests.conftest import AUTH


def create(client: TestClient, **body: object) -> object:
    return client.post("/api/conversations", headers=AUTH, json=body)


def test_a_conversation_keeps_the_application_it_was_started_in(client: TestClient) -> None:
    r = create(client, application="dev")
    assert r.status_code == 200 or r.status_code == 201, r.text
    view = r.json()
    assert view["application"] == "dev"
    again = client.get(f"/api/conversations/{view['id']}", headers=AUTH).json()
    assert again["application"] == "dev"
    listed = client.get("/api/conversations", headers=AUTH).json()
    assert [c["application"] for c in listed] == ["dev"]


def test_a_conversation_without_an_application_belongs_to_chat_from_the_browser(
    client: TestClient,
) -> None:
    view = create(client).json()
    assert view["application"] == "chat"


def test_an_application_must_look_like_a_workspace_key(client: TestClient) -> None:
    for bad in ("Dev", "dev room", "../x", "x" * 41, ""):
        assert create(client, application=bad).status_code == 422, bad


def test_the_effort_level_named_at_the_start_is_kept(client: TestClient) -> None:
    # the body always accepted it; the hub dropped it (found live: a Gründlich chat ran as normal)
    view = create(client, quality="thorough").json()
    assert view["quality"] == "thorough"
    assert (
        client.get(f"/api/conversations/{view['id']}", headers=AUTH).json()["quality"] == "thorough"
    )
    assert create(client).json()["quality"] is None
