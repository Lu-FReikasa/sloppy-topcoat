"""Topcoat's own wiki (wiki_store.py, api/wiki.py): a project's document is taken over by a click
(a new page, then new versions; each is the page from then on), read by nobody else; the Ablage
does not see the wiki's reports and the knowledge base does not index them."""

import uuid
from typing import Any

import pytest
from fastapi.testclient import TestClient

from tests.conftest import AUTH, TEST_DB_URL
from tests.users import make_user
from topcoat_hub.models import DEV_ADMIN_ID
from topcoat_hub.project_store import ProjectStore
from topcoat_hub.tool_groups import group_of
from topcoat_hub.tools import Caller, ToolError
from topcoat_hub.ui.seeds import BUILTIN_TEMPLATES
from topcoat_hub.wiki_store import OTHER, WikiStore, made, page_title, section_of
from topcoat_hub.wiki_tools import WIKI_NODE_ID

BODY = "# B12 Wissensbasis\n\n_Erzeugt aus dem Code des Hub-Builds `abc123` am 2026-11-12; x._\n"


def document(
    client: TestClient, user_id: uuid.UUID = DEV_ADMIN_ID, body: str = BODY, task="B12 Wissensbasis"
) -> tuple[str, str]:
    """A project with a task and a document below it; the document's id and "project id/task id"."""

    async def go() -> tuple[str, str]:
        store = ProjectStore(client.app.state.sessionmaker)
        # a project per document (a project's title is unique per person)
        project = await store.create_project(user_id, title=f"Topcoat-Doku {task}")
        parent = await store.create_node(
            user_id, project.id, kind="task", title=task, body="Programm: doc_section"
        )
        doc = await store.create_node(
            user_id, project.id, kind="report", title=f"Dokument: {task}", body=body,
            parent_id=parent.id, report_kind="document",
        )  # fmt: skip
        return str(doc.id), f"{project.id}/{parent.id}"

    return client.portal.call(go)


def another_document(client: TestClient, where: str, body: str) -> str:
    """A second document below the same task (``where``: project id/task id)."""
    project_id, task_id = (uuid.UUID(x) for x in where.split("/"))

    async def go() -> str:
        store = ProjectStore(client.app.state.sessionmaker)
        doc = await store.create_node(
            DEV_ADMIN_ID, project_id, kind="report", title="Dokument: B12 Wissensbasis",
            body=body, parent_id=task_id, report_kind="document",
        )  # fmt: skip
        return str(doc.id)

    return client.portal.call(go)


def test_names_and_sections() -> None:
    assert section_of("B12 Wissensbasis") == "B Hub"
    assert section_of("C3 Aktionen") == "C Node"
    assert section_of("B5a Gedächtnis") == "B Hub"
    assert section_of("D3b Desktop") == "D Oberfläche und Desktop"
    assert section_of("Ohne Nummer") == OTHER
    assert section_of("Z1 Unbekannt") == OTHER
    assert page_title("Dokument: B1 Gateway") == "B1 Gateway"
    assert made(BODY) == {"build": "abc123", "date": "2026-11-12"}
    assert made("no note") == {}


