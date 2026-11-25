"""The single-step writing tools of the project agent (PPH-04, 04g; project_write_tools.py).

Scope: each tool does what it says through the same store (so the rules and the Akte lines come
for free); a chat only prepares them; another person's rows answer like missing ones; no tool
deletes; a shift keeps the clock time over a change of summer time.
"""

import uuid
from dataclasses import replace
from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient

from tests.conftest import TEST_DB_URL
from tests.users import make_user
from topcoat_hub.models import DEV_ADMIN_ID
from topcoat_hub.project_store import ProjectStore
from topcoat_hub.project_tools import SPECS, ProjectTools
from topcoat_hub.project_write_tools import WRITE_NAMES
from topcoat_hub.tools import Caller, ToolError


def agent(user_id: uuid.UUID = DEV_ADMIN_ID) -> Caller:
    return Caller(user_id, False, "agent", application="project", task_id=uuid.uuid4())


@pytest.fixture
def tools(client: TestClient) -> ProjectTools:
    return ProjectTools(client.app.state.sessionmaker, "Europe/Berlin")


@pytest.fixture
def store(client: TestClient) -> ProjectStore:
    return ProjectStore(client.app.state.sessionmaker)


async def call(tools: ProjectTools, name: str, args: dict, caller: Caller | None = None) -> dict:
    return (await tools.run_tool(caller or agent(), name, args))["output"]


def test_no_writing_tool_deletes() -> None:
    for name in WRITE_NAMES:
        assert not any(w in name for w in ("delete", "remove", "purge", "restore", "trash"))
        assert "scope" not in SPECS[name]["schema"]["properties"]


async def test_create_and_move_a_project(tools: ProjectTools, store: ProjectStore) -> None:
    parent = await call(tools, "project.create", {"title": "Parent", "identifier": "PAR"})
    assert parent["identifier"] == "PAR" and parent["status"] == "active"
    child = await call(tools, "project.create", {"title": "Child", "parent_id": parent["id"]})
    assert child["parent_id"] == parent["id"]
    moved = await call(
        tools,
        "project.move",
        {"project_id": child["id"], "revision": child["revision"], "parent_id": None},
    )
    assert moved["parent_id"] is None


async def test_a_second_project_with_the_same_title_is_refused(tools: ProjectTools) -> None:
    await call(tools, "project.create", {"title": "Only one"})
    with pytest.raises(ToolError) as exc:
        await call(tools, "project.create", {"title": "Only one"})
    assert exc.value.status == 422


async def test_complete_and_claim(tools: ProjectTools, store: ProjectStore) -> None:
    project = await store.create_project(DEV_ADMIN_ID, title="Work")
    task = await store.create_node(DEV_ADMIN_ID, project.id, kind="task", title="Do it")
    claimed = await call(
        tools, "project.node.claim", {"node_id": str(task.id), "revision": task.revision}
    )
    assert claimed["status"] == "in_progress"
    with pytest.raises(ToolError) as exc:  # it is not 'todo' any more
        await call(
            tools,
            "project.node.claim",
            {"node_id": str(task.id), "revision": claimed["revision"]},
        )
    assert exc.value.status == 422
    done = await call(
        tools, "project.node.complete", {"node_id": str(task.id), "revision": claimed["revision"]}
    )
    assert done["status"] == "done"
    with pytest.raises(ToolError):  # done already
        await call(
            tools, "project.node.complete", {"node_id": str(task.id), "revision": done["revision"]}
        )


async def test_a_stale_revision_is_a_conflict(tools: ProjectTools, store: ProjectStore) -> None:
    project = await store.create_project(DEV_ADMIN_ID, title="Stale")
    task = await store.create_node(DEV_ADMIN_ID, project.id, kind="task", title="T")
    await store.update_node(DEV_ADMIN_ID, task.id, revision=task.revision, title="T2")
    with pytest.raises(ToolError) as exc:
        await call(
            tools, "project.node.complete", {"node_id": str(task.id), "revision": task.revision}
        )
    assert exc.value.status == 409


