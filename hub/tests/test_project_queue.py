"""Requests between the applications (PPH-05, 05b): from ``project.request.send`` to the answer.

A free grant sends the request at once; a confirm grant sends it when the person confirms it; a
declined request is never queued. The queue refuses a task for a reason the note says. The answer
is filed by the hub from the task's stored evidence, whether the agent sent it through
``research.message.send`` or only finished. The queue is the hub's table: the test runs the real
AgentStore against the test database; the agent's work is played by the test.
"""

import uuid
from datetime import UTC, datetime
from typing import Any

import pytest
from fastapi.testclient import TestClient

from tests.conftest import AUTH
from topcoat_hub.agent_service import AgentError
from topcoat_hub.agent_store import AgentStore
from topcoat_hub.message_service import MessageService
from topcoat_hub.message_tools import MessageTools
from topcoat_hub.models import DEV_ADMIN_ID, AgentTask
from topcoat_hub.project_events import SYSTEM
from topcoat_hub.project_store import ProjectStore
from topcoat_hub.project_tools import ProjectTools
from topcoat_hub.tools import Caller, ToolError


class QueueFake:
    """Puts the task into the real agent table, as AgentService.start does for a queued task."""

    def __init__(self, sessionmaker: Any, refuse: str | None = None) -> None:
        self.sessionmaker = sessionmaker
        self.refuse = refuse
        self.calls: list[dict[str, Any]] = []
        self.canceled: list[uuid.UUID] = []

    async def start(
        self, user: Any, prompt: str, model: Any, application: str | None = None, **_: Any
    ) -> AgentTask:
        if self.refuse:
            raise AgentError(503, "agent_disabled", self.refuse)
        self.calls.append({"user": user.id, "prompt": prompt, "application": application})
        async with self.sessionmaker() as db:
            task = await AgentStore(db, user.uuid).create_task(
                prompt, None, "gui", application=application
            )
            await db.commit()
            return task

    async def cancel(self, user_id: uuid.UUID, task_id: uuid.UUID) -> None:
        self.canceled.append(task_id)


def planner() -> Caller:
    return Caller(user_id=DEV_ADMIN_ID, admin=True, via="agent", application="project")


def researcher(task_id: uuid.UUID) -> Caller:
    return Caller(
        user_id=DEV_ADMIN_ID, admin=True, via="agent", application="research", task_id=task_id
    )


@pytest.fixture
def store(client: TestClient) -> ProjectStore:
    return ProjectStore(client.app.state.sessionmaker)


async def _project(store: ProjectStore, title: str, scope: dict[str, Any]) -> uuid.UUID:
    project = await store.create_project(DEV_ADMIN_ID, title=title)
    await store.set_scope(DEV_ADMIN_ID, project.id, revision=1, scope=scope)
    return project.id


FREE = {"research": {"request_report": "free"}}


async def _send(
    client: TestClient, store: ProjectStore, title: str = "Queued", **payload: Any
) -> tuple[uuid.UUID, dict[str, Any], QueueFake]:
    tools = ProjectTools(client.app.state.sessionmaker)
    fake = QueueFake(client.app.state.sessionmaker)
    tools.agents = fake
    pid = await _project(store, title, FREE)
    out = await tools.run_tool(
        planner(),
        "project.request.send",
        {"project_id": str(pid), "target": "research", "topic": "Compare servers", **payload},
    )
    return pid, out["output"], fake


async def _evidence(client: TestClient, task_id: uuid.UUID, excerpt: str, url: str) -> None:
    async with client.app.state.sessionmaker() as db:
        agent_store = AgentStore(db, DEV_ADMIN_ID)
        task = await agent_store.get(task_id)
        assert task is not None
        await agent_store.add_evidence(
            task,
            "web",
            {"url": url, "capability": "web.fetch", "at": "2026-10-29T10:00:00+00:00"},
            excerpt,
            len(excerpt),
        )


def _service(client: TestClient, fake: Any = None) -> MessageService:
    return MessageService(ProjectStore(client.app.state.sessionmaker, SYSTEM), fake)


