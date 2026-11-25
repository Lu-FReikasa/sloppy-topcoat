"""Orders and mandates (PPH-05, 05d): who can give one, what it covers, how an order is sent
inside it, and what the hub does with an answer of Development.

The agent queue is a fake that stores real tasks. The push under a mandate runs through the real
proposal service against the fake dev node of the workshop tests (see further down)."""

import json
import uuid
from datetime import timedelta
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text

from tests.conftest import AUTH, TEST_DB_URL
from tests.test_dev_workshop import DIFF, dev_node, proposals_on, repos  # noqa: F401 - fixtures
from tests.test_project_review import Queue, reviewer
from tests.users import make_user
from topcoat_hub import clock
from topcoat_hub.message_service import MessageService
from topcoat_hub.message_tools import MessageTools
from topcoat_hub.models import DEV_ADMIN_ID
from topcoat_hub.project_events import SYSTEM
from topcoat_hub.project_mandates import ORDER_ACTIONS as ACTIONS
from topcoat_hub.project_store import Invalid, NotFound, ProjectStore
from topcoat_hub.project_tools import ProjectTools
from topcoat_hub.proposals import MandatePush
from topcoat_hub.tools import Caller, ToolError

BRANCH = "topcoat/prototype"


def planner() -> Caller:
    return Caller(DEV_ADMIN_ID, True, "agent", application="project")


def developer(task_id: uuid.UUID) -> Caller:
    return Caller(DEV_ADMIN_ID, True, "agent", application="dev", task_id=task_id)


@pytest.fixture
def store(client: TestClient) -> ProjectStore:
    return ProjectStore(client.app.state.sessionmaker)


def order_args(project_id: uuid.UUID, **more: Any) -> dict[str, Any]:
    return {
        "project_id": str(project_id),
        "action": "create_prototype",
        "repo": "app",
        "branch": BRANCH,
        "goal": "A paging prototype",
        "expected": ["page 2 shows rows 11 to 20"],
        "acceptance": ["tests pass"],
        **more,
    }


class Setup:
    def __init__(self, client: TestClient, store: ProjectStore) -> None:
        self.client, self.store = client, store
        self.sm = client.app.state.sessionmaker
        self.queue = Queue(self.sm)
        self.tools = ProjectTools(self.sm)
        self.tools.agents = self.queue
        self.messages = MessageTools(self.sm)
        self.messages.agents = self.queue
        self.project_id = uuid.uuid4()

    async def project(self) -> uuid.UUID:
        project = await self.store.create_project(DEV_ADMIN_ID, title=f"P{uuid.uuid4().hex[:6]}")
        self.project_id = project.id
        return project.id

    async def draft(self, **more: Any) -> dict[str, Any]:
        out = await self.tools.run_tool(
            planner(), "project.order.draft", order_args(self.project_id, **more)
        )
        return out["output"]

    async def mandate(self, **more: Any) -> Any:
        args: dict[str, Any] = {
            "repo": "app",
            "branch": BRANCH,
            "actions": list(ACTIONS),
            "text": "Go ahead with the paging prototype",
        }
        return await self.store.create_mandate(DEV_ADMIN_ID, self.project_id, **{**args, **more})


@pytest.fixture
async def setup(client: TestClient, store: ProjectStore) -> Setup:
    s = Setup(client, store)
    await s.project()
    return s


# --- the mandate itself ---


async def test_a_mandate_names_one_exact_branch_below_topcoat(setup: Setup) -> None:
    for branch in ("main", "topcoat/*", "topcoat/", "topcoat/a b", "topcoat/../main", "feature/x"):
        with pytest.raises(Invalid):
            await setup.mandate(branch=branch)
    with pytest.raises(Invalid):
        await setup.mandate(actions=["merge"])
    with pytest.raises(Invalid):
        await setup.mandate(actions=[])
    for days in (0, 91):
        with pytest.raises(Invalid):
            await setup.mandate(days=days)
    got = await setup.mandate()
    assert (got.repo, got.branch, got.actions, got.push) == (
        "app",
        BRANCH,
        ["create_prototype", "fix"],
        True,
    )
    assert got.expires_at - clock.hub_now() > timedelta(days=13)
    # the database holds the same rule
    engine = create_engine(TEST_DB_URL)
    with pytest.raises(Exception, match="ck_project_mandates_branch"), engine.begin() as conn:
        conn.execute(
            text("UPDATE project_mandates SET branch = 'main' WHERE id = :i"), {"i": got.id}
        )
    engine.dispose()


