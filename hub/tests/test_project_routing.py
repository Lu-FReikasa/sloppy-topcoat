"""Applications that ask back, ask each other, and speak without being asked (PPH-05, 05e/05f).

Everything runs against the fake agent queue; the answering applications are played by the test
through their `<app>.message.send` tool."""

import uuid
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text

from tests.conftest import AUTH, TEST_DB_URL
from tests.test_project_review import Queue
from tests.users import make_user
from topcoat_hub.agent_store import AgentStore
from topcoat_hub.message_service import MessageService, may_ask
from topcoat_hub.message_tools import MessageTools, schema_for
from topcoat_hub.models import DEV_ADMIN_ID
from topcoat_hub.project_events import SYSTEM
from topcoat_hub.project_store import ProjectStore
from topcoat_hub.tools import Caller, ToolError

BRANCH = "topcoat/proto"
SCOPE = {
    "research": {"request_report": "free"},
    "admin": {"read_released": "free"},
    "dev": {"request_report": "free"},
}


def app_caller(app: str, task_id: uuid.UUID) -> Caller:
    return Caller(DEV_ADMIN_ID, True, "agent", application=app, task_id=task_id)


@pytest.fixture
def store(client: TestClient) -> ProjectStore:
    return ProjectStore(client.app.state.sessionmaker)


class World:
    def __init__(self, client: TestClient, store: ProjectStore) -> None:
        self.client, self.store = client, store
        self.sm = client.app.state.sessionmaker
        self.queue = Queue(self.sm)
        self.tools = MessageTools(self.sm)
        self.tools.agents = self.queue
        self.service = MessageService(ProjectStore(self.sm, SYSTEM), self.queue)
        self.project_id = uuid.uuid4()

    async def request(self, target: str = "dev", scope: dict[str, Any] | None = None) -> Any:
        project = await self.store.create_project(DEV_ADMIN_ID, title=f"R{uuid.uuid4().hex[:6]}")
        self.project_id = project.id
        await self.store.set_scope(DEV_ADMIN_ID, project.id, revision=1, scope=scope or SCOPE)
        node, _ = await self.service.send_request(
            DEV_ADMIN_ID, True, project.id, target=target, payload={"topic": "Look at the repo"}
        )
        assert node.task_id is not None
        return node

    async def send(self, app: str, task_id: uuid.UUID, **body: Any) -> dict[str, Any]:
        out = await self.tools.run_tool(app_caller(app, task_id), f"{app}.message.send", body)
        return out["output"]

    async def of_type(self, type_: str) -> list[Any]:
        return await self.store.messages_of(DEV_ADMIN_ID, self.project_id, type=type_)

    async def evidence(self, task_id: uuid.UUID, excerpt: str) -> None:
        async with self.sm() as db:
            agent_store = AgentStore(db, DEV_ADMIN_ID)
            task = await agent_store.get(task_id)
            assert task is not None
            await agent_store.add_evidence(
                task, "live", {"capability": "system.info", "node": "box"}, excerpt, len(excerpt)
            )


@pytest.fixture
def world(client: TestClient, store: ProjectStore) -> World:
    return World(client, store)


# --- the edges ---


def test_the_registry_lists_who_may_ask_whom() -> None:
    assert may_ask("dev", "research") and may_ask("dev", "admin") and may_ask("dev", "project")
    assert may_ask("admin", "research")
    assert not may_ask("admin", "dev") and not may_ask("research", "dev")
    assert not may_ask("dev", "dev") and not may_ask("research", "admin")


def test_the_tool_of_each_application_offers_only_its_own_kinds_and_targets() -> None:
    research, dev, admin = (schema_for(a)["properties"] for a in ("research", "dev", "admin"))
    assert "request" not in research and research["kind"]["enum"] == [
        "result", "failure", "clarification", "changelog", "proposal",
    ]  # fmt: skip
    assert dev["request"]["properties"]["to"]["enum"] == ["research", "admin", "project"]
    assert admin["request"]["properties"]["to"]["enum"] == ["research"]


# --- asking back ---