def test_take_over_twice_makes_version_two_and_each_take_over_is_the_page(
    client: TestClient,
) -> None:
    doc, where = document(client)
    preview = client.get(f"/api/project/nodes/{doc}/wiki", headers=AUTH).json()
    assert preview["path"] == "B Hub/B12 Wissensbasis"
    assert (preview["new_page"], preview["version"], preview["unchanged"]) == (True, 1, False)
    assert client.get("/api/wiki/pages", headers=AUTH).json()["sections"] == []  # changed nothing

    r = client.post(f"/api/project/nodes/{doc}/wiki", headers=AUTH)
    assert r.status_code == 201, r.text
    page = r.json()
    assert (page["version"], page["current_version"]) == (1, 1)
    # the same text again: refused, nothing to take over
    assert client.get(f"/api/project/nodes/{doc}/wiki", headers=AUTH).json()["unchanged"] is True
    assert client.post(f"/api/project/nodes/{doc}/wiki", headers=AUTH).status_code == 409
    shown = client.get(f"/api/wiki/pages/{page['id']}", headers=AUTH).json()
    assert shown["shown"]["meta"]["build"] == "abc123" and "released" not in shown
    # there is no release step (the maintainer, 2026-11-12)
    release = client.post(f"/api/wiki/pages/{page['id']}/release", headers=AUTH)
    assert release.status_code in (404, 405)

    newer = another_document(client, where, BODY + "\nNeuer Absatz.\n")
    preview = client.get(f"/api/project/nodes/{newer}/wiki", headers=AUTH).json()
    assert (preview["new_page"], preview["version"], preview["page_id"]) == (False, 2, page["id"])
    r = client.post(f"/api/project/nodes/{newer}/wiki", headers=AUTH)
    assert r.json()["version"] == 2
    shown = client.get(f"/api/wiki/pages/{page['id']}", headers=AUTH).json()
    assert shown["current_version"] == 2 and "Neuer Absatz" in shown["shown"]["body"]
    assert len(shown["versions"]) == 2
    old = client.get(f"/api/wiki/pages/{page['id']}?version=1", headers=AUTH).json()
    assert "Neuer Absatz" not in old["shown"]["body"]
    diff = client.get(f"/api/wiki/pages/{page['id']}/diff?old=1&new=2", headers=AUTH).json()
    assert diff["added"] >= 1

    tree = client.get("/api/wiki/pages", headers=AUTH).json()["sections"]
    assert [s["name"] for s in tree] == ["B Hub"] and len(tree[0]["pages"]) == 1


def test_only_a_document_is_taken_over(client: TestClient) -> None:
    async def go() -> str:
        store = ProjectStore(client.app.state.sessionmaker)
        project = await store.create_project(DEV_ADMIN_ID, title="P")
        node = await store.create_node(
            DEV_ADMIN_ID, project.id, kind="report", title="A fact", report_kind="fact"
        )
        return str(node.id)

    fact = client.portal.call(go)
    r = client.get(f"/api/project/nodes/{fact}/wiki", headers=AUTH)
    assert r.status_code == 422 and r.json()["detail"]["code"] == "not_a_document"


def test_nobody_else_sees_a_page_and_the_ablage_does_not_list_it(client: TestClient) -> None:
    doc, _ = document(client)
    page = client.post(f"/api/project/nodes/{doc}/wiki", headers=AUTH).json()
    _, other = make_user(TEST_DB_URL, "Other")
    assert client.get("/api/wiki/pages", headers=other).json() == {"sections": [], "categories": []}
    assert client.get(f"/api/wiki/pages/{page['id']}", headers=other).status_code == 404
    assert client.get(f"/api/project/nodes/{doc}/wiki", headers=other).status_code == 404
    assert client.post(f"/api/project/nodes/{doc}/wiki", headers=other).status_code == 404

    # the Ablage of the owner neither lists the page's report nor changes it
    listed = client.get("/api/reports", headers=AUTH).json()["reports"]
    assert all(r["application"] != "wiki" for r in listed)

    async def report_id() -> str:
        async with client.app.state.sessionmaker() as db:
            found = await WikiStore(db, DEV_ADMIN_ID).page(uuid.UUID(page["id"]))
            return str(found.report.id)

    rid = client.portal.call(report_id)
    assert client.get(f"/api/reports/{rid}", headers=AUTH).status_code == 404
    r = client.post(f"/api/reports/{rid}/status", headers=AUTH, json={"status": "draft"})
    assert r.status_code == 404


def test_a_page_is_not_indexed_in_the_knowledge_base(client: TestClient) -> None:
    doc, _ = document(client)
    client.post(f"/api/project/nodes/{doc}/wiki", headers=AUTH)
    index: Any = client.app.state.report_index
    assert client.portal.call(index.sync_user, DEV_ADMIN_ID) == 0


