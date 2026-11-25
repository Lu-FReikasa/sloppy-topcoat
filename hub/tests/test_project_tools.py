"""The project agent's tools (brief PPH-01-planning-app, 01b; rework 2026-10-27).

Scope: the tools are offered and run only for the project application; they go through the same
scoping and revision rules as the REST routes; there is no delete tool and no tool sets a scope;
a project of another person answers like a missing one.
"""

import uuid

import pytest
from fastapi.testclient import TestClient

from tests.conftest import TEST_DB_URL
from tests.users import make_user
from topcoat_hub.models import DEV_ADMIN_ID
from topcoat_hub.project_store import ProjectStore
from topcoat_hub.project_tools import CORE, META_SPECS, PROJECT_NODE_ID, SPECS, ProjectTools
from topcoat_hub.tools import Caller, ToolError

EXPECTED = {
    "project.list",
    "project.get",
    "project.update",
    "project.node.create",
    "project.node.update",
    "project.node.move",
    "project.report.add",
    "project.request.send",
    "project.order.draft",  # drafts an order; only the person's click or a mandate releases it
    # the reading tools of 04d (project_read_tools.py): none of them changes anything
    "project.node.search",
    "project.node.get",
    "project.agenda",
    "project.summary",
    "project.timeline",
    "project.node.history",
    "project.node.at",
    "project.node.diff",
    "project.label.list",
    "project.view.list",
    "project.trash.list",
    "project.filter.help",
    # 04g: the one writing tool that a chat does not stage, because it is the preparation
    "project.changeset.propose",
    "project.plan.propose",
    "project.batch.propose",
    # 04g: the single-step writing tools (project_write_tools.py); a chat only prepares them
    "project.create",
    "project.duplicate",
    "project.move",
    "project.node.complete",
    "project.node.claim",
    "project.node.reschedule",
    "project.relation.add",
    "project.relation.end",
    "project.label.add",
    "project.label.detach",
    "project.reminder.add",
    "project.reminder.cancel",
    "project.view.save",
}


def planner(user_id: uuid.UUID = DEV_ADMIN_ID) -> Caller:
    return Caller(user_id=user_id, admin=False, via="agent", application="project")


@pytest.fixture
def tools(client: TestClient) -> ProjectTools:
    return ProjectTools(client.app.state.sessionmaker)


@pytest.fixture
def store(client: TestClient) -> ProjectStore:
    return ProjectStore(client.app.state.sessionmaker)


async def test_only_the_project_application_is_offered_the_tools(tools: ProjectTools) -> None:
    assert await tools.offer(Caller(DEV_ADMIN_ID, True, "gui")) == []
    assert await tools.offer(Caller(DEV_ADMIN_ID, True, "agent", application="chat")) == []
    offered = await tools.offer(planner())
    # the core tier with its schemas and the two tools that reach the rest (04d, tiers)
    assert {t["capability"] for t in offered} == set(CORE) | set(META_SPECS)
    assert set(CORE) <= EXPECTED and len(CORE) == 12


def test_there_is_no_delete_and_no_scope_tool() -> None:
    assert set(SPECS) == EXPECTED
    # the trash can be read (project.trash.list) but not touched; nothing deletes or removes
    assert not [name for name in SPECS if "delete" in name or "remove" in name]
    assert not [name for name in SPECS if "purge" in name or "restore" in name]
    assert not [name for name in SPECS if "scope" in name]
    for spec in SPECS.values():
        assert "scope" not in spec["schema"]["properties"]


async def test_a_call_outside_the_project_application_is_refused(tools: ProjectTools) -> None:
    with pytest.raises(ToolError) as exc:
        await tools.run_tool(Caller(DEV_ADMIN_ID, True, "agent"), "project.list", {})
    assert exc.value.status == 404


