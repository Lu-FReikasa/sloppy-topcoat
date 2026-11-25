"""The brief of the Projekte chat (application_brief.py): names and counts, and one line over all
projects of what needs attention. Read from the tables at the moment of the question."""

from types import SimpleNamespace

from fastapi.testclient import TestClient

from topcoat_hub import application_brief
from topcoat_hub.models import DEV_ADMIN_ID
from topcoat_hub.project_store import ProjectStore


async def test_the_brief_counts_what_needs_attention_across_projects(client: TestClient) -> None:
    sessionmaker = client.app.state.sessionmaker
    store = ProjectStore(sessionmaker)
    state = SimpleNamespace(sessionmaker=sessionmaker, settings=SimpleNamespace(hub_timezone="UTC"))

    first = await application_brief.project(state, DEV_ADMIN_ID)
    assert "no project" in first  # nothing yet

    a = await store.create_project(DEV_ADMIN_ID, title="Alpha")
    b = await store.create_project(DEV_ADMIN_ID, title="Beta")
    quiet = await application_brief.project(state, DEV_ADMIN_ID)
    assert "Needs attention" not in quiet

    await store.create_node(
        DEV_ADMIN_ID,
        a.id,
        kind="task",
        title="Late",
        fields={"due_at": "2020-01-01T09:00:00+00:00"},
    )
    review = await store.create_node(DEV_ADMIN_ID, b.id, kind="task", title="Look at it")
    await store.update_node(DEV_ADMIN_ID, review.id, revision=review.revision, status="in_review")
    await store.create_node(
        DEV_ADMIN_ID, b.id, kind="report", title="Which database?", report_kind="question"
    )

    text = await application_brief.project(state, DEV_ADMIN_ID)
    assert "Alpha" in text and "Beta" in text
    line = next(ln for ln in text.splitlines() if ln.startswith("Needs attention"))
    assert line == "Needs attention: 1 overdue, 1 in review, 1 unanswered."
    # the whole brief stays small
    assert len(await application_brief.brief(state, DEV_ADMIN_ID, "project")) < 1700


async def test_the_brief_names_what_the_person_changed_by_hand(client: TestClient) -> None:
    # the maintainer, 2026-11-17: "the agent should always see all my changes"
    from topcoat_hub.project_events import PERSON

    sessionmaker = client.app.state.sessionmaker
    by_hand = ProjectStore(sessionmaker, PERSON, "UTC")
    by_agent = ProjectStore(sessionmaker)  # the system actor
    state = SimpleNamespace(sessionmaker=sessionmaker, settings=SimpleNamespace(hub_timezone="UTC"))
    project = await by_hand.create_project(DEV_ADMIN_ID, title="Topcoat")
    task = await by_hand.create_node(DEV_ADMIN_ID, project.id, kind="task", title="Chat Code lesen")
    await by_hand.update_node(
        DEV_ADMIN_ID, task.id, revision=task.revision, body="Zielanwendung: dev"
    )
    await by_agent.create_node(DEV_ADMIN_ID, project.id, kind="task", title="Made by the system")

    text = await application_brief.person_changes(state, DEV_ADMIN_ID)
    lines = text.splitlines()[1:]
    assert "changed the text of Topcoat 1 «Chat Code lesen»" in lines[0]  # newest first
    assert any("created Topcoat 1 «Chat Code lesen»" in ln for ln in lines)
    assert "Made by the system" not in text
    brief = await application_brief.brief(state, DEV_ADMIN_ID, "project")
    assert "What the person changed in Projekte by hand" in brief


async def test_a_committed_project_change_reaches_the_persons_open_pages(
    client: TestClient,
) -> None:
    # the maintainer, 2026-11-17: an agent's change showed only after leaving the project
    bus = client.app.state.bus
    queue = bus.subscribe()
    try:
        store = ProjectStore(client.app.state.sessionmaker)
        project = await store.create_project(DEV_ADMIN_ID, title="Live")
        await store.create_node(DEV_ADMIN_ID, project.id, kind="task", title="Seen at once")
        seen = []
        while not queue.empty():
            seen.append(queue.get_nowait())
        changed = [d for e, d in seen if e == "project_changed"]
        assert changed and all(d["project_id"] == str(project.id) for d in changed)
        assert all(d["_user"] == str(DEV_ADMIN_ID) for d in changed)  # only the owner's streams
    finally:
        bus.unsubscribe(queue)
