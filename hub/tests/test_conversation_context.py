"""Where a conversation was started (Clipper at a desktop window, conversation_context.py): the
page's view, ids and title are kept in a fixed shape, and the ids the hub knows become names from
the person's own rows only."""

import uuid

from fastapi.testclient import TestClient

from tests.conftest import AUTH, TEST_DB_URL
from tests.users import make_user
from topcoat_hub import conversation_context
from topcoat_hub.models import DEV_ADMIN_ID
from topcoat_hub.project_store import ProjectStore


def test_the_context_keeps_a_fixed_shape() -> None:
    assert conversation_context.clean(None) is None
    assert conversation_context.clean({"module": "Bad Module!"}) is None
    got = conversation_context.clean(
        {
            "module": "page.project",
            "props": {"project": "p1", "Bad-Key": "x", "n": 3, "flag": True, "long": "y" * 500},
            "title": "  Projekte\n  und mehr ",
        }
    )
    assert got == {
        "module": "page.project",
        "props": {"project": "p1", "n": 3, "flag": True, "long": "y" * 120},
        "title": "Projekte und mehr",
    }


def _project(client: TestClient, user_id: uuid.UUID) -> tuple[str, str]:
    async def go() -> tuple[str, str]:
        store = ProjectStore(client.app.state.sessionmaker)
        project = await store.create_project(user_id, title="Hausbau")
        task = await store.create_node(user_id, project.id, kind="task", title="Dach decken")
        return str(project.id), str(task.id)

    return client.portal.call(go)


def _block(client: TestClient, user_id: uuid.UUID, ctx: dict) -> str:
    async def go() -> str:
        async with client.app.state.sessionmaker() as db:
            return await conversation_context.block(db, user_id, conversation_context.clean(ctx))

    return client.portal.call(go)


def test_a_conversation_keeps_where_it_was_started_and_names_only_the_persons_own_rows(
    client: TestClient,
) -> None:
    project, task = _project(client, DEV_ADMIN_ID)
    ctx = {
        "module": "page.project",
        "props": {"project": project, "node": task},
        "title": "Projekte",
    }
    r = client.post(
        "/api/conversations", json={"application": "project", "context": ctx}, headers=AUTH
    )
    assert r.status_code == 201, r.text
    cid = r.json()["id"]
    assert client.get(f"/api/conversations/{cid}", headers=AUTH).json()["context"] == ctx

    mine = _block(client, DEV_ADMIN_ID, ctx)
    assert "project «Hausbau»" in mine and "task «Dach decken»" in mine
    # someone else with the same ids gets no names, only the ids as the page sent them
    other_id, _ = make_user(TEST_DB_URL, "zoe")
    theirs = _block(client, other_id, ctx)
    assert "Hausbau" not in theirs and f"project={project}" in theirs


def test_a_context_the_api_does_not_accept_is_refused(client: TestClient) -> None:
    r = client.post(
        "/api/conversations",
        json={"context": {"module": "page.wiki", "props": {f"k{i}": "v" for i in range(9)}}},
        headers=AUTH,
    )
    assert r.status_code == 422


def test_a_check_is_told_to_read_the_open_page_itself(client: TestClient) -> None:
    project, _ = _project(client, DEV_ADMIN_ID)
    ctx = conversation_context.clean({"module": "page.project", "props": {"project": project}})

    async def go(for_task: bool) -> str:
        async with client.app.state.sessionmaker() as db:
            return await conversation_context.block(db, DEV_ADMIN_ID, ctx, for_task=for_task)

    assert "project tools" in client.portal.call(go, True)
    assert "project tools" not in client.portal.call(go, False)


def test_a_chat_at_a_document_window_gets_the_document_of_the_person_only(
    client: TestClient,
) -> None:
    """A report opens in a window of its own; Clipper's talk there reads it (no tool reads the
    Ablage), and only the person's own report."""
    from topcoat_hub.report_store import ReportStore

    async def make() -> str:
        async with client.app.state.sessionmaker() as db:
            r = await ReportStore(db, DEV_ADMIN_ID).create(
                title="Pixel-Animationen",
                question="q",
                language="de",
                depth="thorough",
                body="# Pixel\n\nAseprite und ComfyUI.",
                meta={},
            )
            await db.commit()
            return str(r.id)

    rid = client.portal.call(make)
    ctx = {"module": "page.document", "props": {"kind": "report", "id": rid}, "title": "Pixel"}
    mine = _block(client, DEV_ADMIN_ID, ctx)
    assert "research report «Pixel-Animationen»" in mine and "Aseprite und ComfyUI" in mine
    other_id, _ = make_user(TEST_DB_URL, "yuki")
    assert "Aseprite" not in _block(client, other_id, ctx)


def test_a_talk_at_a_document_belongs_to_the_application_that_made_it(client: TestClient) -> None:
    from topcoat_hub.report_store import ReportStore

    async def make() -> str:
        async with client.app.state.sessionmaker() as db:
            r = await ReportStore(db, DEV_ADMIN_ID).create(
                title="T", question="q", language="de", depth="thorough", body="b", meta={}
            )
            await db.commit()
            return str(r.id)

    rid = client.portal.call(make)
    ctx = {"module": "page.document", "props": {"kind": "report", "id": rid}, "title": "T"}
    r = client.post("/api/conversations", json={"context": ctx}, headers=AUTH)
    assert r.status_code == 201, r.text
    assert r.json()["application"] == "research"
    # a window that names its application keeps it
    r = client.post(
        "/api/conversations", json={"application": "project", "context": ctx}, headers=AUTH
    )
    assert r.json()["application"] == "project"