def caller(application: str | None) -> Caller:
    return Caller(DEV_ADMIN_ID, True, "agent", application)


def capabilities(client: TestClient, application: str | None) -> set[str]:
    tools = client.portal.call(client.app.state.agents.tools.available, caller(application))
    return {t["capability"] for t in tools}


def invoke(client: TestClient, application: str | None, capability: str, args: dict) -> dict:
    """The tool's output. The answer has the envelope of a tool node (``ok``, ``output``): without
    it the agent loop showed the model ``null`` (live, 2026-11-12)."""
    answer = client.portal.call(
        client.app.state.agents.tools.invoke, caller(application), WIKI_NODE_ID, capability, args
    )
    assert answer["ok"] is True and answer["bytes"] > 0
    return answer["output"]


@pytest.mark.chat_wiki
def test_projekte_verwaltung_chat_and_entwicklung_read_the_wiki_in_its_current_version(
    client: TestClient,
) -> None:
    # Entwicklung since 2026-11-17: its task runs survey the code the wiki describes
    # Research since 2026-11-18: a research about Topcoat reads its documentation
    for app in ("project", "admin", "chat", "dev", "research"):
        assert {"wiki.list", "wiki.read"} <= capabilities(client, app), app
    for app in (None,):
        assert not {"wiki.list", "wiki.read"} & capabilities(client, app), app
        with pytest.raises(ToolError):
            invoke(client, app, "wiki.list", {})

    assert invoke(client, "project", "wiki.list", {})["pages"] == []
    doc, where = document(client)
    client.post(f"/api/project/nodes/{doc}/wiki", headers=AUTH)
    out = invoke(client, "admin", "wiki.read", {"path": "B Hub/B12 Wissensbasis"})
    assert out["version"] == 1 and out["build"] == "abc123" and out["updated_at"]

    newer = another_document(client, where, BODY + "\nZweite Fassung.\n")
    client.post(f"/api/project/nodes/{newer}/wiki", headers=AUTH)
    listed = invoke(client, "admin", "wiki.list", {})["pages"]
    assert [(p["path"], p["version"]) for p in listed] == [("B Hub/B12 Wissensbasis", 2)]
    out = invoke(client, "project", "wiki.read", {"path": "B Hub/B12 Wissensbasis"})
    assert out["version"] == 2 and "Zweite Fassung" in out["content"]
    # a small model asks by number or title
    for asked in ("B12", "b12 wissensbasis"):
        out = invoke(client, "project", "wiki.read", {"path": asked})
        assert out["path"] == "B Hub/B12 Wissensbasis", asked
    with pytest.raises(ToolError) as missing:
        invoke(client, "project", "wiki.read", {"path": "B13"})
    assert "B Hub/B12 Wissensbasis" in missing.value.message


def test_the_seeds_give_the_wiki_group_to_projekte_verwaltung_chat_and_entwicklung() -> None:
    groups = {t["key"]: t["agent"]["tool_scope"]["groups"] for t in BUILTIN_TEMPLATES}
    # Chat since 2026-11-12: Clipper at the Wiki window talks in Chat and reads its page
    # Entwicklung since 2026-11-17: its task runs survey the code the wiki describes
    # Research since 2026-11-18: a research about Topcoat reads its documentation
    for key in ("project", "admin", "chat", "dev", "research"):
        assert "wiki" in groups[key], key
    assert group_of("wiki.read") == "wiki"


def test_a_check_that_names_only_the_notes_still_reads_the_wiki() -> None:
    """Live 2026-11-12: "Was steht im Wiki zu B12?" made the order pick the notes only, and the
    run searched the knowledge base. The wiki is read with every check where it is offered."""
    from topcoat_hub.check_order import Order, resolve

    available = [
        {"node": "knowledge", "capability": "knowledge.search"},
        {"node": "project", "capability": "project.node.get"},
        {"node": "wiki", "capability": "wiki.read"},
    ]
    order = resolve({"target": "any", "tools": ["notes", "project"], "goal": "B12"}, available)
    assert isinstance(order, Order) and "wiki" in order.tools
    without = resolve({"target": "any", "tools": ["notes"], "goal": "x"}, available[:2])
    assert isinstance(without, Order) and "wiki" not in without.tools