async def test_a_mandate_belongs_to_its_owner_and_its_project(
    setup: Setup, client: TestClient
) -> None:
    got = await setup.mandate()
    stranger, _ = make_user(TEST_DB_URL, "stranger mandate", role="admin")
    with pytest.raises(NotFound):
        await setup.store.get_mandate(stranger, got.id)
    with pytest.raises(NotFound):
        await setup.store.mandates_of(stranger, setup.project_id)
    with pytest.raises(NotFound):
        await setup.store.revoke_mandate(stranger, got.id)
    other = await setup.store.create_project(DEV_ADMIN_ID, title="Other")
    assert await setup.store.find_mandate(DEV_ADMIN_ID, other.id, "app", BRANCH, "fix") is None


async def test_a_mandate_is_found_only_for_an_exact_match_while_it_is_active(setup: Setup) -> None:
    got = await setup.mandate()
    find = setup.store.find_mandate
    pid = setup.project_id
    assert (await find(DEV_ADMIN_ID, pid, "app", BRANCH, "fix")).id == got.id
    assert await find(DEV_ADMIN_ID, pid, "app", "topcoat/other", "fix") is None
    assert await find(DEV_ADMIN_ID, pid, "other-repo", BRANCH, "fix") is None
    assert await find(DEV_ADMIN_ID, pid, "app", BRANCH, "merge") is None
    await setup.store.revoke_mandate(DEV_ADMIN_ID, got.id)
    assert await find(DEV_ADMIN_ID, pid, "app", BRANCH, "fix") is None
    with pytest.raises(Invalid):  # a revoked mandate cannot be used
        await setup.store.use_mandate(DEV_ADMIN_ID, got.id, what="order")
    expired = await setup.mandate()
    engine = create_engine(TEST_DB_URL)
    with engine.begin() as conn:
        conn.execute(
            text(
                "UPDATE project_mandates SET expires_at = now() - interval '1 hour' WHERE id = :i"
            ),
            {"i": expired.id},
        )
    engine.dispose()
    assert await find(DEV_ADMIN_ID, pid, "app", BRANCH, "fix") is None


async def test_every_creation_use_and_revocation_is_a_line_of_the_akte(setup: Setup) -> None:
    got = await setup.mandate()
    await setup.store.with_actor(SYSTEM).use_mandate(DEV_ADMIN_ID, got.id, what="order")
    await setup.store.revoke_mandate(DEV_ADMIN_ID, got.id)
    kinds = [e.kind for e in await setup.store.timeline(DEV_ADMIN_ID, setup.project_id, limit=200)]
    assert [k for k in kinds if k.startswith("mandate.")] == [
        "mandate.created",
        "mandate.used",
        "mandate.revoked",
    ]


# --- the order ---


async def test_an_order_without_a_mandate_waits_for_a_click(setup: Setup) -> None:
    out = await setup.draft()
    assert out["message"]["state"] == "awaiting_click" and out["message"]["type"] == "project.order"
    assert out["message"]["to"] == "dev" and setup.queue.started == []
    assert out["title"] == f"create_prototype {BRANCH}"


async def test_the_agent_cannot_order_outside_topcoat_or_with_a_loose_schema(setup: Setup) -> None:
    for branch in ("main", "release/1", "topcoat/../main"):
        with pytest.raises(ToolError) as refused:
            await setup.draft(branch=branch)
        assert refused.value.status == 422
    with pytest.raises(ToolError):
        await setup.draft(action="merge")
    with pytest.raises(ToolError):  # a field the schema does not know
        await setup.tools.run_tool(
            planner(), "project.order.draft", {**order_args(setup.project_id), "push": True}
        )
    assert setup.queue.started == []


