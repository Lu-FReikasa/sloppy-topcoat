"""The built-in programs as plug-in manifests and the one list of commands (D105)."""

from fastapi.testclient import TestClient

from tests.conftest import AUTH, TEST_DB_URL
from tests.users import make_user
from topcoat_hub import plugins


def test_every_built_in_program_is_a_valid_manifest(client: TestClient) -> None:
    got = client.get("/api/ui/plugins", headers=AUTH)
    assert got.status_code == 200, got.text
    manifests = got.json()["plugins"]
    assert {"chat", "research", "dev", "project", "admin", "settings"} <= {
        m["id"] for m in manifests
    }
    for m in manifests:
        assert plugins.problems(m) == [], (m["id"], plugins.problems(m))
        assert m["commands"][0]["action"] == {"kind": "open_program", "program": m["id"]}


def test_the_commands_are_what_the_person_may_open(client: TestClient) -> None:
    admin = client.get("/api/ui/commands", headers=AUTH).json()["commands"]
    ids = {c["id"] for c in admin}
    assert "open.admin" in ids and "open.dev" in ids and "chat.chat" in ids
    _, user = make_user(TEST_DB_URL, "plain", role="user")
    mine = {c["id"] for c in client.get("/api/ui/commands", headers=user).json()["commands"]}
    assert "open.chat" in mine and "open.admin" not in mine and "open.dev" not in mine


def test_a_broken_manifest_names_its_problems() -> None:
    bad = {"id": "X", "version": "1", "title_key": "t", "views": [{"id": "v", "type": "iframe"}]}
    text = " ".join(plugins.problems(bad))
    assert "id" in text and "version" in text and "views.0.type" in text