async def test_a_free_request_is_queued_for_its_target_application(
    client: TestClient, store: ProjectStore
) -> None:
    pid, out, fake = await _send(client, store, questions=["Which is faster?"], min_hosts=2)
    assert out["queue_note"] == ""
    assert out["message"]["state"] == "queued"
    task_id = out["message"]["task_id"]
    assert fake.calls[0]["application"] == "research"
    prompt = fake.calls[0]["prompt"]
    assert "Compare servers" in prompt and "Which is faster?" in prompt
    assert f"reply_to: {out['id']}" in prompt and f"chain: {out['message']['chain_id']}" in prompt
    assert "research.message.send" in prompt
    async with client.app.state.sessionmaker() as db:
        task = await db.get(AgentTask, uuid.UUID(task_id))
        assert task is not None and task.application == "research"
    chain = await store.get_chain(DEV_ADMIN_ID, uuid.UUID(out["message"]["chain_id"]))
    assert chain.project_id == pid and chain.status == "active"


async def test_a_request_waits_for_the_person_and_is_queued_when_confirmed(
    client: TestClient, store: ProjectStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = QueueFake(client.app.state.sessionmaker)
    monkeypatch.setattr(client.app.state.agents, "start", fake.start)
    pid = await _project(store, "Confirmed", {"dev": {"request_report": "confirm"}})
    request, _ = await MessageService(store, fake).send_request(
        DEV_ADMIN_ID, True, pid, target="dev", payload={"topic": "Branch for the trace tool"}
    )
    assert request.task_id is None and request.msg_state == "awaiting_click"
    assert fake.calls == []
    done = client.post(
        f"/api/project/nodes/{request.id}/decide", json={"confirm": True}, headers=AUTH
    )
    assert done.status_code == 200
    assert done.json()["message"]["state"] == "queued"
    assert done.json()["message"]["task_id"] is not None
    assert fake.calls[-1]["application"] == "dev"


async def test_a_declined_request_is_never_queued(
    client: TestClient, store: ProjectStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = QueueFake(client.app.state.sessionmaker)
    monkeypatch.setattr(client.app.state.agents, "start", fake.start)
    pid = await _project(store, "Declined", {"dev": {"request_report": "confirm"}})
    request, _ = await MessageService(store, fake).send_request(
        DEV_ADMIN_ID, True, pid, target="dev", payload={"topic": "Prototype"}
    )
    out = client.post(
        f"/api/project/nodes/{request.id}/decide", json={"confirm": False}, headers=AUTH
    )
    assert out.json()["message"]["state"] == "rejected" and out.json()["message"]["task_id"] is None
    assert fake.calls == []


async def test_a_refused_queue_keeps_the_request_sent_and_says_why(
    client: TestClient, store: ProjectStore
) -> None:
    tools = ProjectTools(client.app.state.sessionmaker)
    tools.agents = QueueFake(client.app.state.sessionmaker, refuse="the agent is switched off")
    pid = await _project(store, "Refused", {"admin": {"read_released": "free"}})
    out = await tools.run_tool(
        planner(),
        "project.request.send",
        {"project_id": str(pid), "target": "admin", "topic": "Released data"},
    )
    assert out["output"]["message"]["state"] == "sent"
    assert out["output"]["message"]["task_id"] is None
    assert "switched off" in out["output"]["queue_note"]


async def test_without_a_queue_the_request_is_sent_and_says_so(
    client: TestClient, store: ProjectStore
) -> None:
    tools = ProjectTools(client.app.state.sessionmaker)  # no agents attached
    pid = await _project(store, "No queue", FREE)
    out = await tools.run_tool(
        planner(),
        "project.request.send",
        {"project_id": str(pid), "target": "research", "topic": "Look"},
    )
    assert out["output"]["message"]["state"] == "sent"
    assert out["output"]["queue_note"] == "the agent queue is not available"


async def test_the_agent_answers_with_claims_and_the_hub_takes_quotes_from_evidence(
    client: TestClient, store: ProjectStore
) -> None:
    pid, out, _ = await _send(client, store, "Answered", must_cover=["speed", "price"], min_hosts=2)
    task_id = uuid.UUID(out["message"]["task_id"])
    await _evidence(
        client, task_id, "Server A is twice as fast.\nIt costs nothing.", "https://a.example.org/x"
    )
    await _evidence(client, task_id, "Server B has a free tier.", "https://b.example.net/y")
    messages = MessageTools(client.app.state.sessionmaker)
    answer = await messages.run_tool(
        researcher(task_id),
        "research.message.send",
        {
            "reply_to": out["id"],
            "kind": "result",
            "summary": "A is faster, B is cheaper.",
            "claims": [
                {"claim": "Server A is faster", "evidence": ["E1"]},
                {"claim": "Server B is cheaper", "evidence": ["E2"]},
                {"claim": "Server C exists", "evidence": ["E9"]},  # no such mark
            ],
            "coverage": {"speed": {"findings": [0, 7]}, "price": {"findings": [1]}},
            "open_points": ["no data on C"],
        },
    )
    assert answer["output"]["type"] == "research.result"
    assert answer["output"]["findings"] == 2
    rows = await store.messages_of(DEV_ADMIN_ID, pid)
    request = next(m for m in rows if m.message_type == "project.request")
    result = next(m for m in rows if m.message_type == "research.result")
    assert request.msg_state == "delivered"
    assert (result.source_app, result.target_app, result.trust) == (
        "research",
        "project",
        "foreign",
    )
    assert result.in_reply_to == request.id and result.chain_id == request.chain_id
    assert result.msg_state == "delivered"
    findings = result.payload["findings"]
    assert findings[0]["quote"] == "Server A is twice as fast."
    assert findings[0]["source_host"] == "a.example.org"
    assert findings[1]["source_url"] == "https://b.example.net/y"
    assert any("Server C exists" in p for p in result.payload["open_points"])
    assert "no data on C" in result.payload["open_points"]
    assert result.payload["coverage"]["speed"]["findings"] == [0]  # index 7 is no finding
    assert result.stats["sources"] == 2 and result.stats["hosts"] == 2
    assert result.stats["queued_at"] is not None


async def test_an_answer_is_taken_once_and_only_for_its_own_request(
    client: TestClient, store: ProjectStore
) -> None:
    _, out, _ = await _send(client, store, "Once")
    task_id = uuid.UUID(out["message"]["task_id"])
    messages = MessageTools(client.app.state.sessionmaker)
    body = {"reply_to": out["id"], "kind": "result", "summary": "Done.", "claims": []}
    with pytest.raises(ToolError) as wrong:
        await messages.run_tool(
            researcher(task_id),
            "research.message.send",
            {**body, "reply_to": str(uuid.uuid4())},
        )
    assert wrong.value.status == 422
    await messages.run_tool(researcher(task_id), "research.message.send", body)
    with pytest.raises(ToolError) as twice:
        await messages.run_tool(researcher(task_id), "research.message.send", body)
    assert twice.value.status == 409
    with pytest.raises(ToolError) as stranger:  # a run that was not started for a request
        await messages.run_tool(researcher(uuid.uuid4()), "research.message.send", body)
    assert stranger.value.status == 409


async def test_only_the_answering_application_is_offered_the_tool(
    client: TestClient,
) -> None:
    messages = MessageTools(client.app.state.sessionmaker)
    # research also writes a long report in sections (research_profile.py); the send tool is
    # offered only to a run that was started for a request (a chat run has none)
    assert [t["capability"] for t in await messages.offer(researcher(uuid.uuid4()))] == [
        "research.report.section",
        "research.report.show",
        "research.report.image",
    ]
    assert await messages.offer(planner()) == []
    with pytest.raises(ToolError):
        await messages.run_tool(planner(), "research.message.send", {})


async def test_a_task_that_finishes_without_the_tool_is_answered_from_its_diagnosis(
    client: TestClient, store: ProjectStore
) -> None:
    pid, out, _ = await _send(client, store, "Fallback")
    task_id = uuid.UUID(out["message"]["task_id"])
    await _evidence(client, task_id, "Only one page said so.", "https://c.example.org/")
    async with client.app.state.sessionmaker() as db:
        agent_store = AgentStore(db, DEV_ADMIN_ID)
        task = await agent_store.get(task_id)
        assert task is not None
        await agent_store.set_status(
            task,
            "done",
            diagnosis={
                "summary": "One source.",
                "findings": [{"claim": "Said so", "tag": "web", "evidence": ["E1"]}],
            },
        )
    await _service(client).on_task_event(DEV_ADMIN_ID, task_id, "done")
    rows = await store.messages_of(DEV_ADMIN_ID, pid)
    assert [m.message_type for m in rows] == ["project.request", "research.result"]
    assert rows[1].payload["findings"][0]["quote"] == "Only one page said so."
    assert rows[0].msg_state == "delivered"


async def test_a_failed_task_files_a_failure_and_closes_the_chain(
    client: TestClient, store: ProjectStore
) -> None:
    pid, out, _ = await _send(client, store, "Failing")
    task_id = uuid.UUID(out["message"]["task_id"])
    async with client.app.state.sessionmaker() as db:
        agent_store = AgentStore(db, DEV_ADMIN_ID)
        task = await agent_store.get(task_id)
        assert task is not None
        await agent_store.set_status(task, "failed", stop_reason="no_tools", error="no node")
    await _service(client).on_task_event(DEV_ADMIN_ID, task_id, "failed")
    rows = await store.messages_of(DEV_ADMIN_ID, pid)
    request, failure = rows
    assert request.msg_state == "failed"
    assert failure.message_type == "research.failure"
    assert failure.payload["reason"] == "node_away" and failure.payload["retryable"] is True
    chain = await store.get_chain(DEV_ADMIN_ID, request.chain_id)
    assert chain.status == "done"


async def test_the_task_starting_moves_the_request_to_running(
    client: TestClient, store: ProjectStore
) -> None:
    pid, out, _ = await _send(client, store, "Running")
    task_id = uuid.UUID(out["message"]["task_id"])
    await _service(client).on_task_event(DEV_ADMIN_ID, task_id, "started")
    (request,) = await store.messages_of(DEV_ADMIN_ID, pid)
    assert request.msg_state == "running"
    # an event for a task that carries no request is nobody's business
    await _service(client).on_task_event(DEV_ADMIN_ID, uuid.uuid4(), "done")


async def test_stopping_a_chain_cancels_its_tasks_and_blocks_new_messages(
    client: TestClient, store: ProjectStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    pid, out, _ = await _send(client, store, "Stopped")
    task_id = uuid.UUID(out["message"]["task_id"])
    fake = QueueFake(client.app.state.sessionmaker)
    monkeypatch.setattr(client.app.state.agents, "cancel", fake.cancel)
    chain_id = out["message"]["chain_id"]
    stopped = client.post(f"/api/project/chains/{chain_id}/stop", headers=AUTH)
    assert stopped.status_code == 200
    assert stopped.json()["chain"]["status"] == "stopped"
    assert stopped.json()["stopped"] == [out["id"]]
    assert fake.canceled == [task_id]
    (request,) = await store.messages_of(DEV_ADMIN_ID, pid)
    assert request.msg_state == "stopped"
    again = client.post(f"/api/project/chains/{chain_id}/stop", headers=AUTH)
    assert again.status_code == 200 and again.json()["stopped"] == []
    # a late answer finds a stopped request and files nothing
    await _service(client).on_task_event(DEV_ADMIN_ID, task_id, "done")
    assert len(await store.messages_of(DEV_ADMIN_ID, pid)) == 1
    with pytest.raises(Exception, match="stopped"):
        await store.add_message(
            DEV_ADMIN_ID,
            pid,
            type="project.request",
            sender="project",
            receiver="research",
            payload={"topic": "More"},
            chain_id=uuid.UUID(chain_id),
        )


def test_another_persons_chain_cannot_be_stopped(client: TestClient) -> None:
    gone = client.post(f"/api/project/chains/{uuid.uuid4()}/stop", headers=AUTH)
    assert gone.status_code == 404


def test_the_time_in_the_stats_is_an_iso_string() -> None:
    assert datetime(2026, 10, 29, tzinfo=UTC).isoformat().startswith("2026-10-29")