async def test_list_and_get_show_the_tree_with_refs(
    tools: ProjectTools, store: ProjectStore
) -> None:
    project = await store.create_project(DEV_ADMIN_ID, title="Trial")
    topic = await store.create_node(DEV_ADMIN_ID, project.id, kind="topic", title="Repo")
    listed = await tools.run_tool(planner(), "project.list", {})
    assert [p["title"] for p in listed["output"]["projects"]] == ["Trial"]
    got = await tools.run_tool(planner(), "project.get", {"project_id": str(project.id)})
    assert got["output"]["project"]["id"] == str(project.id)
    node = got["output"]["nodes"][0]
    assert node["number"] == "1" and node["ref"] == "Trial 1" and node["id"] == str(topic.id)


async def test_a_node_is_created_and_numbered_by_the_tool(
    tools: ProjectTools, store: ProjectStore
) -> None:
    project = await store.create_project(DEV_ADMIN_ID, title="Tools")
    parent = await tools.run_tool(
        planner(),
        "project.node.create",
        {"project_id": str(project.id), "kind": "topic", "title": "Parent"},
    )
    child = await tools.run_tool(
        planner(),
        "project.node.create",
        {
            "project_id": str(project.id),
            "kind": "task",
            "title": "Child",
            "parent_id": parent["output"]["id"],
        },
    )
    assert child["output"]["number"] == "1.1"
    assert child["output"]["ref"] == "Tools 1.1"


async def test_a_move_changes_the_parent_and_keeps_the_number(
    tools: ProjectTools, store: ProjectStore
) -> None:
    project = await store.create_project(DEV_ADMIN_ID, title="Move tool")
    a = await store.create_node(DEV_ADMIN_ID, project.id, kind="topic", title="A")
    b = await store.create_node(DEV_ADMIN_ID, project.id, kind="topic", title="B")
    task = await store.create_node(DEV_ADMIN_ID, project.id, kind="task", title="T", parent_id=a.id)
    moved = await tools.run_tool(
        planner(),
        "project.node.move",
        {"node_id": str(task.id), "revision": task.revision, "parent_id": str(b.id)},
    )
    assert moved["output"]["number"] == "1.1"
    assert moved["output"]["parent_id"] == str(b.id)


async def test_a_stale_revision_from_the_agent_is_a_conflict(
    tools: ProjectTools, store: ProjectStore
) -> None:
    project = await store.create_project(DEV_ADMIN_ID, title="Stale tool")
    await store.update_project(DEV_ADMIN_ID, project.id, revision=1, description="changed")
    with pytest.raises(ToolError) as exc:
        await tools.run_tool(
            planner(),
            "project.update",
            {"project_id": str(project.id), "revision": 1, "description": "late"},
        )
    assert exc.value.status == 409


async def test_a_report_is_written_into_the_tree(tools: ProjectTools, store: ProjectStore) -> None:
    project = await store.create_project(DEV_ADMIN_ID, title="Reports tool")
    out = await tools.run_tool(
        planner(),
        "project.report.add",
        {
            "project_id": str(project.id),
            "report_kind": "question",
            "title": "Which node?",
            "body": "node-amd or node-nvidia",
        },
    )
    assert out["output"]["report_kind"] == "question"
    assert out["output"]["number"] == "1"


async def test_another_persons_project_answers_like_a_missing_one(
    tools: ProjectTools, store: ProjectStore
) -> None:
    project = await store.create_project(DEV_ADMIN_ID, title="Private tool")
    stranger, _ = make_user(TEST_DB_URL, "stranger tool")
    with pytest.raises(ToolError) as exc:
        await tools.run_tool(planner(stranger), "project.get", {"project_id": str(project.id)})
    missing = uuid.uuid4()
    with pytest.raises(ToolError) as gone:
        await tools.run_tool(planner(stranger), "project.get", {"project_id": str(missing)})
    assert exc.value.status == gone.value.status == 404
    assert exc.value.code == gone.value.code == "not_found"


async def test_the_project_node_id_is_fixed() -> None:
    assert uuid.UUID("00000000-0000-4000-8000-00000000a0d5") == PROJECT_NODE_ID


