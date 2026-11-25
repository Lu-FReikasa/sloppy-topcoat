"""Upload sources of the knowledge base (E2-03 rest): files the admin puts in by hand, no node.

They share the folder rules (only notes, the exclude list, the secret filter, per-user visibility)
and show up in the same search."""

from typing import Any

from fastapi.testclient import TestClient

from tests.conftest import AUTH, TEST_DB_URL
from tests.fakenode import eventually
from tests.users import make_user

NOTE = "# Router\n\nThe router reboots every sunday at 04:00 to apply the maintenance window.\n"


def upload(client: TestClient, who=AUTH, **body: Any):
    payload = {"name": "Handbook", "filename": "router.md", "text": NOTE, **body}
    return client.post("/api/knowledge/uploads", headers=who, json=payload)


def search(client: TestClient, q: str, who=AUTH) -> list[dict[str, Any]]:
    r = client.get("/api/knowledge/search", headers=who, params={"q": q})
    assert r.status_code == 200, r.text
    return r.json()["hits"]


def test_an_upload_is_searchable_at_once(client: TestClient) -> None:
    r = upload(client)
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["kind"] == "upload" and body["status"] == "ready" and body["node"] == ""
    assert body["files"] == 1 and body["chunks"] >= 1
    assert body["file"] == {"path": "router.md", "chunks": body["chunks"], "blocked": 0}
    hits = search(client, "router reboots sunday")
    assert hits and hits[0]["source"] == "Handbook"


def test_a_second_file_goes_into_the_same_source_and_a_new_text_replaces_the_old(
    client: TestClient,
) -> None:
    sid = upload(client).json()["id"]
    r = upload(client, source_id=sid, filename="nas.txt", text="The NAS shares media over NFS.")
    assert r.status_code == 201 and r.json()["files"] == 2
    upload(client, source_id=sid, text="# Router\n\nThe router is replaced by a switch.\n")
    assert not any("reboots" in h["text"] for h in search(client, "router reboots sunday"))
    assert any("switch" in h["text"] for h in search(client, "router replaced switch"))
    assert client.get(f"/api/knowledge/sources/{sid}", headers=AUTH).json()["files"] == 2


def test_a_file_can_be_removed(client: TestClient) -> None:
    sid = upload(client).json()["id"]
    r = client.delete(
        f"/api/knowledge/sources/{sid}/files", headers=AUTH, params={"path": "router.md"}
    )
    assert r.status_code == 204
    assert search(client, "router reboots sunday") == []
    assert client.get(f"/api/knowledge/sources/{sid}", headers=AUTH).json()["files"] == 0
    r = client.delete(
        f"/api/knowledge/sources/{sid}/files", headers=AUTH, params={"path": "router.md"}
    )
    assert r.status_code == 404


def test_the_folder_rules_apply(client: TestClient) -> None:
    assert upload(client, filename="photo.png").status_code == 422
    assert upload(client, filename="ZUGANGSDATEN.md").json()["detail"]["code"] == "excluded"
    assert upload(client, filename="my-password.txt").status_code == 422
    assert upload(client, text="x" * (1 << 20) + "y").status_code == 413
    assert upload(client, text="a\x00b").status_code == 422
    # a path is reduced to its name
    r = upload(client, filename="C:\\docs\\sub/ok.md")
    assert r.json()["file"]["path"] == "ok.md"
    assert upload(client, name="").status_code == 422  # a new source needs a name


def test_the_secret_filter_holds_chunks_back(client: TestClient) -> None:
    text = NOTE + "\n## Login\n\nDas Passwort für den NAS ist hunter2x und gilt für alle.\n"
    body = upload(client, text=text).json()
    assert body["file"]["blocked"] >= 1 and body["blocked"] >= 1
    assert search(client, "hunter2x") == []
    assert search(client, "router reboots sunday")  # the rest of the file is searchable


def test_a_short_note_with_one_password_line_keeps_the_rest(client: TestClient) -> None:
    # found on real hardware: a one-chunk note vanished whole because of one line
    text = (
        "# Printer\n\nThe printer sits in the cellar next to the boiler.\n"
        "Das Passwort für den Drucker ist hunter2x\nIts toner is replaced every spring.\n"
    )
    body = upload(client, filename="printer.md", text=text).json()
    assert body["chunks"] == 1 and body["file"]["blocked"] >= 1
    assert search(client, "hunter2x") == []
    hits = search(client, "printer cellar boiler toner")
    assert hits and "cellar" in hits[0]["text"] and "toner" in hits[0]["text"]
    assert "hunter2x" not in hits[0]["text"] and "Passwort" not in hits[0]["text"]


def test_a_secret_that_spreads_over_two_lines_is_left_out_too(client: TestClient) -> None:
    text = (
        "# Switch\n\nThe switch is mounted in the rack, top slot.\n"
        "Passwort:\nhunter2x\nFirmware is from 2024.\n"
    )
    upload(client, filename="switch.md", text=text)
    assert search(client, "hunter2x") == []
    assert any("rack" in h["text"] for h in search(client, "switch mounted rack firmware"))


def test_a_private_key_holds_the_whole_chunk_back(client: TestClient) -> None:
    text = (
        "# Keys\n\nThe backup key lives here.\n-----BEGIN OPENSSH PRIVATE KEY-----\n"
        "b3BlbnNzaC1rZXktdjEAAAAABG5vbmUAAAAEbm9uZQAAAAAAAAABAAAAMwAAAAtzc2gtZW\n"
        "-----END OPENSSH PRIVATE KEY-----\n"
    )
    body = upload(client, filename="keys.md", text=text).json()
    assert body["chunks"] == 0 and body["file"]["blocked"] >= 1


def test_only_the_admin_uploads_and_private_stays_private(client: TestClient) -> None:
    _, bob = make_user(TEST_DB_URL, "bob")
    _, admin_key = make_user(TEST_DB_URL, "admin-2", role="admin")
    for who in (bob, admin_key):
        assert upload(client, who=who).status_code == 403
    sid = upload(client, name="Mine", visibility="private").json()["id"]
    assert search(client, "router reboots sunday", who=bob) == []
    assert client.get(f"/api/knowledge/sources/{sid}", headers=bob).status_code == 404
    r = client.delete(
        f"/api/knowledge/sources/{sid}/files", headers=bob, params={"path": "router.md"}
    )
    assert r.status_code == 403
    assert search(client, "router reboots sunday")  # the owner still sees it


def test_a_node_source_takes_no_upload_and_an_upload_source_needs_no_node(
    client: TestClient,
) -> None:
    sid = upload(client).json()["id"]
    # re-indexing an upload source never asks a node: it only recounts and re-embeds
    r = client.post(f"/api/knowledge/sources/{sid}/reindex", headers=AUTH)
    assert r.status_code == 200

    def settled() -> dict[str, Any]:
        s = client.get(f"/api/knowledge/sources/{sid}", headers=AUTH).json()
        assert s["status"] == "ready" and s["files"] == 1, s
        return s

    eventually(settled, timeout=10)
    assert upload(client, source_id="00000000-0000-4000-8000-000000000000").status_code == 404