async def test_reschedule_by_dates_and_by_days_keeping_the_clock_time(
    tools: ProjectTools, store: ProjectStore
) -> None:
    project = await store.create_project(DEV_ADMIN_ID, title="Plan")
    task = await store.create_node(
        DEV_ADMIN_ID,
        project.id,
        kind="task",
        title="Ship",
        # 09:00 in Berlin, the day before the clocks go back (2026-10-25)
        fields={"due_at": "2026-10-24T09:00:00+02:00"},
    )
    shifted = await call(
        tools,
        "project.node.reschedule",
        {"node_id": str(task.id), "revision": task.revision, "shift_days": 2},
    )
    assert datetime.fromisoformat(shifted["due_at"]).astimezone(UTC) == datetime(
        2026, 10, 26, 8, 0, tzinfo=UTC
    )  # 09:00 on the 26th is 09:00 in winter time (+01:00), not 07:00 UTC
    explicit = await call(
        tools,
        "project.node.reschedule",
        {"node_id": str(task.id), "revision": shifted["revision"], "due_at": None},
    )
    assert explicit["due_at"] is None
    with pytest.raises(ToolError):  # both forms at once
        await call(
            tools,
            "project.node.reschedule",
            {
                "node_id": str(task.id),
                "revision": explicit["revision"],
                "shift_days": 1,
                "due_at": "2026-11-01T09:00:00+01:00",
            },
        )
    with pytest.raises(ToolError):  # nothing to shift
        await call(
            tools,
            "project.node.reschedule",
            {"node_id": str(task.id), "revision": explicit["revision"], "shift_days": 1},
        )


async def test_relations_labels_and_reminders(tools: ProjectTools, store: ProjectStore) -> None:
    project = await store.create_project(DEV_ADMIN_ID, title="Links")
    a = await store.create_node(DEV_ADMIN_ID, project.id, kind="task", title="A")
    b = await store.create_node(DEV_ADMIN_ID, project.id, kind="task", title="B")
    rel = await call(
        tools,
        "project.relation.add",
        {"node_id": str(a.id), "other_id": str(b.id), "kind": "blocked_by"},
    )
    with pytest.raises(ToolError):  # a loop of blockers
        await call(
            tools,
            "project.relation.add",
            {"node_id": str(b.id), "other_id": str(a.id), "kind": "blocked_by"},
        )
    assert (await call(tools, "project.relation.end", {"relation_id": rel["id"]}))["ended"]

    added = await call(tools, "project.label.add", {"node_id": str(a.id), "label": "Urgent"})
    assert [item["title"] for item in added["labels"]] == ["Urgent"]
    again = await call(tools, "project.label.add", {"node_id": str(b.id), "label": "urgent"})
    assert again["labels"][0]["id"] == added["labels"][0]["id"]  # the same label, case aside
    gone = await call(tools, "project.label.detach", {"node_id": str(a.id), "label": "Urgent"})
    assert gone["labels"] == []
    with pytest.raises(ToolError) as exc:
        await call(tools, "project.label.detach", {"node_id": str(a.id), "label": "Nothing"})
    assert exc.value.status == 404

    reminder = await call(
        tools,
        "project.reminder.add",
        {"node_id": str(a.id), "at": "2030-01-01T09:00:00+01:00"},
    )
    assert reminder["at"].startswith("2030-01-01")
    with pytest.raises(ToolError):  # a time without an offset is not a time
        await call(
            tools, "project.reminder.add", {"node_id": str(a.id), "at": "2030-01-01T09:00:00"}
        )
    assert (await call(tools, "project.reminder.cancel", {"reminder_id": reminder["id"]}))[
        "cancelled"
    ]