async def test_a_request_needs_a_grant_and_says_so_when_missing(
    tools: ProjectTools, store: ProjectStore
) -> None:
    project = await store.create_project(DEV_ADMIN_ID, title="Tool requests")
    args = {"project_id": str(project.id), "target": "research", "topic": "Look at it"}
    with pytest.raises(ToolError) as refused:
        await tools.run_tool(planner(), "project.request.send", args)
    assert refused.value.status == 422
    await store.set_scope(
        DEV_ADMIN_ID, project.id, revision=1, scope={"research": {"request_report": "free"}}
    )
    done = await tools.run_tool(planner(), "project.request.send", args)
    assert done["output"]["message"]["state"] == "sent"
    assert done["output"]["message"]["type"] == "project.request"
    assert done["output"]["ref"] == "Tool requests 1"


def test_the_request_tool_has_no_action_to_name() -> None:
    properties = SPECS["project.request.send"]["schema"]["properties"]
    assert "action" not in properties  # the grant is decided from the target, never named


async def test_the_offered_list_is_small_and_the_rest_is_one_step_away(
    tools: ProjectTools, store: ProjectStore
) -> None:
    import json

    from topcoat_hub.project_tools import all_tool_entries

    offered = await tools.offer(planner())
    assert len(json.dumps(offered)) < len(json.dumps(all_tool_entries())) * 0.6

    listing = (await tools.run_tool(planner(), "project.more.help", {}))["output"]
    names = {t["name"] for t in listing["tools"]}
    assert names == EXPECTED - set(CORE)
    assert all(t["does"] for t in listing["tools"])
    one = (await tools.run_tool(planner(), "project.more.help", {"name": "project.view.save"}))[
        "output"
    ]
    assert one["input_schema"] == SPECS["project.view.save"]["schema"]

    project = await store.create_project(DEV_ADMIN_ID, title="Tiered")
    task = await store.create_node(DEV_ADMIN_ID, project.id, kind="task", title="T")
    ran = await tools.run_tool(
        planner(),
        "project.more.run",
        {
            "name": "project.node.claim",
            "args": {"node_id": str(task.id), "revision": task.revision},
        },
    )
    assert ran["output"]["status"] == "in_progress"


async def test_a_call_through_more_run_is_checked_and_prepared_like_the_tool_itself(
    tools: ProjectTools, store: ProjectStore
) -> None:
    from dataclasses import replace

    project = await store.create_project(DEV_ADMIN_ID, title="Through")
    task = await store.create_node(DEV_ADMIN_ID, project.id, kind="task", title="T")
    args = {"node_id": str(task.id), "revision": task.revision}
    with pytest.raises(ToolError) as exc:  # the inner schema is checked
        await tools.run_tool(
            planner(), "project.more.run", {"name": "project.node.claim", "args": {"node_id": "x"}}
        )
    assert exc.value.status == 422 and "project.node.claim" in exc.value.message
    with pytest.raises(ToolError):  # only the extended tools, nothing else, and not itself
        await tools.run_tool(
            planner(), "project.more.run", {"name": "project.more.run", "args": {}}
        )
    chat = replace(planner(), stage_writes=True)
    out = await tools.run_tool(
        chat, "project.more.run", {"name": "project.node.claim", "args": args}
    )
    assert out["output"]["staged"] is True  # prepared, as the direct call would be
    assert (await store.get_node(DEV_ADMIN_ID, task.id)).status == "todo"


async def test_the_tree_comes_a_page_at_a_time_without_bodies(
    tools: ProjectTools, store: ProjectStore
) -> None:
    project = await store.create_project(DEV_ADMIN_ID, title="Pages")
    for i in range(14):
        await store.create_node(
            DEV_ADMIN_ID, project.id, kind="task", title=f"Task {i}", body="x" * 400
        )
    first = await tools.run_tool(planner(), "project.get", {"project_id": str(project.id)})
    out = first["output"]
    assert out["nodes_total"] == 14 and len(out["nodes"]) == 12 and out["next_from"] == 12
    assert "body" not in out["nodes"][0] and out["nodes"][0]["revision"] >= 1
    assert len(str(out)) < 3500  # the observation the model reads in one turn holds a page
    second = await tools.run_tool(
        planner(), "project.get", {"project_id": str(project.id), "from": 12}
    )
    assert [n["number"] for n in second["output"]["nodes"]] == ["13", "14"]
    assert second["output"]["next_from"] is None
