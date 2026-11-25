"""The Projekte chat reads its own application and changes it only after a click (D15).

Scope: a chat run (``Caller.stage_writes``) gets a writing project call answered with "prepared"
and nothing changes; the person applies it with one click on that one row, or refuses it; a second
click, another person's click and an API key are refused; a stale revision is caught when the change
is prepared; the reading tools work as before; the tools of the application count for its chat.
"""

import uuid
from dataclasses import replace

import pytest
from fastapi.testclient import TestClient

from tests.conftest import AUTH, TEST_DB_URL
from tests.users import make_user
from topcoat_hub import tool_groups
from topcoat_hub.models import DEV_ADMIN_ID
from topcoat_hub.project_changes import ChangeStore, content_hash
from topcoat_hub.project_store import ProjectStore
from topcoat_hub.project_tools import ProjectTools
from topcoat_hub.tools import Caller, ToolError


def chat_run(task_id: uuid.UUID | None = None) -> Caller:
    return Caller(
        DEV_ADMIN_ID,
        False,
        "agent",
        application="project",
        stage_writes=True,
        task_id=task_id or uuid.uuid4(),
    )


@pytest.fixture
def tools(client: TestClient) -> ProjectTools:
    return ProjectTools(client.app.state.sessionmaker)


@pytest.fixture
def store(client: TestClient) -> ProjectStore:
    return ProjectStore(client.app.state.sessionmaker)


async def test_a_chat_run_prepares_a_change_and_writes_nothing(
    tools: ProjectTools, store: ProjectStore, client: TestClient
) -> None:
    project = await store.create_project(DEV_ADMIN_ID, title="Trial")
    task = uuid.uuid4()
    out = await tools.run_tool(
        chat_run(task),
        "project.node.create",
        {"project_id": str(project.id), "kind": "task", "title": "Tidy up"},
    )
    assert out["output"]["staged"] is True and out["output"]["applied"] is False
    assert await store.tree(DEV_ADMIN_ID, project.id) == []  # nothing was written
    rows = await ChangeStore(client.app.state.sessionmaker).for_task(DEV_ADMIN_ID, task)
    assert [(r.capability, r.status) for r in rows] == [("project.node.create", "pending")]
    assert rows[0].preview == {
        "target": "Trial",
        # a task the chat prepares gets the lines a run needs, as in a set (plan_checked)
        "fields": {
            "kind": "task",
            "title": "Tidy up",
            "body": "Zielanwendung: research\nErgebnisform: Ergebnis | Beleg\n- [ ] Tidy up",
        },
    }
    assert rows[0].content_hash == content_hash(rows[0].capability, rows[0].input)


async def test_reading_tools_still_answer_for_a_chat_run(
    tools: ProjectTools, store: ProjectStore
) -> None:
    project = await store.create_project(DEV_ADMIN_ID, title="Trial")
    got = await tools.run_tool(chat_run(), "project.list", {})
    assert [p["id"] for p in got["output"]["projects"]] == [str(project.id)]
    one = await tools.run_tool(chat_run(), "project.get", {"project_id": str(project.id)})
    assert one["output"]["project"]["title"] == "Trial"


async def test_a_stale_revision_is_caught_when_the_change_is_prepared(
    tools: ProjectTools, store: ProjectStore
) -> None:
    project = await store.create_project(DEV_ADMIN_ID, title="Trial")
    with pytest.raises(ToolError) as exc:
        await tools.run_tool(
            chat_run(),
            "project.update",
            {"project_id": str(project.id), "revision": 7, "status": "dropped"},
        )
    assert exc.value.status == 409


async def test_a_run_that_is_not_a_chat_writes_directly(
    tools: ProjectTools, store: ProjectStore
) -> None:
    project = await store.create_project(DEV_ADMIN_ID, title="Trial")
    direct = replace(chat_run(), stage_writes=False)
    out = await tools.run_tool(
        direct,
        "project.node.create",
        {"project_id": str(project.id), "kind": "topic", "title": "Written"},
    )
    assert out["output"]["title"] == "Written"
    assert len(await store.tree(DEV_ADMIN_ID, project.id)) == 1


async def _prepare(client: TestClient, project_id: str) -> str:
    tools = ProjectTools(client.app.state.sessionmaker)
    out = await tools.run_tool(
        chat_run(uuid.UUID(int=5)),
        "project.update",
        {"project_id": project_id, "revision": 1, "status": "paused"},
    )
    return str(out["output"]["change_id"])


async def test_a_click_applies_a_change_once(client: TestClient) -> None:
    store = ProjectStore(client.app.state.sessionmaker)
    project = await store.create_project(DEV_ADMIN_ID, title="Trial")
    change = await _prepare(client, str(project.id))

    listed = client.get(f"/api/project/changes?task_id={uuid.UUID(int=5)}", headers=AUTH).json()
    assert [(c["id"], c["status"]) for c in listed] == [(change, "pending")]

    done = client.post(f"/api/project/changes/{change}/apply", headers=AUTH)
    assert done.status_code == 200 and done.json()["status"] == "applied"
    assert (await store.get_project(DEV_ADMIN_ID, project.id)).status == "paused"

    again = client.post(f"/api/project/changes/{change}/apply", headers=AUTH)
    assert again.status_code == 409  # a second click finds it decided