async def test_duplicate_copies_the_structure_not_the_history(
    tools: ProjectTools, store: ProjectStore
) -> None:
    source = await store.create_project(DEV_ADMIN_ID, title="Source")
    topic = await store.create_node(DEV_ADMIN_ID, source.id, kind="topic", title="Topic")
    await store.create_node(DEV_ADMIN_ID, source.id, kind="task", title="Task", parent_id=topic.id)
    await store.create_node(
        DEV_ADMIN_ID, source.id, kind="report", title="Finding", report_kind="note"
    )
    copy = await call(tools, "project.duplicate", {"project_id": str(source.id), "title": "Copy"})
    assert copy["title"] == "Copy" and copy["id"] != str(source.id)
    titles = [n.title for n in await store.tree(DEV_ADMIN_ID, uuid.UUID(copy["id"]))]
    assert titles == ["Topic", "Task"]  # the report stays behind
    kinds = [e.kind for e in await store.timeline(DEV_ADMIN_ID, uuid.UUID(copy["id"]))]
    assert "project.from_template" in kinds


async def test_a_saved_view_is_checked(tools: ProjectTools) -> None:
    saved = await call(
        tools,
        "project.view.save",
        {"title": "Reviews", "type": "board", "filter": "status = in_review"},
    )
    assert saved["type"] == "board"
    with pytest.raises(ToolError) as exc:
        await call(tools, "project.view.save", {"title": "Bad", "filter": "no such field = 1"})
    assert exc.value.status == 422


async def test_a_chat_only_prepares_them(
    tools: ProjectTools, store: ProjectStore, client: TestClient
) -> None:
    project = await store.create_project(DEV_ADMIN_ID, title="Prepared")
    task = await store.create_node(DEV_ADMIN_ID, project.id, kind="task", title="T")
    chat = replace(agent(), stage_writes=True)
    out = await call(
        tools, "project.node.claim", {"node_id": str(task.id), "revision": task.revision}, chat
    )
    assert out["staged"] is True and out["applied"] is False
    assert out["preview"]["target"] == "Prepared 1"
    assert (await store.get_node(DEV_ADMIN_ID, task.id)).status == "todo"  # nothing was written
    created = await call(tools, "project.create", {"title": "Not yet"}, chat)
    assert created["staged"] is True
    assert [p.title for p in await store.list_projects(DEV_ADMIN_ID)] == ["Prepared"]
    with pytest.raises(ToolError):  # a stale revision is caught when it is prepared
        await call(tools, "project.node.claim", {"node_id": str(task.id), "revision": 99}, chat)


async def test_another_persons_rows_answer_like_missing_ones(
    tools: ProjectTools, store: ProjectStore
) -> None:
    other, _ = make_user(TEST_DB_URL, "Other")
    project = await store.create_project(other, title="Theirs")
    task = await store.create_node(other, project.id, kind="task", title="T")
    for name, args in (
        ("project.node.claim", {"node_id": str(task.id), "revision": 1}),
        ("project.node.complete", {"node_id": str(task.id), "revision": 1}),
        ("project.duplicate", {"project_id": str(project.id), "title": "Mine now"}),
        ("project.move", {"project_id": str(project.id), "revision": 1, "parent_id": None}),
        ("project.label.add", {"node_id": str(task.id), "label": "x"}),
        ("project.reminder.add", {"node_id": str(task.id), "at": "2030-01-01T09:00:00+01:00"}),
    ):
        with pytest.raises(ToolError) as exc:
            await call(tools, name, args)
        assert exc.value.status == 404, name


def test_the_model_reads_the_change_set_tool_whole() -> None:
    """The description of a tool reaches the model cut to a number of characters. For the change
    set tool the cut at 400 took away the operations, their keys and the examples."""
    from topcoat_hub import agent_prompts, project_tools

    defs, index = agent_prompts.native_tools(project_tools.tool_entries())
    seen = {
        index[d["function"]["name"]]: d["function"]["description"]
        for d in defs
        if d["function"]["name"] in index
    }
    text = seen["project.changeset.propose"]
    assert (
        "set_status" in text
        and "`node` in an operation, not `node_id`" in text
        and "Search before you create." in text
    )
    assert "never as an addition to another change" in seen["project.node.complete"]
    # other than project tools stay short
    long = {
        "node_id": "n",
        "node": "tools-a",
        "capability": "shell.read",
        "description": "x " * 600,
        "input_schema": {},
    }
    out, _ = agent_prompts.native_tools([long])
    assert len(out[0]["function"]["description"]) <= agent_prompts.DESCRIPTION_CHARS
