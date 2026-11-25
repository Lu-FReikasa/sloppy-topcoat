"""The place of a person's waiting task, told to that person only (queue_watch.py), and the richer
``agent_task`` event the herald of the desktop client reads (agent_events.py)."""

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from fastapi.testclient import TestClient

from tests.conftest import TEST_DB_URL
from tests.users import make_user
from topcoat_hub.agent_events import task_event
from topcoat_hub.models import DEV_ADMIN_ID, AgentTask
from topcoat_hub.queue_watch import QueueWatch, places


def test_places_are_the_first_request_of_each_task() -> None:
    waiting = [
        {"ahead": 0, "kind": "chat"},
        {"ahead": 1, "task_id": "t1"},
        {"ahead": 2, "task_id": "t1"},
        {"ahead": 3, "task_id": "t2"},
    ]
    assert places(waiting) == {"t1": 1, "t2": 3}


def _task(client: TestClient, user_id: uuid.UUID) -> str:
    async def go() -> str:
        async with client.app.state.sessionmaker() as db:
            t = AgentTask(user_id=user_id, prompt="x", title="Recherche", status="queued")
            db.add(t)
            await db.commit()
            return str(t.id)

    return client.portal.call(go)


def test_the_owner_hears_the_place_when_it_changes_and_when_the_task_left_the_queue(
    client: TestClient,
) -> None:
    other, _ = make_user(TEST_DB_URL, "zoe")
    mine, theirs = _task(client, DEV_ADMIN_ID), _task(client, other)
    queue: list[dict[str, Any]] = [{"ahead": 0, "task_id": theirs}, {"ahead": 1, "task_id": mine}]
    heard: list[tuple[str, dict[str, Any]]] = []
    watch = QueueWatch(
        lambda: queue, client.app.state.sessionmaker, lambda e, d: heard.append((e, d))
    )

    client.portal.call(watch.tick)
    mine_heard = [d for _, d in heard if d["task_id"] == mine]
    assert mine_heard == [
        {"task_id": mine, "phase": "queued", "ahead": 1, "_user": str(DEV_ADMIN_ID)}
    ]
    assert all(d["_user"] == str(other) for _, d in heard if d["task_id"] == theirs)

    heard.clear()
    client.portal.call(watch.tick)
    assert heard == []  # nothing changed, nothing said

    queue[:] = [{"ahead": 0, "task_id": mine}]
    client.portal.call(watch.tick)
    phases = {d["task_id"]: (d["phase"], d.get("ahead")) for _, d in heard}
    assert phases == {mine: ("queued", 0), theirs: ("running", None)}


def test_the_task_event_carries_what_the_herald_needs() -> None:
    start = datetime(2026, 11, 12, 20, 0, tzinfo=UTC)
    t = AgentTask(
        id=uuid.uuid4(),
        user_id=DEV_ADMIN_ID,
        title="Recherche zu Alien",
        status="done",
        step_count=7,
        origin="tasks",
        kind="single",
        application="research",
        started_at=start,
        finished_at=start + timedelta(seconds=95),
    )
    ev = task_event(t)
    assert (ev["title"], ev["origin"], ev["application"], ev["seconds"]) == (
        "Recherche zu Alien",
        "tasks",
        "research",
        95,
    )
    assert ev["parent_id"] is None and ev["_user"] == str(DEV_ADMIN_ID)