async def test_a_refused_change_stays_unapplied(client: TestClient) -> None:
    store = ProjectStore(client.app.state.sessionmaker)
    project = await store.create_project(DEV_ADMIN_ID, title="Trial")
    change = await _prepare(client, str(project.id))
    refused = client.post(f"/api/project/changes/{change}/refuse", headers=AUTH)
    assert refused.json()["status"] == "refused"
    assert (await store.get_project(DEV_ADMIN_ID, project.id)).status == "active"
    assert client.post(f"/api/project/changes/{change}/apply", headers=AUTH).status_code == 409


async def test_a_change_that_no_longer_fits_is_marked_failed(client: TestClient) -> None:
    store = ProjectStore(client.app.state.sessionmaker)
    project = await store.create_project(DEV_ADMIN_ID, title="Trial")
    change = await _prepare(client, str(project.id))
    await store.update_project(DEV_ADMIN_ID, project.id, revision=1, title="Renamed")
    done = client.post(f"/api/project/changes/{change}/apply", headers=AUTH).json()
    assert done["status"] == "failed" and done["result"]["error"] == "conflict"
    assert (await store.get_project(DEV_ADMIN_ID, project.id)).status == "active"


async def test_another_person_cannot_see_or_decide_a_change(client: TestClient) -> None:
    store = ProjectStore(client.app.state.sessionmaker)
    project = await store.create_project(DEV_ADMIN_ID, title="Trial")
    change = await _prepare(client, str(project.id))
    _, other = make_user(TEST_DB_URL, "mallory")
    # a key never applies anything (only a click does), and the change is not hers to refuse
    assert client.post(f"/api/project/changes/{change}/apply", headers=other).status_code == 403
    assert client.post(f"/api/project/changes/{change}/refuse", headers=other).status_code == 404
    listed = client.get(f"/api/project/changes?task_id={uuid.UUID(int=5)}", headers=other)
    assert listed.json() == []


def test_the_tools_of_the_application_count_for_its_chat() -> None:
    assert "project" in tool_groups.KEYS
    assert tool_groups.group_of("project.get") == "project"


async def test_a_new_entry_shows_the_entry_it_goes_under_by_number_and_title(
    client: TestClient,
) -> None:
    # live 2026-11-17: "Darunter 44c2095b-…" under each of three new tasks
    store = ProjectStore(client.app.state.sessionmaker)
    project = await store.create_project(DEV_ADMIN_ID, title="Topcoat")
    topic = await store.create_node(DEV_ADMIN_ID, project.id, kind="topic", title="Imagetools")
    tools = ProjectTools(client.app.state.sessionmaker)
    await tools.run_tool(
        chat_run(uuid.UUID(int=6)),
        "project.node.create",
        {
            "project_id": str(project.id),
            "kind": "task",
            "title": "Recherche",
            "parent_id": str(topic.id),
        },
    )
    [listed] = client.get(f"/api/project/changes?task_id={uuid.UUID(int=6)}", headers=AUTH).json()
    assert listed["preview"]["parent"] == {"ref": "Topcoat 1", "title": "Imagetools"}
    refused = client.post(f"/api/project/changes/{listed['id']}/refuse", headers=AUTH).json()
    assert refused["preview"]["parent"]["title"] == "Imagetools"
    # another person's list never resolves an entry that is not theirs
    _, other = make_user(TEST_DB_URL, "eve")
    assert (
        client.get(f"/api/project/changes?task_id={uuid.UUID(int=6)}", headers=other).json() == []
    )


async def test_a_single_task_of_the_chat_passes_the_checks_of_a_plan(
    tools: ProjectTools, store: ProjectStore, client: TestClient
) -> None:
    """Live 2026-11-18: refused as a set (a point about node-amd in a research task), the planner
    staged the same tasks one by one with project.node.create, past every check."""
    from topcoat_hub.models import Node

    name = f"pc-{uuid.uuid4().hex[:6]}"
    async with client.app.state.sessionmaker() as db:
        db.add(
            Node(name=name, os="linux", arch="amd64", public_key="k", config={}, config_hash="h")
        )
        await db.commit()
    project = await store.create_project(DEV_ADMIN_ID, title="Machines")
    body = f"Zielanwendung: research\nErgebnisform: Frage | Antwort\n- [ ] Modelle auf {name}?"
    args = {"project_id": str(project.id), "kind": "task", "title": "Modelle", "body": body}
    with pytest.raises(ToolError, match="Zielanwendung: admin"):
        await tools.run_tool(chat_run(), "project.node.create", args)
    ok = {**args, "body": body.replace("research", "admin")}
    out = await tools.run_tool(chat_run(), "project.node.create", ok)
    assert out["output"]["staged"] is True
