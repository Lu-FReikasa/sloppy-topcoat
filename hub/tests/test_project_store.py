"""The Projekte store against PostgreSQL (brief PPH-01-planning-app; rework 2026-10-27).

Needs the test database (see ``conftest.py``). Scope: the store filters by person and application;
titles are unique per person; numbers are given once and never change; a move changes the parent
only; revisions refuse stale writes; the tree rules hold on write; the scope is the person's.
"""

import uuid

import pytest
from sqlalchemy import create_engine, text

from tests.conftest import TEST_DB_URL
from tests.users import make_user
from topcoat_hub.message_service import MessageService
from topcoat_hub.models import DEV_ADMIN_ID
from topcoat_hub.project_store import Conflict, Invalid, NotFound, ProjectStore


@pytest.fixture
def store(client) -> ProjectStore:  # type: ignore[no-untyped-def]
    return ProjectStore(client.app.state.sessionmaker)


async def test_create_and_read_a_project(store: ProjectStore) -> None:
    project = await store.create_project(DEV_ADMIN_ID, title="Topcoat", description="Dev")
    again = await store.get_project(DEV_ADMIN_ID, project.id)
    assert again.title == "Topcoat"
    assert again.description == "Dev"
    assert again.application == "project"
    assert again.scope == {}
    assert again.revision == 1


async def test_titles_are_unique_per_person(store: ProjectStore) -> None:
    await store.create_project(DEV_ADMIN_ID, title="Home-Lab")
    with pytest.raises(Invalid):
        await store.create_project(DEV_ADMIN_ID, title="Home-Lab")
    other, _ = make_user(TEST_DB_URL, "second")
    assert (await store.create_project(other, title="Home-Lab")).title == "Home-Lab"


async def test_a_blank_title_is_refused(store: ProjectStore) -> None:
    with pytest.raises(Invalid):
        await store.create_project(DEV_ADMIN_ID, title="   ")


async def test_another_person_sees_nothing(store: ProjectStore) -> None:
    project = await store.create_project(DEV_ADMIN_ID, title="Private")
    stranger = uuid.uuid4()
    with pytest.raises(NotFound):
        await store.get_project(stranger, project.id)
    assert await store.list_projects(stranger) == []


async def test_a_row_of_another_application_is_invisible(store: ProjectStore) -> None:
    project_id = uuid.uuid4()
    engine = create_engine(TEST_DB_URL)
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO projects (id, user_id, application, title, description,"
                " status, scope, next_child, revision) VALUES (:id, :u, 'chat', 'x', '',"
                " 'active', '{}', 0, 1)"
            ),
            {"id": project_id, "u": DEV_ADMIN_ID},
        )
    with pytest.raises(NotFound):
        await store.get_project(DEV_ADMIN_ID, project_id)
    assert all(p.id != project_id for p in await store.list_projects(DEV_ADMIN_ID))


async def test_topics_and_tasks_are_numbered_below_their_parent(store: ProjectStore) -> None:
    project = await store.create_project(DEV_ADMIN_ID, title="Numbers")
    first = await store.create_node(DEV_ADMIN_ID, project.id, kind="topic", title="Repo")
    second = await store.create_node(DEV_ADMIN_ID, project.id, kind="topic", title="Live")
    child = await store.create_node(
        DEV_ADMIN_ID, project.id, kind="task", title="Check", parent_id=first.id
    )
    grandchild = await store.create_node(
        DEV_ADMIN_ID, project.id, kind="task", title="Deep", parent_id=child.id
    )
    assert [first.number, second.number] == ["1", "2"]
    assert child.number == "1.1"
    assert grandchild.number == "1.1.1"
    assert [n.number for n in await store.tree(DEV_ADMIN_ID, project.id)] == [
        "1",
        "1.1",
        "1.1.1",
        "2",
    ]


async def test_numbers_are_in_numeric_order(store: ProjectStore) -> None:
    project = await store.create_project(DEV_ADMIN_ID, title="Order")
    for i in range(12):
        await store.create_node(DEV_ADMIN_ID, project.id, kind="topic", title=f"t{i}")
    numbers = [n.number for n in await store.tree(DEV_ADMIN_ID, project.id)]
    assert numbers[8:11] == ["9", "10", "11"]


async def test_a_move_keeps_the_number(store: ProjectStore) -> None:
    project = await store.create_project(DEV_ADMIN_ID, title="Move")
    topic = await store.create_node(DEV_ADMIN_ID, project.id, kind="topic", title="A")
    task = await store.create_node(
        DEV_ADMIN_ID, project.id, kind="task", title="B", parent_id=topic.id
    )
    other = await store.create_node(DEV_ADMIN_ID, project.id, kind="topic", title="C")
    moved = await store.move_node(DEV_ADMIN_ID, task.id, revision=task.revision, parent_id=other.id)
    assert moved.number == "1.1"
    assert moved.parent_id == other.id
    # a number is never reused by a new node under the old parent
    fresh = await store.create_node(
        DEV_ADMIN_ID, project.id, kind="task", title="D", parent_id=topic.id
    )
    assert fresh.number == "1.2"