async def test_a_matching_mandate_sends_the_order_and_counts_the_use(setup: Setup) -> None:
    mandate = await setup.mandate()
    out = await setup.draft()
    assert out["message"]["state"] == "queued" and out["queue_note"] == ""
    started = setup.queue.started[-1]
    assert started["application"] == "dev" and "dev.message.send" in started["prompt"]
    assert BRANCH in started["prompt"] and "no push" in started["prompt"]
    assert out["message"]["stats"]["mandate_id"] == str(mandate.id)
    assert (await setup.store.get_mandate(DEV_ADMIN_ID, mandate.id)).uses == 1


async def test_fix_rounds_stay_inside_but_another_branch_or_repo_needs_a_click(
    setup: Setup,
) -> None:
    await setup.mandate()
    fix = await setup.draft(action="fix", goal="Fix paging")
    assert fix["message"]["state"] == "queued"
    for more in ({"branch": "topcoat/second"}, {"repo": "other"}):
        out = await setup.draft(**more)
        assert out["message"]["state"] == "awaiting_click", more
    assert len(setup.queue.started) == 1  # "thirty branches for one prototype" cannot happen


async def test_an_ended_mandate_sends_nothing_and_a_revoked_one_stops_a_waiting_order(
    setup: Setup,
) -> None:
    mandate = await setup.mandate()
    await setup.store.revoke_mandate(DEV_ADMIN_ID, mandate.id)
    assert (await setup.draft())["message"]["state"] == "awaiting_click"
    # an order that was sent under a mandate, revoked before it left, waits for a click again
    second = await setup.mandate()
    service = MessageService(ProjectStore(setup.sm, SYSTEM), setup.queue)
    order, _ = await service.draft_order(
        DEV_ADMIN_ID, True, setup.project_id,
        payload={"action": "fix", "repo": "app", "branch": BRANCH, "spec": {"goal": "x"}},
    )  # fmt: skip
    assert order.msg_state == "queued"
    await setup.store.revoke_mandate(DEV_ADMIN_ID, second.id)
    sent = await service._as(SYSTEM).add_message(
        DEV_ADMIN_ID, setup.project_id, type="project.order", sender="project", receiver="dev",
        payload={"action": "fix", "repo": "app", "branch": BRANCH, "spec": {"goal": "y"}},
        state="sent", chain_id=order.chain_id, stats={"mandate_id": str(second.id)},
    )  # fmt: skip
    held, note = await service.queue(DEV_ADMIN_ID, True, sent)
    assert held.msg_state == "awaiting_click" and "waits for a click" in note
    assert len(setup.queue.started) == 1  # only the first one ever left


# --- the click ---


def _release(client: TestClient, order_id: str, who: dict[str, str] = AUTH, **body: Any):
    return client.post(f"/api/project/nodes/{order_id}/mandate", headers=who, json=body)


