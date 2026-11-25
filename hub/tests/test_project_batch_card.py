"""The batch card (PPH-04, plan of 2026-11-08, S4): the planner proposes a batch, the chat only
prepares it, and only the person's click on the card starts it.

Scope: a card names open tasks of one project in their order and a limit of runs; a chat, a run or
an API key gets a prepared card and nothing starts; the apply of that card (the person's click)
starts the batch through the run service; a task that is not open refuses the card at once."""

import uuid
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi.testclient import TestClient

from topcoat_hub import clock
from topcoat_hub.models import DEV_ADMIN_ID as USER
from topcoat_hub.models import ProjectBatch, ProjectRelation
from topcoat_hub.project_changes import PENDING, ChangeStore
from topcoat_hub.project_runs import RunStore
from topcoat_hub.project_store import ProjectStore
from topcoat_hub.project_tools import BATCH_TOOL, ProjectTools
from topcoat_hub.tools import Caller, ToolError


class FakeRuns:
    """Records the one start the run service would take; the real service has its own tests."""

    def __init__(self) -> None:
        self.started: list[dict[str, Any]] = []

    async def create_batch(
        self, user_id: uuid.UUID, project_id: uuid.UUID, node_ids: list[uuid.UUID], max_runs: int
    ) -> SimpleNamespace:
        self.started.append({"project": project_id, "nodes": node_ids, "max_runs": max_runs})
        return SimpleNamespace(
            id=uuid.uuid4(),
            project_id=project_id,
            node_ids=[str(n) for n in node_ids],
            max_runs=max_runs,
            runs=0,
            status="active",
            stop_reason=None,
            created_at=None,
            stopped_at=None,
        )

    def nodes_for(self, targets: set[str]) -> list[str]:
        """The nodes of the rows: here one fixed row for ``dev``."""
        return ["geo", "pc"] if "dev" in targets else []


def chat(task_id: uuid.UUID | None = None) -> Caller:
    return Caller(
        user_id=USER,
        admin=False,
        via="agent",
        application="project",
        stage_writes=True,
        task_id=task_id,
    )


def click() -> Caller:
    """The apply of a prepared card: the signed-in person, no chat flag (api/project.py)."""
    return Caller(USER, False, "gui", application="project")


@pytest.fixture
def tools(client: TestClient) -> ProjectTools:
    tools = ProjectTools(client.app.state.sessionmaker)
    tools.runs = FakeRuns()
    return tools


@pytest.fixture
def store(client: TestClient) -> ProjectStore:
    return ProjectStore(client.app.state.sessionmaker)


async def _tasks(store: ProjectStore, n: int) -> tuple[uuid.UUID, list[str]]:
    project = await store.create_project(USER, title=f"Card {uuid.uuid4().hex[:10]}")
    ids = []
    for i in range(n):
        node = await store.create_node(
            USER, project.id, kind="task", title=f"Article {i}", body="just text"
        )
        ids.append(str(node.id))
    return project.id, ids


@pytest.mark.asyncio
async def test_a_chat_prepares_the_card_and_starts_nothing(
    client: TestClient, tools: ProjectTools, store: ProjectStore
) -> None:
    project_id, ids = await _tasks(store, 2)
    args = {"project_id": str(project_id), "node_ids": ids, "max_runs": 3}
    out = await tools.run_tool(chat(), BATCH_TOOL, args)

    assert out["output"]["staged"] is True and out["output"]["applied"] is False
    assert tools.runs.started == []
    changes = ChangeStore(client.app.state.sessionmaker)
    row = await changes.get(USER, uuid.UUID(out["output"]["change_id"]))
    assert row.status == PENDING and row.capability == BATCH_TOOL
    assert row.preview["fields"]["max_runs"] == 3
    assert len(row.preview["fields"]["tasks"]) == 2  # the tasks as the card shows them, in order
    # the application and mode of each task, in the same order (the card's badge)
    assert len(row.preview["fields"]["task_apps"]) == 2
    # the card names the nodes of the tasks' rows; these tasks name no application
    assert row.preview["fields"]["nodes"] == "-"
    project2 = await store.create_project(USER, title=f"Card {uuid.uuid4().hex[:10]}")
    dev = await store.create_node(
        USER,
        project2.id,
        kind="task",
        title="Dev",
        body="Zielanwendung: dev\nErgebnisform: A | B\n",
    )
    args2 = {"project_id": str(project2.id), "node_ids": [str(dev.id)], "max_runs": 1}
    out2 = await tools.run_tool(chat(), BATCH_TOOL, args2)
    row2 = await changes.get(USER, uuid.UUID(out2["output"]["change_id"]))
    assert row2.preview["fields"]["nodes"] == "geo, pc"
    assert row.preview["fields"]["task_apps"][0] == {"application": None, "mode": "lesen"}


