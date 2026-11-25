"""A node comes online: tasks that stopped for lack of one go on (docs/PLATFORM.md, section 4)."""

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from tests.conftest import AUTH, TEST_DB_URL
from tests.users import make_user
from topcoat_hub.agent_service import AgentError
from topcoat_hub.models import DEV_ADMIN_ID, AgentTask
from topcoat_hub.resume_stuck import StuckTaskResumer


class StubAgents:
    """Stands in for the agent service: records who continued what, refuses what it is told to."""

    def __init__(self) -> None:
        self.resumed: list[tuple[str, uuid.UUID]] = []
        self.refuse: dict[uuid.UUID, str] = {}

    async def resume(self, user: Any, task_id: uuid.UUID) -> None:
        if task_id in self.refuse:
            raise AgentError(
                429 if self.refuse[task_id] == "too_many_tasks" else 409, self.refuse[task_id], "no"
            )
        self.resumed.append((user.id, task_id))


def task(
    title: str,
    *,
    user: uuid.UUID = DEV_ADMIN_ID,
    status: str = "failed",
    reason: str | None = "no_tools",
    hours_ago: float = 1,
    runs: int = 1,
) -> uuid.UUID:
    engine = create_engine(TEST_DB_URL)
    with Session(engine) as s:
        row = AgentTask(
            user_id=user,
            prompt=title,
            title=title,
            status=status,
            stop_reason=reason,
            runs=runs,
            finished_at=datetime.now(UTC) - timedelta(hours=hours_ago),
        )
        s.add(row)
        s.commit()
        task_id = row.id
    engine.dispose()
    return task_id


def resumer(client: TestClient, agents: StubAgents) -> StuckTaskResumer:
    state = client.app.state
    settings = state.settings
    settings.resume_stuck_enabled = True
    settings.resume_stuck_max = 5
    return StuckTaskResumer(state.bus, state.sessionmaker, agents, settings)  # type: ignore[arg-type]


def online(node: str, state: str = "available", name: str = "node-a") -> tuple[str, dict[str, Any]]:
    return "node_updated", {"id": node, "state": state, "name": name}


def test_only_the_change_to_online_counts(client: TestClient) -> None:
    r = resumer(client, StubAgents())
    assert r.came_online(*online("n1")) == "n1"  # first seen since the hub started: it came online
    assert r.came_online(*online("n1")) is None  # an update of a node that stays online
    assert r.came_online(*online("n1", "busy")) is None
    assert r.came_online(*online("n1", "offline")) is None
    assert r.came_online(*online("n1")) == "n1"  # offline, then online again
    assert r.came_online("node_event", {"id": "n1", "state": "available"}) is None
    assert r.came_online(*online("n2", "offline")) is None
    assert r.came_online("node_updated", {"state": "available"}) is None


def test_tasks_that_stopped_for_lack_of_a_node_go_on(client: TestClient) -> None:
    a = task("check the containers", reason="no_tools")
    b = task("look at the journal", reason="tools_missing")
    task("a task that failed for another reason", reason="time_budget")
    task("a task that is done", status="done", reason=None)
    task("too old", hours_ago=200)
    task("tried often enough", runs=3)
    agents = StubAgents()
    r = resumer(client, agents)
    assert client.portal.call(r.handle, *online("n1")) == 2
    assert {t for _, t in agents.resumed} == {a, b}
    assert {u for u, _ in agents.resumed} == {str(DEV_ADMIN_ID)}


def test_the_inbox_says_what_continues(client: TestClient) -> None:
    task("check the containers")
    r = resumer(client, StubAgents())
    client.portal.call(r.handle, *online("n1", name="node-tools"))
    (item,) = client.get("/api/inbox", headers=AUTH).json()["items"]
    assert item["kind"] == "tasks_resumed" and item["application"] == "admin"
    assert item["urgent"] is False
    assert "node-tools" in item["title"] and "check the containers" in item["summary"]


def test_a_few_at_a_time_and_none_when_the_person_has_too_many_running(client: TestClient) -> None:
    ids = [task(f"task {i}", hours_ago=10 - i) for i in range(4)]
    agents = StubAgents()
    r = resumer(client, agents)
    client.app.state.settings.resume_stuck_max = 2
    assert client.portal.call(r.handle, *online("n1")) == 2
    assert [t for _, t in agents.resumed] == ids[:2]  # the oldest first

    again = StubAgents()
    again.refuse = {ids[0]: "too_many_tasks"}
    r2 = resumer(client, again)
    client.app.state.settings.resume_stuck_max = 5
    assert client.portal.call(r2.handle, *online("n9")) == 0  # the person's limit: nothing more


def test_a_task_that_cannot_be_continued_does_not_stop_the_others(client: TestClient) -> None:
    first = task("gone", hours_ago=5)
    second = task("fine", hours_ago=1)
    agents = StubAgents()
    agents.refuse = {first: "not_resumable"}
    r = resumer(client, agents)
    assert client.portal.call(r.handle, *online("n1")) == 1
    assert [t for _, t in agents.resumed] == [second]


def test_every_person_gets_their_own_tasks_continued_and_their_own_note(
    client: TestClient, clean_db: str
) -> None:
    other_id, other = make_user(clean_db, "user-b")
    mine = task("mine")
    theirs = task("theirs", user=other_id)
    agents = StubAgents()
    r = resumer(client, agents)
    assert client.portal.call(r.handle, *online("n1")) == 2
    by_user = {u: t for u, t in agents.resumed}
    assert by_user == {str(DEV_ADMIN_ID): mine, str(other_id): theirs}
    assert "theirs" not in client.get("/api/inbox", headers=AUTH).json()["items"][0]["summary"]
    assert "mine" not in client.get("/api/inbox", headers=other).json()["items"][0]["summary"]


def test_switched_off_it_does_nothing(client: TestClient) -> None:
    task("check")
    agents = StubAgents()
    r = resumer(client, agents)
    client.app.state.settings.resume_stuck_enabled = False
    assert client.portal.call(r.handle, *online("n1")) == 0
    assert agents.resumed == []