async def test_a_report_takes_a_number_and_cannot_have_children(store: ProjectStore) -> None:
    project = await store.create_project(DEV_ADMIN_ID, title="Reports")
    report = await store.create_node(
        DEV_ADMIN_ID, project.id, kind="report", title="Q", report_kind="question"
    )
    assert report.number == "1" and report.report_kind == "question" and report.status is None
    with pytest.raises(Invalid):
        await store.create_node(
            DEV_ADMIN_ID, project.id, kind="task", title="x", parent_id=report.id
        )


async def test_a_report_kind_is_needed_for_a_report_and_refused_otherwise(
    store: ProjectStore,
) -> None:
    project = await store.create_project(DEV_ADMIN_ID, title="Kinds")
    with pytest.raises(Invalid):
        await store.create_node(DEV_ADMIN_ID, project.id, kind="report", title="x")
    with pytest.raises(Invalid):
        await store.create_node(
            DEV_ADMIN_ID, project.id, kind="topic", title="x", report_kind="note"
        )


async def test_only_a_task_has_a_status(store: ProjectStore) -> None:
    project = await store.create_project(DEV_ADMIN_ID, title="Status")
    task = await store.create_node(DEV_ADMIN_ID, project.id, kind="task", title="t")
    assert task.status == "todo"
    done = await store.update_node(DEV_ADMIN_ID, task.id, revision=task.revision, status="done")
    assert done.status == "done"
    topic = await store.create_node(DEV_ADMIN_ID, project.id, kind="topic", title="T")
    with pytest.raises(Invalid):
        await store.update_node(DEV_ADMIN_ID, topic.id, revision=topic.revision, status="done")


async def test_a_parent_in_another_project_is_refused(store: ProjectStore) -> None:
    one = await store.create_project(DEV_ADMIN_ID, title="One")
    two = await store.create_project(DEV_ADMIN_ID, title="Two")
    foreign = await store.create_node(DEV_ADMIN_ID, two.id, kind="topic", title="x")
    with pytest.raises(Invalid):
        await store.create_node(DEV_ADMIN_ID, one.id, kind="task", title="y", parent_id=foreign.id)


async def test_a_loop_of_parents_is_refused(store: ProjectStore) -> None:
    project = await store.create_project(DEV_ADMIN_ID, title="Loop")
    a = await store.create_node(DEV_ADMIN_ID, project.id, kind="topic", title="a")
    b = await store.create_node(DEV_ADMIN_ID, project.id, kind="topic", title="b", parent_id=a.id)
    with pytest.raises(Invalid):
        await store.move_node(DEV_ADMIN_ID, a.id, revision=a.revision, parent_id=b.id)


async def test_a_dependency_loop_is_refused(store: ProjectStore) -> None:
    project = await store.create_project(DEV_ADMIN_ID, title="Deps")
    a = await store.create_node(DEV_ADMIN_ID, project.id, kind="task", title="a")
    b = await store.create_node(DEV_ADMIN_ID, project.id, kind="task", title="b", depends_on=[a.id])
    with pytest.raises(Invalid):
        await store.update_node(DEV_ADMIN_ID, a.id, revision=a.revision, depends_on=[b.id])


async def test_a_stale_revision_is_a_conflict_and_changes_nothing(store: ProjectStore) -> None:
    project = await store.create_project(DEV_ADMIN_ID, title="Stale")
    await store.update_project(DEV_ADMIN_ID, project.id, revision=1, description="new")
    with pytest.raises(Conflict):
        await store.update_project(DEV_ADMIN_ID, project.id, revision=1, description="late")
    assert (await store.get_project(DEV_ADMIN_ID, project.id)).description == "new"


async def test_a_question_is_answered_and_nothing_else_is(store: ProjectStore) -> None:
    project = await store.create_project(DEV_ADMIN_ID, title="Answer")
    question = await store.create_node(
        DEV_ADMIN_ID, project.id, kind="report", title="Which node?", report_kind="question"
    )
    note = await store.create_node(
        DEV_ADMIN_ID, project.id, kind="report", title="Note", report_kind="note"
    )
    answered = await store.answer_question(DEV_ADMIN_ID, question.id, "node-amd")
    assert answered.answer == "node-amd" and answered.answered_at is not None
    with pytest.raises(Invalid):
        await store.answer_question(DEV_ADMIN_ID, note.id, "no")


