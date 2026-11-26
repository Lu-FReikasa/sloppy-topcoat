"""Messages between the applications: the registry, the store and the routes (PPH-05, 05a).

Scope: a type, an edge or a payload the registry does not list is refused; a message is a report of
the receiving project with the columns of a message; the state moves only along its table; a stopped
chain takes no new message; everything is scoped by the person; the routes show it all."""

import uuid

import pytest
from fastapi.testclient import TestClient

from tests.conftest import AUTH, TEST_DB_URL
from tests.users import make_user
from topcoat_hub import messages
from topcoat_hub.models import DEV_ADMIN_ID
from topcoat_hub.project_events import Actor
from topcoat_hub.project_store import Invalid, NotFound, ProjectStore

REQUEST = {"topic": "Which framework?", "questions": ["Which is fastest?"], "min_sources": 2}
RESULT = {
    "summary": "FastAPI is fast.",
    "findings": [{"claim": "fast", "quote": "very fast", "source_url": "https://a.example/x"}],
}


@pytest.fixture
def store(client: TestClient) -> ProjectStore:
    return ProjectStore(client.app.state.sessionmaker)


class TestRegistry:
    def test_every_type_starts_with_its_sending_application(self) -> None:
        reg = messages.load()
        assert len(reg.types) == 26
        for spec in reg.types.values():
            assert spec.name.split(".")[0] in spec.senders
            assert spec.trust in messages.TRUSTS and spec.effect in messages.EFFECTS
            assert spec.label("en") and spec.label("de")
        assert reg.states[0] == "draft"

    def test_it_knows_who_may_send_what_to_whom(self) -> None:
        assert messages.check_edge("project.request", "project", "research")
        assert messages.check_edge("dev.request", "dev", "project")
        for bad in (
            ("project.request", "research", "project"),  # the wrong way round
            ("project.order", "project", "research"),  # orders go to development only
            ("research.result", "dev", "project"),  # the prefix is the sender
            ("admin.request", "admin", "dev"),
        ):
            with pytest.raises(messages.MessageError) as exc:
                messages.check_edge(*bad)
            assert exc.value.code == "bad_edge"
        with pytest.raises(messages.MessageError) as exc:
            messages.get("project.shell")
        assert exc.value.code == "unknown_type"

    def test_it_checks_the_payload(self) -> None:
        messages.validate("project.request", "project", "research", REQUEST)
        for payload in (
            {},
            {"topic": ""},
            {"topic": "x", "format": "novel"},
            {"topic": "x", "surprise": 1},
        ):
            with pytest.raises(messages.MessageError) as exc:
                messages.validate("project.request", "project", "research", payload)
            assert exc.value.code == "bad_payload"
        with pytest.raises(messages.MessageError):  # a finding needs its quote
            messages.validate(
                "research.result",
                "research",
                "project",
                {"summary": "s", "findings": [{"claim": "c"}]},
            )
        messages.validate("research.result", "research", "project", RESULT)

    def test_trust_follows_the_sender(self) -> None:
        assert messages.get("research.result").trust == "foreign"
        assert messages.get("research.result").needs_review
        assert messages.get("dev.result").trust == "system"
        assert messages.get("project.note").trust == "own"
        assert messages.get("project.order").effect == "mandate"

    def test_a_report_kind_is_made_of_a_type(self) -> None:
        assert messages.report_kind_of("project.note") == "note"
        assert messages.report_kind_of("project.fact") == "fact"
        assert messages.report_kind_of("dev.request") == "request"
        assert messages.report_kind_of("project.request") == "request"
        assert messages.report_kind_of("research.result") == "message"
        assert messages.report_kind_of("project.order") == "message"

    def test_a_message_reads_as_text_made_by_code(self) -> None:
        text = messages.body_of("project.request", REQUEST)
        assert text.startswith("Which framework?") and "- Which is fastest?" in text
        assert "very fast" in messages.body_of("research.result", RESULT)
        assert messages.title_of("project.request", REQUEST) == "Which framework?"
        assert (
            messages.title_of("research.failure", {"reason": "no_sources"}) == "Failed: no_sources"
        )

    def test_responsibilities_are_in_both_languages(self) -> None:
        for app in ("project", "research", "dev", "admin"):
            assert messages.responsibility(app, "en") and messages.responsibility(app, "de")
        assert messages.responsibility("nobody") == ""


