"""A request from the project agent runs through the real hub (PPH-05, 05b).

The project agent's tool sends a request; the hub's agent service queues it for Research; a
scripted model looks at a fake node and answers through ``research.message.send``; the hub's
lifecycle hook moves the request along. Only the model and the node are fakes."""

import re
import uuid
from typing import Any

from fastapi.testclient import TestClient

from tests.fakenode import eventually
from tests.test_agent import finish, tool, use, vm, wait_for  # noqa: F401 - vm is a fixture
from topcoat_hub.models import DEV_ADMIN_ID
from topcoat_hub.project_store import ProjectStore
from topcoat_hub.project_tools import ProjectTools
from topcoat_hub.tools import Caller


def _verdict(messages: list[dict[str, str]]) -> str:
    match = re.search(r"message_id: ([0-9a-f-]{36})", messages[1]["content"])
    assert match, messages[1]["content"]
    return tool(
        "messages",
        "project.review.submit",
        message_id=match.group(1),
        verdict="accept",
        reasons="the claim is backed by the quote",
    )


def _reply_to(messages: list[dict[str, str]]) -> str:
    match = re.search(r"reply_to: ([0-9a-f-]{36})", messages[1]["content"])
    assert match, messages[1]["content"]
    return match.group(1)


def _answer(messages: list[dict[str, str]]) -> str:
    return tool(
        "messages",
        "research.message.send",
        reply_to=_reply_to(messages),
        kind="result",
        summary="web ran out of memory",
        claims=[{"claim": "web was killed for memory", "evidence": ["E1"]}],
        open_points=["not checked on the other host"],
    )


def _router(answers: bool):  # type: ignore[no-untyped-def]
    """One model for the three runs that overlap (research, reviewer, planner): it tells them
    apart by what it is shown, so the order in which they ask does not matter."""

    def reply(messages: list[dict[str, str]]) -> str:
        first, last = messages[1]["content"], messages[-1]["content"]
        if "[review of a message" in first:
            return finish("Reviewed.") if "recorded" in last else _verdict(messages)
        if "[planner run" in first:
            return finish("Nothing to change.")
        if last.startswith("[E1]") and answers:
            return _answer(messages)
        return (
            finish()
            if len(messages) > 3
            else tool("vm-docker", "docker.logs", container="web", lines=50)
        )

    return reply


async def _request(client: TestClient, store: ProjectStore) -> tuple[uuid.UUID, dict[str, Any]]:
    """Runs in the app's own loop (``client.portal``), where its agent service lives."""
    project = await store.create_project(DEV_ADMIN_ID, title="Memory")
    await store.set_scope(
        DEV_ADMIN_ID, project.id, revision=1, scope={"research": {"request_report": "free"}}
    )
    tools = ProjectTools(client.app.state.sessionmaker)
    tools.agents = client.app.state.agents
    out = await tools.run_tool(
        Caller(DEV_ADMIN_ID, True, "agent", application="project"),
        "project.request.send",
        {"project_id": str(project.id), "target": "research", "topic": "why does web restart?"},
    )
    assert out["output"]["queue_note"] == "", out["output"]
    return project.id, out["output"]


def test_the_agent_of_research_answers_through_the_tool(
    client: TestClient,
    vm,  # noqa: F811
    open_ceiling: None,
) -> None:
    # research looks and answers; the reviewer accepts; the planner run that follows only finishes
    use(client, *[_router(True)] * 30)
    store = ProjectStore(client.app.state.sessionmaker)
    pid, sent = client.portal.call(_request, client, store)
    task_id = sent["message"]["task_id"]
    wait_for(client, task_id, "done")

    def reviewed() -> list[Any]:
        rows = client.portal.call(store.messages_of, DEV_ADMIN_ID, pid)
        assert [r.message_type for r in rows] == [
            "project.request", "research.result", "project.review",
        ], [(r.message_type, r.msg_state) for r in rows]  # fmt: skip
        assert rows[0].msg_state == "accepted", rows[0].msg_state
        return rows

    rows = eventually(reviewed, timeout=20)
    request, result, review = rows
    assert result.msg_state == "accepted" and review.payload["verdict"] == "accept"
    assert result.verification["ok"] is True
    assert result.message_type == "research.result" and result.in_reply_to == request.id
    (finding,) = result.payload["findings"]
    assert finding["claim"] == "web was killed for memory"
    assert "out of memory" in finding["quote"]
    assert result.payload["open_points"] == ["not checked on the other host"]
    assert result.stats["tool_calls"] >= 1
    assert result.chain_id == request.chain_id


def test_a_run_that_never_answers_still_gets_its_request_closed(
    client: TestClient,
    vm,  # noqa: F811
) -> None:
    use(client, *[_router(False)] * 30)
    store = ProjectStore(client.app.state.sessionmaker)
    pid, sent = client.portal.call(_request, client, store)
    wait_for(client, sent["message"]["task_id"], "done")

    def closed() -> list[Any]:
        rows = client.portal.call(store.messages_of, DEV_ADMIN_ID, pid)
        assert len(rows) >= 2 and rows[0].msg_state in ("reviewing", "accepted")
        return rows

    rows = eventually(closed, timeout=10)
    assert rows[1].message_type == "research.result"
    assert rows[1].payload["summary"] == "The web container ran out of memory."
