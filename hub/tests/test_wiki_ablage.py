"""A document of the Ablage into the wiki (2026-11-13): a preview, then the person's click; the page
lies in the folder of the application that made the document, a second click on the same text
changes nothing, another document with the same title gets a page of its own, and another person
gets a 404 (D20)."""

import uuid

from fastapi.testclient import TestClient

from tests.conftest import AUTH, TEST_DB_URL
from tests.users import make_user
from topcoat_hub.artifact_store import ArtifactStore
from topcoat_hub.models import DEV_ADMIN_ID, AgentTask
from topcoat_hub.report_store import ReportStore


def _documents(client: TestClient) -> tuple[str, str, str]:
    async def go() -> tuple[str, str, str]:
        async with client.app.state.sessionmaker() as db:
            reports = ReportStore(db, DEV_ADMIN_ID)
            common = {"question": "q", "language": "de", "depth": "thorough", "meta": {}}
            one = await reports.create(title="Pixel/Animation", body="# Eins", **common)
            two = await reports.create(title="Pixel/Animation", body="# Zwei", **common)
            task = AgentTask(
                id=uuid.uuid4(),
                user_id=DEV_ADMIN_ID,
                prompt="p",
                title="t",
                application="dev",
                status="done",
            )
            db.add(task)
            await db.flush()
            art = await ArtifactStore(db, DEV_ADMIN_ID).put_report(task.id, "Build-Notiz", "Text")
            await db.commit()
            return str(one.id), str(two.id), str(art.id)

    return client.portal.call(go)


def test_a_document_of_the_ablage_becomes_a_page_in_its_applications_folder(
    client: TestClient,
) -> None:
    one, two, art = _documents(client)
    preview = client.get(f"/api/wiki/ablage/report/{one}?lang=de", headers=AUTH).json()
    assert preview["path"] == "Recherche/Pixel-Animation" and preview["new_page"] is True
    r = client.post(f"/api/wiki/ablage/report/{one}?lang=de", headers=AUTH)
    assert r.status_code == 201, r.text
    assert r.json()["folder"] == "Recherche" and r.json()["version"] == 1
    # the same text again: nothing to take over
    again = client.post(f"/api/wiki/ablage/report/{one}?lang=de", headers=AUTH)
    assert again.status_code == 409
    # another report with the same title does not overwrite the first one's page
    other = client.post(f"/api/wiki/ablage/report/{two}?lang=de", headers=AUTH).json()
    assert other["path"] == f"Recherche/Pixel-Animation ({two[:8]})"
    # the report of a development task goes to the development folder
    dev = client.post(f"/api/wiki/ablage/artifact/{art}?lang=de", headers=AUTH).json()
    assert dev["path"] == "Entwicklung/Build-Notiz"
    paths = [
        p["path"]
        for s in client.get("/api/wiki/pages", headers=AUTH).json()["sections"]
        for p in s["pages"]
    ]
    assert {"Recherche/Pixel-Animation", "Entwicklung/Build-Notiz"} <= set(paths)


def test_another_person_cannot_take_a_document_into_their_wiki(client: TestClient) -> None:
    one, _, art = _documents(client)
    _, other = make_user(TEST_DB_URL, "kim")
    assert client.get(f"/api/wiki/ablage/report/{one}", headers=other).status_code == 404
    assert client.post(f"/api/wiki/ablage/artifact/{art}", headers=other).status_code == 404
    assert client.post(f"/api/wiki/ablage/thing/{one}", headers=AUTH).status_code == 404