class TestStore:
    async def test_a_request_is_a_report_with_the_columns_of_a_message(
        self, store: ProjectStore
    ) -> None:
        project = await store.create_project(DEV_ADMIN_ID, title="Msg")
        chain = await store.start_chain(DEV_ADMIN_ID, project.id, title="Frameworks")
        node = await store.add_message(
            DEV_ADMIN_ID,
            project.id,
            type="project.request",
            sender="project",
            receiver="research",
            payload=REQUEST,
            state="sent",
            chain_id=chain.id,
        )
        assert (node.kind, node.report_kind, node.number) == ("report", "request", "1")
        assert (node.message_type, node.source_app, node.target_app) == (
            "project.request",
            "project",
            "research",
        )
        assert (node.msg_state, node.round, node.trust, node.chain_id) == (
            "sent",
            1,
            "own",
            chain.id,
        )
        assert node.payload == REQUEST and node.title == "Which framework?"
        events = await store.timeline(DEV_ADMIN_ID, project.id, limit=50)
        assert [e.kind for e in events][-2:] == ["chain.started", "message.sent"]
        sent = events[-1]
        assert sent.chain_id == chain.id and sent.payload["type"] == "project.request"

    async def test_an_incoming_message_is_filed_as_received_with_its_trust(
        self, store: ProjectStore
    ) -> None:
        project = await store.create_project(DEV_ADMIN_ID, title="Incoming")
        request = await store.add_message(
            DEV_ADMIN_ID,
            project.id,
            type="project.request",
            sender="project",
            receiver="research",
            payload=REQUEST,
        )
        result = await store.add_message(
            DEV_ADMIN_ID,
            project.id,
            type="research.result",
            sender="research",
            receiver="project",
            payload=RESULT,
            state="delivered",
            in_reply_to=request.id,
            round=2,
        )
        assert result.trust == "foreign" and result.in_reply_to == request.id and result.round == 2
        assert result.report_kind == "message" and "very fast" in result.body
        last = (await store.timeline(DEV_ADMIN_ID, project.id, limit=50))[-1]
        assert last.kind == "message.received" and last.payload["trust"] == "foreign"

    async def test_the_registry_refuses_what_it_does_not_list(self, store: ProjectStore) -> None:
        project = await store.create_project(DEV_ADMIN_ID, title="Refuse")
        for kwargs in (
            {
                "type": "project.request",
                "sender": "research",
                "receiver": "project",
                "payload": REQUEST,
            },
            {"type": "project.request", "sender": "project", "receiver": "research", "payload": {}},
            {"type": "made.up", "sender": "project", "receiver": "research", "payload": {}},
        ):
            with pytest.raises(Invalid):
                await store.add_message(DEV_ADMIN_ID, project.id, **kwargs)
        with pytest.raises(Invalid):
            await store.add_message(
                DEV_ADMIN_ID,
                project.id,
                type="project.request",
                sender="project",
                receiver="research",
                payload=REQUEST,
                state="flying",
            )
        assert await store.tree(DEV_ADMIN_ID, project.id) == []  # nothing was filed

    async def test_the_state_moves_along_its_table_and_every_move_is_a_line(
        self, store: ProjectStore
    ) -> None:
        project = await store.create_project(DEV_ADMIN_ID, title="States")
        node = await store.add_message(
            DEV_ADMIN_ID,
            project.id,
            type="project.request",
            sender="project",
            receiver="dev",
            payload=REQUEST,
            state="awaiting_click",
        )
        with pytest.raises(Invalid):
            await store.set_message_state(DEV_ADMIN_ID, node.id, "delivered")  # not yet sent
        task = uuid.uuid4()
        for state in ("sent", "queued"):
            node = await store.set_message_state(DEV_ADMIN_ID, node.id, state, reason=f"to {state}")
        node = await store.set_message_state(
            DEV_ADMIN_ID, node.id, "running", stats={"queued_at": "t0"}, verification={"ok": True}
        )
        node = await store.set_message_state(DEV_ADMIN_ID, node.id, "delivered", stats={"steps": 3})
        assert node.stats == {"queued_at": "t0", "steps": 3} and node.verification == {"ok": True}
        node = await store.set_message_state(DEV_ADMIN_ID, node.id, "accepted")
        with pytest.raises(Invalid):
            await store.set_message_state(DEV_ADMIN_ID, node.id, "running")  # an end is an end
        lines = [
            e
            for e in await store.timeline(DEV_ADMIN_ID, project.id, limit=50)
            if e.kind == "message.state_changed"
        ]
        assert [(e.payload["from"], e.payload["to"]) for e in lines] == [
            ("awaiting_click", "sent"),
            ("sent", "queued"),
            ("queued", "running"),
            ("running", "delivered"),
            ("delivered", "accepted"),
        ]
        assert task  # (the id of a task is attached by the service, tested with it)

    async def test_a_node_that_is_no_message_has_no_state(self, store: ProjectStore) -> None:
        project = await store.create_project(DEV_ADMIN_ID, title="Plain")
        task = await store.create_node(DEV_ADMIN_ID, project.id, kind="task", title="T")
        with pytest.raises(Invalid):
            await store.set_message_state(DEV_ADMIN_ID, task.id, "sent")

    async def test_the_rounds_of_a_chain_count_and_a_long_chain_is_flagged_once(
        self, store: ProjectStore
    ) -> None:
        project = await store.create_project(DEV_ADMIN_ID, title="Rounds")
        chain = await store.start_chain(DEV_ADMIN_ID, project.id)
        for n in (1, 2, 4):
            await store.add_message(
                DEV_ADMIN_ID,
                project.id,
                type="project.request",
                sender="project",
                receiver="research",
                payload={"topic": f"round {n}"},
                chain_id=chain.id,
                round=n,
            )
        assert (await store.get_chain(DEV_ADMIN_ID, chain.id)).rounds == 4
        assert await store.flag_chain(DEV_ADMIN_ID, chain.id, "round_4") is True
        assert await store.flag_chain(DEV_ADMIN_ID, chain.id, "slow") is False
        flagged = await store.get_chain(DEV_ADMIN_ID, chain.id)
        assert flagged.flag_reason == "round_4" and flagged.flagged_at is not None

    async def test_stopping_a_chain_ends_what_is_on_its_way_and_refuses_news(
        self, store: ProjectStore
    ) -> None:
        project = await store.create_project(DEV_ADMIN_ID, title="Stop")
        chain = await store.start_chain(DEV_ADMIN_ID, project.id)
        task = uuid.uuid4()
        running = await store.add_message(
            DEV_ADMIN_ID,
            project.id,
            type="project.request",
            sender="project",
            receiver="research",
            payload=REQUEST,
            chain_id=chain.id,
            state="running",
        )
        done = await store.add_message(
            DEV_ADMIN_ID,
            project.id,
            type="project.request",
            sender="project",
            receiver="dev",
            payload=REQUEST,
            chain_id=chain.id,
            state="accepted",
        )
        stopped = await store.stop_chain(DEV_ADMIN_ID, chain.id)
        assert [n.id for n, _ in stopped] == [running.id]
        by_id = {n.id: n for n in await store.messages_of(DEV_ADMIN_ID, project.id)}
        assert by_id[running.id].msg_state == "stopped" and by_id[done.id].msg_state == "accepted"
        assert (await store.get_chain(DEV_ADMIN_ID, chain.id)).status == "stopped"
        assert await store.stop_chain(DEV_ADMIN_ID, chain.id) == []  # a second stop changes nothing
        with pytest.raises(Invalid):
            await store.add_message(
                DEV_ADMIN_ID,
                project.id,
                type="research.result",
                sender="research",
                receiver="project",
                payload=RESULT,
                chain_id=chain.id,
            )
        assert task

    async def test_a_chain_is_done_when_nothing_is_on_its_way(self, store: ProjectStore) -> None:
        project = await store.create_project(DEV_ADMIN_ID, title="Done")
        chain = await store.start_chain(DEV_ADMIN_ID, project.id)
        node = await store.add_message(
            DEV_ADMIN_ID,
            project.id,
            type="project.request",
            sender="project",
            receiver="research",
            payload=REQUEST,
            chain_id=chain.id,
        )
        await store.finish_chain(DEV_ADMIN_ID, chain.id)
        assert (await store.get_chain(DEV_ADMIN_ID, chain.id)).status == "active"
        for state in ("queued", "running", "delivered", "accepted"):
            await store.set_message_state(DEV_ADMIN_ID, node.id, state)
        await store.finish_chain(DEV_ADMIN_ID, chain.id)
        assert (await store.get_chain(DEV_ADMIN_ID, chain.id)).status == "done"

    async def test_a_message_can_be_found_by_its_task(
        self, store: ProjectStore, client: TestClient
    ) -> None:
        project = await store.create_project(DEV_ADMIN_ID, title="Task")
        node = await store.add_message(
            DEV_ADMIN_ID,
            project.id,
            type="project.request",
            sender="project",
            receiver="research",
            payload=REQUEST,
        )
        assert await store.message_for_task(DEV_ADMIN_ID, uuid.uuid4()) is None
        assert node.task_id is None

    async def test_everything_is_the_persons_own(self, store: ProjectStore) -> None:
        other, _ = make_user(TEST_DB_URL, "Other msg")
        mine = await store.create_project(DEV_ADMIN_ID, title="Mine")
        chain = await store.start_chain(DEV_ADMIN_ID, mine.id)
        node = await store.add_message(
            DEV_ADMIN_ID,
            mine.id,
            type="project.request",
            sender="project",
            receiver="research",
            payload=REQUEST,
            chain_id=chain.id,
        )
        with pytest.raises(NotFound):
            await store.get_chain(other, chain.id)
        with pytest.raises(NotFound):
            await store.start_chain(other, mine.id)
        with pytest.raises(NotFound):
            await store.set_message_state(other, node.id, "queued")
        with pytest.raises(NotFound):
            await store.add_message(
                other,
                mine.id,
                type="project.request",
                sender="project",
                receiver="research",
                payload=REQUEST,
            )
        with pytest.raises(NotFound):
            await store.stop_chain(other, chain.id)
        assert await store.chains_of(other) == []
        assert await store.message_for_task(other, uuid.uuid4()) is None

    async def test_a_chain_belongs_to_one_project(self, store: ProjectStore) -> None:
        a = await store.create_project(DEV_ADMIN_ID, title="A")
        b = await store.create_project(DEV_ADMIN_ID, title="B")
        chain = await store.start_chain(DEV_ADMIN_ID, a.id)
        with pytest.raises(Invalid):
            await store.add_message(
                DEV_ADMIN_ID,
                b.id,
                type="project.request",
                sender="project",
                receiver="research",
                payload=REQUEST,
                chain_id=chain.id,
            )

    async def test_an_agent_that_writes_is_named_in_the_line(self, client: TestClient) -> None:
        agent = Actor("agent", app="research", agent_task_id=uuid.uuid4())
        store = ProjectStore(client.app.state.sessionmaker, agent)
        project = await store.create_project(DEV_ADMIN_ID, title="Named")
        await store.add_message(
            DEV_ADMIN_ID,
            project.id,
            type="research.failure",
            sender="research",
            receiver="project",
            payload={"reason": "no_sources"},
            state="delivered",
        )
        last = (await store.timeline(DEV_ADMIN_ID, project.id, limit=50))[-1]
        assert (last.actor_kind, last.actor_app, last.kind) == (
            "agent",
            "research",
            "message.received",
        )