async def test_a_clarification_makes_the_request_wait_and_asks_the_person_once(
    world: World,
) -> None:
    request = await world.request("research")
    tid = request.task_id
    body = {"kind": "clarification", "clarification": {"questions": ["Which repository?"]}}
    out = await world.send("research", tid, **body)
    assert out["type"] == "research.clarification"
    (asked,) = await world.of_type("project.question")
    assert "Which repository?" in asked.title and asked.answer is None
    assert (await world.store.get_node(DEV_ADMIN_ID, request.id)).msg_state == "waiting"
    assert world.queue.started[-1]["origin"] == "planner"  # prepares an answer, applies nothing
    with pytest.raises(ToolError) as again:  # the request waits: no second answer from this run
        await world.send("research", tid, **body)
    assert again.value.status == 409
    # the run ending afterwards changes nothing about the waiting request
    await world.service.on_task_event(DEV_ADMIN_ID, tid, "done")
    assert (await world.store.get_node(DEV_ADMIN_ID, request.id)).msg_state == "waiting"
    assert len(await world.of_type("project.question")) == 1


async def test_a_clarification_must_have_a_question(world: World) -> None:
    request = await world.request("research")
    with pytest.raises(ToolError) as bad:
        await world.send("research", request.task_id, kind="clarification", clarification={})
    assert bad.value.status == 422


async def test_a_waiting_request_is_stopped_with_its_chain(world: World) -> None:
    request = await world.request("research")
    await world.send(
        "research", request.task_id, kind="clarification", clarification={"questions": ["Which?"]}
    )
    await world.service.stop_chain(DEV_ADMIN_ID, request.chain_id)
    assert (await world.store.get_node(DEV_ADMIN_ID, request.id)).msg_state == "stopped"


# --- asking each other ---


async def test_development_asks_research_inside_the_chain_under_the_projects_grant(
    world: World,
) -> None:
    request = await world.request("dev")
    out = await world.send(
        "dev", request.task_id, kind="request",
        request={"to": "research", "payload": {"topic": "How does paging work in lib X?"}},
    )  # fmt: skip
    assert out["type"] == "dev.request" and out["queue_note"] == ""
    (asked,) = await world.of_type("dev.request")
    assert (asked.source_app, asked.target_app, asked.trust) == ("dev", "research", "system")
    assert asked.chain_id == request.chain_id and asked.in_reply_to == request.id
    assert asked.msg_state == "queued"
    started = world.queue.started[-1]
    assert started["application"] == "research" and "request from dev" in started["prompt"]
    # research answers: the answer goes to the project, in the same chain, and is reviewed there
    await world.evidence(asked.task_id, "Paging is done with limit and offset.")
    await world.send(
        "research", asked.task_id, kind="result", summary="limit and offset",
        claims=[{"claim": "paging uses limit and offset", "evidence": ["E1"]}],
    )  # fmt: skip
    (result,) = await world.of_type("research.result")
    assert result.chain_id == request.chain_id and result.in_reply_to == asked.id
    assert result.msg_state == "reviewing" and world.queue.started[-1]["application"] == "project"


async def test_the_grant_of_the_chains_project_decides_what_development_may_ask(
    world: World,
) -> None:
    request = await world.request("dev", scope={"dev": {"request_report": "free"}})
    ask = {"to": "research", "payload": {"topic": "Anything"}}
    with pytest.raises(ToolError) as none:  # no grant for research in this project
        await world.send("dev", request.task_id, kind="request", request=ask)
    assert none.value.status == 422 and "not granted" in str(none.value)
    assert await world.of_type("dev.request") == []
    project = await world.store.get_project(DEV_ADMIN_ID, world.project_id)
    await world.store.set_scope(
        DEV_ADMIN_ID, project.id, revision=project.revision,
        scope={"dev": {"request_report": "free"}, "research": {"request_report": "confirm"}},
    )  # fmt: skip
    out = await world.send("dev", request.task_id, kind="request", request=ask)
    (asked,) = await world.of_type("dev.request")
    assert asked.msg_state == "awaiting_click" and out["queue_note"] == ""
    assert world.queue.started == [world.queue.started[0]]  # only the first request ever left


async def test_an_unlisted_edge_or_a_bad_payload_is_refused(world: World) -> None:
    request = await world.request("dev")
    for ask in (
        {"to": "dev", "payload": {"topic": "x"}},  # not in the enum
        {"to": "research", "payload": {}},  # no topic
        {"to": "research", "payload": {"topic": "x", "surprise": 1}},
    ):
        with pytest.raises(ToolError) as refused:
            await world.send("dev", request.task_id, kind="request", request=ask)
        assert refused.value.status == 422
    research = await world.request("research")
    with pytest.raises(ToolError) as no_kind:  # research has no request kind at all
        await world.send(
            "research", research.task_id, kind="request",
            request={"to": "admin", "payload": {"topic": "x"}},
        )  # fmt: skip
    assert no_kind.value.status == 422
    assert await world.of_type("dev.request") == []


