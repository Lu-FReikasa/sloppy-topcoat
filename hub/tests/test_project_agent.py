"""The project agent's memory area and start path (brief PPH-01-planning-app, 01b).

Scope: project has its own top-level memory tag, which people who already have the memory tree
get as well; a run that belongs to project keeps that application on its task and sees the
project tools, and no other run does.
"""

import uuid
from typing import Any

from fastapi.testclient import TestClient
from sqlalchemy import select

from tests.conftest import TEST_DB_URL
from tests.users import make_user
from topcoat_hub.agent_service import ToolsAdapter
from topcoat_hub.agent_store import AgentStore
from topcoat_hub.memory_tags import PROGRAMS, ROOT_NAMES, ROOT_OF_PROGRAM, TagTree, default_tag
from topcoat_hub.models import DEV_ADMIN_ID, AgentTask, MemoryTag
from topcoat_hub.project_tools import PROJECT_NODE_ID, ProjectTools
from topcoat_hub.tools import Caller


def test_project_has_its_own_memory_root() -> None:
    assert "project" in PROGRAMS
    assert ROOT_OF_PROGRAM["project"] == "project"
    assert "project" in ROOT_NAMES
    assert default_tag("note", "project") == "project"


async def test_a_person_with_the_old_tree_gets_the_project_root(client: TestClient) -> None:
    user_id, _ = make_user(TEST_DB_URL, "older")
    sessionmaker = client.app.state.sessionmaker
    old = [name for name in ROOT_NAMES if name != "project"]
    async with sessionmaker() as db:
        for name in old:
            db.add(
                MemoryTag(
                    user_id=user_id,
                    name=name,
                    responsible="person",
                    description="",
                    created_by="seed",
                    sensitive=False,
                )
            )
        await db.commit()

    async with sessionmaker() as db:
        tags = await TagTree(db, user_id).ensure()
        await db.commit()
        roots = sorted(t.name for t in tags if t.parent_id is None)
    assert roots == sorted(ROOT_NAMES)

    async with sessionmaker() as db:
        # the second call adds nothing: the root is there once
        again = await TagTree(db, user_id).ensure()
        await db.commit()
        assert len([t for t in again if t.name == "project"]) == 1


async def test_a_project_task_keeps_its_application(client: TestClient) -> None:
    user_id, _ = make_user(TEST_DB_URL, "runner")
    async with client.app.state.sessionmaker() as db:
        store = AgentStore(db, user_id)
        task = await store.create_task("plan a small project", None, "gui", application="project")
        plain = await store.create_task("a chat question", None, "gui")
        await db.commit()
        project_id, plain_id = task.id, plain.id

    async with client.app.state.sessionmaker() as db:
        rows = {t.id: t.application for t in await db.scalars(select(AgentTask))}
    assert rows[project_id] == "project"
    assert rows[plain_id] is None


class _NoMachines:
    async def usable_tools(self, caller: Caller) -> list[dict[str, Any]]:
        return []


async def test_the_project_tools_come_only_with_the_project_application(
    client: TestClient,
) -> None:
    adapter = ToolsAdapter(
        _NoMachines(),  # type: ignore[arg-type]
        project=ProjectTools(client.app.state.sessionmaker),
    )
    planner = Caller(uuid.uuid4(), False, "agent", application="project")
    plain = Caller(uuid.uuid4(), False, "agent")

    offered = {t["capability"] for t in await adapter.available(planner)}
    assert "project.list" in offered
    assert not {t["capability"] for t in await adapter.available(plain)} & offered


def test_every_virtual_node_has_its_own_id() -> None:
    from topcoat_hub.github_access import GITHUB_NODE_ID
    from topcoat_hub.identity import IDENTITY_NODE_ID
    from topcoat_hub.inventory import INVENTORY_NODE_ID
    from topcoat_hub.knowledge import KNOWLEDGE_NODE_ID
    from topcoat_hub.project_tools import PROJECT_NODE_ID
    from topcoat_hub.topcoat_self import TOPCOAT_NODE_ID

    ids = [
        KNOWLEDGE_NODE_ID,
        INVENTORY_NODE_ID,
        IDENTITY_NODE_ID,
        TOPCOAT_NODE_ID,
        GITHUB_NODE_ID,
        PROJECT_NODE_ID,
    ]
    assert len(set(ids)) == len(ids)


class _Recorder:
    """Stands in for GitHub: any call to it is a bug in the routing."""

    def __init__(self) -> None:
        self.calls = 0

    async def offer(self, caller: Caller) -> list[dict[str, Any]]:
        return []

    async def run_tool(self, caller: Caller, capability: str, tool_input: dict[str, Any]) -> Any:
        self.calls += 1
        raise AssertionError("a project call reached GitHub")


async def test_a_project_call_never_reaches_another_virtual_node(client: TestClient) -> None:
    github = _Recorder()
    adapter = ToolsAdapter(
        _NoMachines(),  # type: ignore[arg-type]
        github=github,  # type: ignore[arg-type]
        project=ProjectTools(client.app.state.sessionmaker),
    )
    result = await adapter.invoke(
        Caller(DEV_ADMIN_ID, False, "agent", application="project"),
        PROJECT_NODE_ID,
        "project.list",
        {},
    )
    assert result["ok"] is True
    assert github.calls == 0


class _Machines:
    async def usable_tools(self, caller: Caller) -> list[dict[str, Any]]:
        return [
            {
                "node": "vm-docker",
                "node_id": "00000000-0000-0000-0000-000000000001",
                "capability": "docker.logs",
                "description": "Logs.",
                "input_schema": {},
            }
        ]


async def test_a_project_run_is_offered_only_the_project_tools(client: TestClient) -> None:
    adapter = ToolsAdapter(
        _Machines(),  # type: ignore[arg-type]
        project=ProjectTools(client.app.state.sessionmaker),
    )
    names = {
        t["capability"]
        for t in await adapter.available(
            Caller(DEV_ADMIN_ID, False, "agent", application="project")
        )
    }
    assert names and all(n.startswith("project.") for n in names)
    other = {t["capability"] for t in await adapter.available(Caller(DEV_ADMIN_ID, False, "agent"))}
    assert "docker.logs" in other