class TestRoutes:
    def test_the_registry_is_readable(self, client: TestClient) -> None:
        body = client.get("/api/project/messages/registry", headers=AUTH).json()
        assert len(body["types"]) == 26 and "dev" in body["responsibilities"]
        request = next(t for t in body["types"] if t["type"] == "project.request")
        assert request["from"] == ["project"] and request["to"] == ["research", "dev", "admin"]

    async def test_messages_and_chains_are_listed_and_filtered(
        self, client: TestClient, store: ProjectStore
    ) -> None:
        project = await store.create_project(DEV_ADMIN_ID, title="Routes")
        chain = await store.start_chain(DEV_ADMIN_ID, project.id, title="One")
        await store.add_message(
            DEV_ADMIN_ID,
            project.id,
            type="project.request",
            sender="project",
            receiver="research",
            payload=REQUEST,
            chain_id=chain.id,
        )
        await store.add_message(
            DEV_ADMIN_ID,
            project.id,
            type="project.note",
            sender="project",
            receiver="project",
            payload={},
        )
        base = f"/api/project/projects/{project.id}"
        everything = client.get(f"{base}/messages", headers=AUTH).json()["messages"]
        assert [m["message"]["type"] for m in everything] == ["project.request", "project.note"]
        assert everything[0]["message"]["chain_id"] == str(chain.id)
        by_chain = client.get(f"{base}/messages?chain={chain.id}", headers=AUTH).json()["messages"]
        assert [m["message"]["type"] for m in by_chain] == ["project.request"]
        by_type = client.get(f"{base}/messages?type=project.note", headers=AUTH).json()["messages"]
        assert len(by_type) == 1
        assert client.get(f"{base}/messages?type=made.up", headers=AUTH).status_code == 422
        assert (
            client.get(f"{base}/messages?state=sent", headers=AUTH).json()["messages"][0][
                "message"
            ]["state"]
            == "sent"
        )
        chains = client.get(f"{base}/chains", headers=AUTH).json()["chains"]
        assert [(c["title"], c["status"], c["rounds"]) for c in chains] == [("One", "active", 1)]
        one = client.get(f"/api/project/chains/{chain.id}", headers=AUTH).json()
        assert one["chain"]["id"] == str(chain.id) and len(one["messages"]) == 1

    async def test_another_person_sees_none_of_it(
        self, client: TestClient, store: ProjectStore
    ) -> None:
        project = await store.create_project(DEV_ADMIN_ID, title="Private")
        chain = await store.start_chain(DEV_ADMIN_ID, project.id)
        _, other = make_user(TEST_DB_URL, "Nosy")
        for path in (
            f"/api/project/projects/{project.id}/messages",
            f"/api/project/projects/{project.id}/chains",
            f"/api/project/chains/{chain.id}",
        ):
            assert client.get(path, headers=other).status_code == 404, path