async def test_a_stopped_chain_takes_no_question_from_development(world: World) -> None:
    request = await world.request("dev")
    await world.service.stop_chain(DEV_ADMIN_ID, request.chain_id)
    with pytest.raises(ToolError) as refused:
        await world.send(
            "dev", request.task_id, kind="request",
            request={"to": "research", "payload": {"topic": "x"}},
        )  # fmt: skip
    assert refused.value.status in (409, 422)
    assert await world.of_type("dev.request") == []


async def test_development_asking_the_project_raises_a_question_for_the_person(
    world: World,
) -> None:
    request = await world.request("dev")
    await world.send(
        "dev", request.task_id, kind="request",
        request={"to": "project", "payload": {"topic": "Which database should the prototype use?"}},
    )  # fmt: skip
    (asked,) = await world.of_type("project.question")
    assert "Which database" in asked.title
    (filed,) = await world.of_type("dev.request")
    assert filed.target_app == "project" and filed.msg_state == "accepted"
    assert world.queue.started[-1]["origin"] == "planner"


async def test_administration_answers_from_its_evidence_in_its_own_shape(world: World) -> None:
    request = await world.request("admin")
    await world.evidence(request.task_id, "disk /data is 91 % full")
    await world.send(
        "admin", request.task_id, kind="result", summary="/data is nearly full",
        claims=[{"claim": "/data is 91 % full", "evidence": ["E1"]}],
    )  # fmt: skip
    (result,) = await world.of_type("admin.result")
    (finding,) = result.payload["findings"]
    assert (
        finding["claim"] == "/data is 91 % full" and finding["quote"] == "disk /data is 91 % full"
    )
    assert finding["source"].startswith("system.info@box") and "coverage" not in result.payload
    assert result.trust == "system" and result.msg_state == "accepted"  # no review needed


async def test_administration_may_ask_research_but_nothing_else(world: World) -> None:
    request = await world.request("admin")
    await world.send(
        "admin", request.task_id, kind="request",
        request={"to": "research", "payload": {"topic": "Is version 3 of the tool out?"}},
    )  # fmt: skip
    (asked,) = await world.of_type("admin.request")
    assert asked.target_app == "research" and asked.msg_state == "queued"
    with pytest.raises(ToolError):
        await world.send(
            "admin", request.task_id, kind="request",
            request={"to": "dev", "payload": {"topic": "x"}},
        )  # fmt: skip


# --- what nobody asked for ---


async def _run_of_the_person(world: World, app: str) -> uuid.UUID:
    async with world.sm() as db:
        task = await AgentStore(db, DEV_ADMIN_ID).create_task(
            "look at the box", None, "gui", application=app
        )
        await db.commit()
        return task.id


async def test_a_proposal_nobody_asked_for_lands_in_the_inbox_as_a_card(world: World) -> None:
    tid = await _run_of_the_person(world, "research")
    out = await world.send(
        "research", tid, kind="proposal",
        proposal={"title": "Check the backups", "why": "They are old.", "steps": ["list", "test"]},
    )  # fmt: skip
    assert out["type"] == "research.proposal"
    inbox = [p for p in await world.store.list_projects(DEV_ADMIN_ID) if p.title == "Inbox"]
    assert len(inbox) == 1
    node = (await world.store.messages_of(DEV_ADMIN_ID, inbox[0].id))[0]
    assert (node.msg_state, node.trust, node.chain_id) == ("awaiting_click", "foreign", None)
    assert world.queue.started[-1]["origin"] == "planner"
    assert "sorting run" in world.queue.started[-1]["prompt"]
    # nothing was created from it: no task, no order
    kinds = {n.kind for n in await world.store.tree(DEV_ADMIN_ID, inbox[0].id)}
    assert kinds == {"report"}