async def test_a_scope_is_stored_normalized_and_only_set_by_the_person(
    store: ProjectStore,
) -> None:
    project = await store.create_project(DEV_ADMIN_ID, title="Scoped")
    scoped = await store.set_scope(
        DEV_ADMIN_ID,
        project.id,
        revision=project.revision,
        scope={"dev": {"create_branch": "confirm", "request_report": "free"}, "research": {}},
    )
    assert scoped.scope == {"dev": {"create_branch": "confirm", "request_report": "free"}}
    assert scoped.revision == project.revision + 1


async def test_a_scope_cannot_grant_what_is_never_allowed(store: ProjectStore) -> None:
    project = await store.create_project(DEV_ADMIN_ID, title="Never")
    with pytest.raises(Invalid):
        await store.set_scope(DEV_ADMIN_ID, project.id, revision=1, scope={"dev": {"push": "free"}})
    with pytest.raises(Invalid):
        await store.set_scope(
            DEV_ADMIN_ID, project.id, revision=1, scope={"admin": {"deploy": "confirm"}}
        )


async def test_there_is_no_depth_limit(store: ProjectStore) -> None:
    project = await store.create_project(DEV_ADMIN_ID, title="Deep tree")
    parent = None
    for level in range(7):
        node = await store.create_node(
            DEV_ADMIN_ID,
            project.id,
            kind="topic",
            title=f"level {level}",
            parent_id=parent.id if parent else None,
        )
        parent = node
    assert parent.number == ".".join(["1"] * 7)


async def test_a_free_grant_sends_a_request_at_once(store: ProjectStore) -> None:
    project = await store.create_project(DEV_ADMIN_ID, title="Requests")
    await store.set_scope(
        DEV_ADMIN_ID, project.id, revision=1, scope={"research": {"request_report": "free"}}
    )
    request, note = await MessageService(store, None).send_request(
        DEV_ADMIN_ID, True, project.id, target="research", payload={"topic": "Compare servers"}
    )
    assert request.report_kind == "request"
    assert (request.message_type, request.source_app, request.target_app) == (
        "project.request", "project", "research",
    )  # fmt: skip
    assert request.msg_state == "sent" and request.chain_id is not None
    assert note == "the agent queue is not available"


async def test_a_confirm_grant_waits_for_the_person(store: ProjectStore) -> None:
    project = await store.create_project(DEV_ADMIN_ID, title="Confirm")
    await store.set_scope(
        DEV_ADMIN_ID, project.id, revision=1, scope={"dev": {"request_report": "confirm"}}
    )
    service = MessageService(store, None)
    request, _ = await service.send_request(
        DEV_ADMIN_ID, True, project.id, target="dev", payload={"topic": "Branch for the trace tool"}
    )
    assert request.msg_state == "awaiting_click"
    sent, _ = await service.decide(DEV_ADMIN_ID, True, request.id, confirm=True)
    assert sent.msg_state == "sent"
    with pytest.raises(Invalid):
        await service.decide(DEV_ADMIN_ID, True, request.id, confirm=False)


async def test_a_declined_request_stays_rejected(store: ProjectStore) -> None:
    project = await store.create_project(DEV_ADMIN_ID, title="Decline")
    await store.set_scope(
        DEV_ADMIN_ID, project.id, revision=1, scope={"dev": {"request_report": "confirm"}}
    )
    service = MessageService(store, None)
    request, _ = await service.send_request(
        DEV_ADMIN_ID, True, project.id, target="dev", payload={"topic": "Prototype"}
    )
    declined, _ = await service.decide(DEV_ADMIN_ID, True, request.id, confirm=False)
    assert declined.msg_state == "rejected"


async def test_a_request_without_a_grant_is_refused(store: ProjectStore) -> None:
    project = await store.create_project(DEV_ADMIN_ID, title="No grant")
    with pytest.raises(Invalid):
        await MessageService(store, None).send_request(
            DEV_ADMIN_ID, True, project.id, target="dev", payload={"topic": "Branch"}
        )
    assert [n for n in await store.tree(DEV_ADMIN_ID, project.id)] == []
    assert await store.chains_of(DEV_ADMIN_ID, project.id) == []


async def test_a_request_the_registry_refuses_is_not_stored(store: ProjectStore) -> None:
    project = await store.create_project(DEV_ADMIN_ID, title="Bad payload")
    await store.set_scope(
        DEV_ADMIN_ID, project.id, revision=1, scope={"research": {"request_report": "free"}}
    )
    service = MessageService(store, None)
    for payload in ({}, {"topic": "x", "surprise": 1}, {"topic": "x", "min_hosts": -1}):
        with pytest.raises(Invalid):
            await service.send_request(
                DEV_ADMIN_ID, True, project.id, target="research", payload=payload
            )
    with pytest.raises(Invalid):
        await service.send_request(
            DEV_ADMIN_ID, True, project.id, target="chat", payload={"topic": "x"}
        )
    assert [n for n in await store.tree(DEV_ADMIN_ID, project.id)] == []