def test_the_person_orders_pages_in_folders_and_categories_and_a_take_over_keeps_the_place(
    client: TestClient,
) -> None:
    doc, where = document(client)
    page = client.post(f"/api/project/nodes/{doc}/wiki", headers=AUTH).json()
    url = f"/api/wiki/pages/{page['id']}"

    r = client.patch(
        url,
        json={"folder": " B Hub / Wissen ", "categories": ["Entwurf", "entwurf", " Hub "]},
        headers=AUTH,
    )
    assert r.status_code == 200, r.text
    assert (r.json()["folder"], r.json()["path"]) == (
        "B Hub/Wissen",
        "B Hub/Wissen/B12 Wissensbasis",
    )
    assert r.json()["categories"] == ["Entwurf", "Hub"]
    tree = client.get("/api/wiki/pages", headers=AUTH).json()
    assert [s["name"] for s in tree["sections"]] == ["B Hub/Wissen"]
    assert tree["categories"] == ["Entwurf", "Hub"]

    # the next take-over of the same task lands on the moved page, as version 2
    second = another_document(client, where, BODY + "\nmehr\n")
    preview = client.get(f"/api/project/nodes/{second}/wiki", headers=AUTH).json()
    assert (preview["path"], preview["new_page"], preview["version"]) == (
        "B Hub/Wissen/B12 Wissensbasis", False, 2,
    )  # fmt: skip

    # a folder is renamed with what is in it
    r = client.post(
        "/api/wiki/folders/rename", json={"old": "B Hub", "new": "Hub/Kern"}, headers=AUTH
    )
    assert (r.status_code, r.json()) == (200, {"moved": 1})
    assert client.get(url, headers=AUTH).json()["folder"] == "Hub/Kern/Wissen"


@pytest.mark.parametrize(
    "body",
    [
        {"folder": "/ /"},
        {"folder": "a/b/c/d/e/f/g"},
        {"folder": "x" * 61},
        {"title": "a/b"},
        {"categories": ["x" * 41]},
        {"categories": [f"c{i}" for i in range(13)]},
    ],
)
def test_a_folder_title_or_category_the_tree_cannot_hold_is_refused(
    client: TestClient, body: dict[str, Any]
) -> None:
    doc, _ = document(client)
    page = client.post(f"/api/project/nodes/{doc}/wiki", headers=AUTH).json()
    r = client.patch(f"/api/wiki/pages/{page['id']}", json=body, headers=AUTH)
    assert r.status_code == 422, r.text


def test_two_pages_cannot_share_a_place_and_a_folder_cannot_go_into_itself(
    client: TestClient,
) -> None:
    one = client.post(f"/api/project/nodes/{document(client)[0]}/wiki", headers=AUTH).json()
    other_doc, _ = document(client, task="B1 Gateway")
    other = client.post(f"/api/project/nodes/{other_doc}/wiki", headers=AUTH).json()
    r = client.patch(
        f"/api/wiki/pages/{other['id']}", json={"title": "B12 Wissensbasis"}, headers=AUTH
    )
    assert r.status_code == 409
    assert one["path"] == "B Hub/B12 Wissensbasis"
    r = client.post(
        "/api/wiki/folders/rename", json={"old": "B Hub", "new": "B Hub/x"}, headers=AUTH
    )
    assert r.status_code == 422


def test_nobody_else_moves_a_page(client: TestClient) -> None:
    doc, _ = document(client)
    page = client.post(f"/api/project/nodes/{doc}/wiki", headers=AUTH).json()
    _, other = make_user(TEST_DB_URL, "zoe")
    r = client.patch(f"/api/wiki/pages/{page['id']}", json={"folder": "Fremd"}, headers=other)
    assert r.status_code == 404