async def test_the_person_releases_an_order_and_the_mandate_comes_from_the_order(
    setup: Setup, client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(client.app.state.agents, "start", setup.queue.start)
    order = (await setup.draft())["id"]
    done = _release(client, order, days=7, text="Paging only", push=False)
    assert done.status_code == 200, done.text
    mandate = done.json()["mandate"]
    assert (mandate["repo"], mandate["branch"], mandate["actions"]) == (
        "app", BRANCH, ["create_prototype", "fix"],
    )  # fmt: skip
    assert mandate["push"] is False and mandate["text"] == "Paging only" and mandate["uses"] == 1
    assert mandate["state"] == "active" and "merging" in mandate["not_covered"]
    assert done.json()["order"]["message"]["state"] == "queued"
    assert setup.queue.started[-1]["application"] == "dev"
    again = _release(client, order)  # the order is out: no second mandate from the same card
    assert again.status_code == 422
    listed = client.get(f"/api/project/projects/{setup.project_id}/mandates", headers=AUTH)
    assert [m["id"] for m in listed.json()["mandates"]] == [mandate["id"]]


async def test_the_body_cannot_widen_what_the_card_showed(
    setup: Setup, client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(client.app.state.agents, "start", setup.queue.start)
    order = (await setup.draft())["id"]
    for extra in ({"branch": "main"}, {"repo": "other"}, {"actions": ["merge"]}, {"days": 0}):
        assert _release(client, order, **extra).status_code == 422, extra
    assert (await setup.store.mandates_of(DEV_ADMIN_ID, setup.project_id)) == []


async def test_an_api_key_and_a_plain_user_cannot_give_or_take_back_a_mandate(
    setup: Setup, client: TestClient
) -> None:
    order = (await setup.draft())["id"]
    mandate = await setup.mandate()
    _, admin_key = make_user(TEST_DB_URL, "admin key", role="admin")
    _, user_key = make_user(TEST_DB_URL, "plain key", role="user")
    for who in (admin_key, user_key):
        assert _release(client, order, who).status_code in (401, 403)
        revoke = client.post(f"/api/project/mandates/{mandate.id}/revoke", headers=who)
        assert revoke.status_code in (401, 403)
    assert (await setup.store.get_mandate(DEV_ADMIN_ID, mandate.id)).revoked_at is None
    assert (
        await setup.store.get_node(DEV_ADMIN_ID, uuid.UUID(order))
    ).msg_state == "awaiting_click"


async def test_the_person_revokes_and_it_holds_at_once(setup: Setup, client: TestClient) -> None:
    mandate = await setup.mandate()
    done = client.post(f"/api/project/mandates/{mandate.id}/revoke", headers=AUTH)
    assert done.status_code == 200 and done.json()["mandate"]["state"] == "revoked"
    assert (await setup.draft())["message"]["state"] == "awaiting_click"
    missing = client.post(f"/api/project/mandates/{uuid.uuid4()}/revoke", headers=AUTH)
    assert missing.status_code == 404


# --- the answer of Development ---


class PushStub:
    def __init__(self, outcome: MandatePush) -> None:
        self.outcome, self.calls = outcome, []

    async def push_under_mandate(self, user_id: uuid.UUID, mandate_id: uuid.UUID, message: str):
        self.calls.append((mandate_id, message))
        return self.outcome


async def _delivered(setup: Setup, stub: Any, **result: Any) -> tuple[Any, Any, Any]:
    mandate = await setup.mandate()
    setup.messages.proposals = stub
    order = await setup.draft()
    task_id = uuid.UUID(order["message"]["task_id"])
    await setup.messages.run_tool(
        developer(task_id),
        "dev.message.send",
        {
            "reply_to": order["id"],
            "kind": "result",
            "summary": "Paging works.",
            "branch": BRANCH,
            "acceptance": [{"item": "tests pass", "met": True}],
            **result,
        },
    )
    rows = await setup.store.messages_of(DEV_ADMIN_ID, setup.project_id, type="dev.result")
    return mandate, order, rows[-1]


async def _accept(setup: Setup, result: Any, verdict: str = "accept", **more: Any) -> None:
    await setup.messages.run_tool(
        reviewer(result.task_id),
        "project.review.submit",
        {"message_id": str(result.id), "verdict": verdict, "reasons": "because", **more},
    )


async def test_the_result_of_development_is_checked_against_the_order(setup: Setup) -> None:
    _, _, result = await _delivered(
        setup, PushStub(MandatePush(True, 1)), tests={"state": "passed"}, head="abc123"
    )
    assert result.message_type == "dev.result" and result.trust == "system"
    assert result.msg_state == "reviewing" and result.verification["ok"] is True
    assert result.verification["self_reported"] == ["head", "tests"]
    assert "Paging works." in setup.queue.started[-1]["prompt"]  # the reviewer reads it


async def test_a_wrong_branch_failed_tests_or_unmet_acceptance_show_in_the_checks(
    setup: Setup,
) -> None:
    _, _, result = await _delivered(
        setup,
        PushStub(MandatePush(True, 1)),
        branch="topcoat/elsewhere",
        tests={"state": "failed"},
        acceptance=[{"item": "tests pass", "met": False}],
    )
    issues = " ".join(result.verification["issues"])
    assert result.verification["ok"] is False
    assert "topcoat/elsewhere is not the ordered branch" in issues
    assert "the tests failed" in issues and "acceptance not met: tests pass" in issues


async def test_an_accepted_answer_under_a_mandate_pushes_the_mandated_branch(setup: Setup) -> None:
    stub = PushStub(MandatePush(True, 1))
    mandate, _, result = await _delivered(setup, stub)
    await _accept(setup, result)
    ((used, message),) = stub.calls
    assert used == mandate.id and BRANCH in message and "A paging prototype" in message
    facts = await setup.store.messages_of(DEV_ADMIN_ID, setup.project_id, type="project.fact")
    assert len(facts) == 1 and BRANCH in facts[0].body and "mandate" in facts[0].body


async def test_an_order_released_by_a_click_alone_is_not_pushed(setup: Setup) -> None:
    stub = PushStub(MandatePush(True, 1))
    setup.messages.proposals = stub
    order = await setup.draft()  # no mandate: awaiting a click
    sent = await setup.store.with_actor(SYSTEM).set_message_state(
        DEV_ADMIN_ID, uuid.UUID(order["id"]), "sent", reason="clicked"
    )
    service = MessageService(ProjectStore(setup.sm, SYSTEM), setup.queue)
    sent, _ = await service.queue(DEV_ADMIN_ID, True, sent)
    await setup.messages.run_tool(
        developer(sent.task_id),
        "dev.message.send",
        {"reply_to": str(sent.id), "kind": "result", "summary": "Done.", "branch": BRANCH},
    )
    result = (await setup.store.messages_of(DEV_ADMIN_ID, setup.project_id, type="dev.result"))[-1]
    await _accept(setup, result)
    assert stub.calls == []  # accepted and not pushed: the push is the person's own click


async def test_a_stopped_push_leaves_a_question_and_a_line_and_nothing_else(setup: Setup) -> None:
    stub = PushStub(MandatePush(False, 1, "refusal", "secrets_found", "looks like a secret"))
    mandate, _, result = await _delivered(setup, stub)
    await _accept(setup, result)
    asked = await setup.store.messages_of(DEV_ADMIN_ID, setup.project_id, type="project.question")
    assert len(asked) == 1 and "looks like a secret" in asked[0].title
    assert "refusal" in asked[0].title
    kinds = [e.kind for e in await setup.store.timeline(DEV_ADMIN_ID, setup.project_id, limit=200)]
    assert "mandate.push_stopped" in kinds
    assert (await setup.store.get_mandate(DEV_ADMIN_ID, mandate.id)).uses == 1  # the order only


async def test_a_fix_round_goes_out_by_itself_inside_the_mandate(setup: Setup) -> None:
    mandate, _, result = await _delivered(setup, PushStub(MandatePush(True, 1)))
    await _accept(setup, result, "revise", followups=["page 2 is off by one"])
    orders = await setup.store.messages_of(DEV_ADMIN_ID, setup.project_id, type="project.order")
    assert len(orders) == 2
    fix = orders[-1]
    assert fix.payload["action"] == "fix" and fix.payload["branch"] == BRANCH
    assert fix.payload["findings"] == ["page 2 is off by one"] and fix.round == 2
    assert fix.payload["previous_result"] == str(result.id) and fix.in_reply_to == result.id
    assert fix.msg_state == "queued" and fix.chain_id == orders[0].chain_id
    assert (await setup.store.get_mandate(DEV_ADMIN_ID, mandate.id)).uses == 2


async def test_a_fix_round_without_a_mandate_any_more_waits_for_a_click(setup: Setup) -> None:
    mandate, _, result = await _delivered(setup, PushStub(MandatePush(True, 1)))
    await setup.store.revoke_mandate(DEV_ADMIN_ID, mandate.id)
    await _accept(setup, result, "revise", followups=["again"])
    fix = (await setup.store.messages_of(DEV_ADMIN_ID, setup.project_id, type="project.order"))[-1]
    assert fix.msg_state == "awaiting_click" and fix.round == 2


async def test_only_development_answers_an_order_and_only_once(setup: Setup) -> None:
    await setup.mandate()
    order = await setup.draft()
    task_id = uuid.UUID(order["message"]["task_id"])
    body = {"reply_to": order["id"], "kind": "result", "summary": "Done.", "branch": BRANCH}
    research = Caller(DEV_ADMIN_ID, True, "agent", application="research", task_id=task_id)
    with pytest.raises(ToolError) as wrong_app:  # research was not asked
        await setup.messages.run_tool(research, "research.message.send", body)
    assert wrong_app.value.status in (404, 409, 422)
    with pytest.raises(ToolError) as loose:  # a field the dev schema does not know
        await setup.messages.run_tool(developer(task_id), "dev.message.send", {**body, "push": 1})
    assert loose.value.status == 422
    await setup.messages.run_tool(developer(task_id), "dev.message.send", body)
    with pytest.raises(ToolError) as twice:
        await setup.messages.run_tool(developer(task_id), "dev.message.send", body)
    assert twice.value.status == 409


async def test_a_dev_run_that_ends_without_the_tool_is_answered_from_its_summary(
    setup: Setup,
) -> None:
    from topcoat_hub.agent_store import AgentStore

    await setup.mandate()
    order = await setup.draft()
    task_id = uuid.UUID(order["message"]["task_id"])
    async with setup.sm() as db:
        agent_store = AgentStore(db, DEV_ADMIN_ID)
        task = await agent_store.get(task_id)
        await agent_store.set_status(task, "done", summary="I added paging.")
    service = MessageService(ProjectStore(setup.sm, SYSTEM), setup.queue)
    await service.on_task_event(DEV_ADMIN_ID, task_id, "done")
    result = (await setup.store.messages_of(DEV_ADMIN_ID, setup.project_id, type="dev.result"))[0]
    assert result.payload["summary"] == "I added paging."
    assert "without sending its structured result" in result.payload["open_points"][0]


# --- the push through the real proposal service ---


@pytest.fixture
def push_world(client: TestClient, connect, proposals_on, repos):  # noqa: F811
    repos("app", "acme/app", "test-branch")
    _, node = dev_node(client, connect)
    store = ProjectStore(client.app.state.sessionmaker)
    sm = client.app.state.sessionmaker

    def make(**more: Any) -> tuple[uuid.UUID, uuid.UUID]:
        async def go() -> tuple[uuid.UUID, uuid.UUID]:
            project = await store.create_project(DEV_ADMIN_ID, title=f"Push{uuid.uuid4().hex[:5]}")
            mandate = await store.create_mandate(
                DEV_ADMIN_ID, project.id, repo="app", branch="topcoat/fix",
                actions=["create_prototype", "fix"], **more,
            )  # fmt: skip
            return project.id, mandate.id

        return client.portal.call(go)

    def push(mandate_id: uuid.UUID) -> MandatePush:
        return client.portal.call(
            client.app.state.proposals.push_under_mandate, DEV_ADMIN_ID, mandate_id, "Fix paging"
        )

    def uses(mandate_id: uuid.UUID) -> int:
        return client.portal.call(store.get_mandate, DEV_ADMIN_ID, mandate_id).uses

    return node, make, push, uses, store, sm


def test_a_push_under_a_mandate_goes_through_the_same_checks_and_the_same_path(
    push_world, client: TestClient
) -> None:
    node, make, push, uses, _, _ = push_world
    _, mandate_id = make()
    out = push(mandate_id)
    assert out.pushed and out.attempts == 1 and out.proposal["status"] == "executed"
    call = node.execute_calls[-1]
    assert call["capability"] == "repo.push" and call["input"]["diff_sha256"] == DIFF["sha256"]
    assert call["input"]["branch"] == "topcoat/fix" and call["hash"] == out.proposal["hash"]
    assert [c["capability"] for c in node.capability_calls][-1] == "repo.diff"  # the real diff
    assert uses(mandate_id) == 1
    engine = create_engine(TEST_DB_URL)
    with engine.connect() as conn:
        row = conn.execute(
            text("SELECT via FROM tool_calls WHERE capability = 'repo.push' ORDER BY id DESC")
        ).first()
    engine.dispose()
    assert row.via == "proposal"  # audited like a click


@pytest.mark.parametrize(
    ("answer", "code"),
    [
        ({"empty": True}, "no_change"),
        ({"truncated": True}, "too_big"),
        ({"masked": 2}, "secrets_found"),
        ({"branch": "main"}, "not_on_work_branch"),
    ],
)
def test_a_refusal_of_the_checks_stops_at_once_and_nothing_is_pushed(
    push_world, answer: dict[str, Any], code: str
) -> None:
    node, make, push, uses, _, _ = push_world
    node.capability_answers["repo.diff"] = {**DIFF, **answer}
    _, mandate_id = make()
    out = push(mandate_id)
    assert (out.pushed, out.stopped, out.code, out.attempts) == (False, "refusal", code, 1)
    assert node.execute_calls == [] and uses(mandate_id) == 0


def test_a_change_on_another_branch_is_outside_the_mandate_and_its_card_is_refused(
    push_world, client: TestClient
) -> None:
    node, make, push, uses, _, _ = push_world
    node.capability_answers["repo.diff"] = {**DIFF, "branch": "topcoat/other"}
    _, mandate_id = make()
    out = push(mandate_id)
    assert (out.stopped, out.code) == ("refusal", "outside_mandate")
    assert node.execute_calls == [] and uses(mandate_id) == 0
    assert out.proposal["status"] == "pending"  # the card as it was made ...
    shown = client.get(f"/api/agent/proposals/{out.proposal['id']}", headers=AUTH).json()
    assert shown["status"] == "refused"  # ... is not left lying around to be clicked


def test_only_technical_errors_are_tried_again_and_not_the_same_one_twice(push_world) -> None:
    node, make, push, uses, _, _ = push_world
    node.capability_answers["repo.push"] = {"error": {"code": "timeout", "message": "slow"}}
    _, mandate_id = make()
    out = push(mandate_id)
    assert (out.pushed, out.stopped, out.code, out.attempts) == (False, "same_error", "timeout", 2)
    assert len(node.execute_calls) == 2 and uses(mandate_id) == 0


def test_a_technical_error_followed_by_success_pushes(push_world) -> None:
    node, make, push, uses, _, _ = push_world
    seen: list[int] = []

    def flaky(_: dict[str, Any]) -> dict[str, Any]:
        seen.append(1)
        if len(seen) == 1:
            return {"error": {"code": "timeout", "message": "slow"}}
        return {"pushed": True, "branch": "topcoat/fix", "commit": "abc123"}

    node.capability_answers["repo.push"] = flaky
    _, mandate_id = make()
    out = push(mandate_id)
    assert out.pushed and out.attempts == 2 and uses(mandate_id) == 1


def test_a_revoked_or_expired_mandate_pushes_nothing_and_reads_nothing(
    push_world, client: TestClient
) -> None:
    node, make, push, _, store, _ = push_world
    _, revoked = make()
    client.portal.call(store.revoke_mandate, DEV_ADMIN_ID, revoked)
    _, expired = make()
    engine = create_engine(TEST_DB_URL)
    with engine.begin() as conn:
        conn.execute(
            text(
                "UPDATE project_mandates SET expires_at = now() - interval '1 minute' WHERE id = :i"
            ),
            {"i": expired},
        )
    engine.dispose()
    _, no_push = make(push=False)
    for mandate_id in (revoked, expired, no_push):
        out = push(mandate_id)
        assert (out.pushed, out.stopped, out.code) == (False, "mandate", "mandate_ended")
    assert node.capability_calls == [] and node.execute_calls == []
    assert push(uuid.uuid4()).code == "no_mandate"


def test_the_click_path_still_asks_for_its_click(push_world, client: TestClient) -> None:
    node = push_world[0]
    made = client.post(
        "/api/dev/repos/app/push-proposal", headers=AUTH, json={"message": "Fix paging"}
    )
    assert made.status_code == 200 and made.json()["status"] == "pending"
    assert node.execute_calls == []  # nothing runs without the click, mandate or not
    assert json.dumps(made.json()).find("mandate") == -1