async def test_a_changelog_is_received_and_not_acted_on_and_the_inbox_is_reused(
    world: World,
) -> None:
    tid = await _run_of_the_person(world, "dev")
    await world.send("dev", tid, kind="changelog", changelog={"what": "Rebuilt the image"})
    await world.send("dev", tid, kind="changelog", changelog={"what": "Rebuilt it again"})
    inbox = [p for p in await world.store.list_projects(DEV_ADMIN_ID) if p.title == "Inbox"]
    assert len(inbox) == 1
    rows = await world.store.messages_of(DEV_ADMIN_ID, inbox[0].id, type="dev.changelog")
    assert [r.msg_state for r in rows] == ["accepted", "accepted"]


async def test_the_inbox_is_named_by_the_persons_language(world: World) -> None:
    engine = create_engine(TEST_DB_URL)
    with engine.begin() as conn:
        conn.execute(text("UPDATE users SET language = 'de' WHERE id = :i"), {"i": DEV_ADMIN_ID})
    engine.dispose()
    tid = await _run_of_the_person(world, "admin")
    await world.send("admin", tid, kind="changelog", changelog={"what": "Neu gestartet"})
    assert "Eingang" in [p.title for p in await world.store.list_projects(DEV_ADMIN_ID)]


async def test_a_result_needs_a_request_and_a_loose_field_is_refused(world: World) -> None:
    tid = await _run_of_the_person(world, "research")
    with pytest.raises(ToolError) as no_request:
        await world.send("research", tid, kind="result", summary="Done.")
    assert no_request.value.status == 409
    with pytest.raises(ToolError) as loose:
        await world.send("research", tid, kind="proposal", proposal={"title": "x", "run": "rm"})
    assert loose.value.status == 422
    with pytest.raises(ToolError) as unknown:
        await world.send("research", tid, kind="proposal", proposal={"title": "x"}, order="push")
    assert unknown.value.status == 422


async def test_the_person_accepts_a_proposal_into_a_task_or_declines_it(
    world: World, client: TestClient
) -> None:
    tid = await _run_of_the_person(world, "research")
    steps = {"title": "Check the backups", "why": "They are old.", "steps": ["list", "test"]}
    await world.send("research", tid, kind="proposal", proposal=steps)
    await world.send("research", tid, kind="proposal", proposal={"title": "Other idea"})
    inbox = next(p for p in await world.store.list_projects(DEV_ADMIN_ID) if p.title == "Inbox")
    first, second = await world.store.messages_of(DEV_ADMIN_ID, inbox.id)
    done = client.post(
        f"/api/project/nodes/{first.id}/proposal", headers=AUTH, json={"accept": True}
    )
    assert done.status_code == 200 and done.json()["message"]["state"] == "accepted"
    tasks = [n for n in await world.store.tree(DEV_ADMIN_ID, inbox.id) if n.kind == "task"]
    assert [t.title for t in tasks] == ["Check the backups"] and "- list" in tasks[0].body
    no = client.post(
        f"/api/project/nodes/{second.id}/proposal", headers=AUTH, json={"accept": False}
    )
    assert no.json()["message"]["state"] == "rejected"
    assert len([n for n in await world.store.tree(DEV_ADMIN_ID, inbox.id) if n.kind == "task"]) == 1
    again = client.post(
        f"/api/project/nodes/{first.id}/proposal", headers=AUTH, json={"accept": True}
    )
    assert again.status_code == 422  # decided once


async def test_a_key_cannot_click_and_another_person_cannot_see_the_inbox(
    world: World, client: TestClient
) -> None:
    tid = await _run_of_the_person(world, "research")
    await world.send("research", tid, kind="proposal", proposal={"title": "Check the backups"})
    inbox = next(p for p in await world.store.list_projects(DEV_ADMIN_ID) if p.title == "Inbox")
    (node,) = await world.store.messages_of(DEV_ADMIN_ID, inbox.id)
    _, key = make_user(TEST_DB_URL, "key clicker", role="admin")
    assert client.post(
        f"/api/project/nodes/{node.id}/proposal", headers=key, json={"accept": True}
    ).status_code in (401, 403)
    stranger, _ = make_user(TEST_DB_URL, "inbox stranger", role="user")
    assert await world.store.list_projects(stranger) == []
    from topcoat_hub.project_store import NotFound

    with pytest.raises(NotFound):
        await world.store.messages_of(stranger, inbox.id)
    assert (await world.store.get_node(DEV_ADMIN_ID, node.id)).msg_state == "awaiting_click"