@pytest.mark.asyncio
async def test_the_click_on_the_card_starts_the_batch_in_the_order_given(
    tools: ProjectTools, store: ProjectStore
) -> None:
    project_id, ids = await _tasks(store, 2)
    args = {"project_id": str(project_id), "node_ids": ids[::-1], "max_runs": 4}
    out = await tools.run_tool(click(), BATCH_TOOL, args)

    assert out["output"]["started"] is True
    started = tools.runs.started
    assert len(started) == 1 and started[0]["max_runs"] == 4
    assert [str(n) for n in started[0]["nodes"]] == ids[::-1]


@pytest.mark.asyncio
async def test_an_api_key_or_a_run_never_starts_a_batch(
    tools: ProjectTools, store: ProjectStore
) -> None:
    project_id, ids = await _tasks(store, 1)
    args = {"project_id": str(project_id), "node_ids": ids, "max_runs": 1}
    via_key = Caller(USER, False, "api", application="project")
    out = await tools.run_tool(via_key, BATCH_TOOL, args)
    assert out["output"]["staged"] is True
    assert tools.runs.started == []


@pytest.mark.asyncio
async def test_a_task_that_is_not_open_refuses_the_card_with_every_problem(
    tools: ProjectTools, store: ProjectStore
) -> None:
    project_id, ids = await _tasks(store, 2)
    runs = RunStore(store._sessionmaker)
    await runs.set_run(USER, uuid.UUID(ids[0]), status="in_review", task_id=None, reason="test")
    topic = await store.create_node(USER, project_id, kind="topic", title="A folder")
    args = {
        "project_id": str(project_id),
        "node_ids": [ids[0], str(topic.id)],
        "max_runs": 2,
    }
    with pytest.raises(ToolError) as exc:
        await tools.run_tool(chat(), BATCH_TOOL, args)
    assert exc.value.status == 422
    assert "not open" in exc.value.message and "not a task" in exc.value.message
    assert tools.runs.started == []


@pytest.mark.asyncio
async def test_the_card_reads_each_task_and_the_reason_of_a_blocked_one(
    client: TestClient, store: ProjectStore
) -> None:
    project_id, ids = await _tasks(store, 2)
    async with client.app.state.sessionmaker() as db:
        db.add(
            ProjectBatch(
                user_id=USER,
                project_id=project_id,
                node_ids=ids,
                max_runs=2,
                runs=0,
                status="active",
                created_at=clock.hub_now(),
                updated_at=clock.hub_now(),
            )
        )
        await db.commit()
    runs = client.app.state.project_runs
    batch = (await runs.batches(USER, project_id))[0]
    await RunStore(store._sessionmaker).set_run(
        USER, uuid.UUID(ids[0]), status="blocked", task_id=None, reason="row 1 has no value"
    )

    detail = await runs.detail(USER, batch.id)
    first, second = detail["tasks"]
    assert (first["status"], first["reason"]) == ("blocked", "row 1 has no value")
    assert (second["status"], second["reason"]) == ("todo", None)
    assert detail["status"] == "active" and detail["max_runs"] == 2


@pytest.mark.asyncio
async def test_the_card_names_what_a_task_waits_for_before_and_after_the_click(
    client: TestClient, tools: ProjectTools, store: ProjectStore
) -> None:
    project_id, ids = await _tasks(store, 2)
    first, second = ids
    async with client.app.state.sessionmaker() as db:  # the second waits for the first
        db.add(
            ProjectRelation(
                user_id=USER,
                application="project",
                node_id=uuid.UUID(second),
                other_id=uuid.UUID(first),
                kind="blocked_by",
                valid_from=clock.hub_now(),
            )
        )
        await db.commit()

    first_node = await store.get_node(USER, uuid.UUID(first))
    args = {"project_id": str(project_id), "node_ids": ids, "max_runs": 2}
    out = await tools.run_tool(chat(), BATCH_TOOL, args)
    tasks = out["output"]["preview"]["fields"]["tasks"]
    assert not tasks[0].endswith(")")
    assert tasks[1].endswith(f"(wartet auf {first_node.number})")

    async with client.app.state.sessionmaker() as db:
        db.add(
            ProjectBatch(
                user_id=USER,
                project_id=project_id,
                node_ids=ids,
                max_runs=2,
                runs=0,
                status="active",
                created_at=clock.hub_now(),
                updated_at=clock.hub_now(),
            )
        )
        await db.commit()
    runs = client.app.state.project_runs
    batch = (await runs.batches(USER, project_id))[0]
    detail = await runs.detail(USER, batch.id)
    assert detail["tasks"][0]["waits_for"] == []
    assert detail["tasks"][1]["waits_for"] == [
        {"number": first_node.number, "title": "Article 0", "status": "todo"}
    ]


def test_the_card_tool_is_offered_to_the_planner() -> None:
    from topcoat_hub.project_tools import CORE

    assert BATCH_TOOL in CORE
