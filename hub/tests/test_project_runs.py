"""Runs of project tasks and batches (PPH-04, plan of 2026-11-08, S2 and S4).

Scope: a task names its application and its result form; a result fits its form only when it has a
row per checklist item and a value in every column; a finished run sets its node to in_review or
blocked by that check, never by the model's word; a batch runs its tasks in order and stops at the
first one that does not fit, at the limit of runs, or at the person's stop; a chat run is not
touched by any of this."""

import asyncio
import uuid
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from topcoat_hub.agent_prompts import Finish, check_findings
from topcoat_hub.gateway import configured_slots
from topcoat_hub.models import DEV_ADMIN_ID as USER
from topcoat_hub.models import AgentEvidence, AgentTask, ProjectNode
from topcoat_hub.node_policy import AppRow, NodePolicy
from topcoat_hub.project_check import SecondLookResult
from topcoat_hub.project_runs import (
    RunError,
    batch_places,
    check_right,
    form_of,
    result_problem,
    run_prompt,
    target_of,
    with_evidence,
)
from topcoat_hub.project_store import ProjectStore

# a free agent run: a Development task with a checklist would else be checklist_check
FORM_BODY = (
    "Zielanwendung: dev\nProgramm: frei\n"
    "Ergebnisform: Artikel | Befund\n- [ ] first\n- [ ] second\n"
)


class TestRules:
    def test_a_task_names_its_application_and_form(self) -> None:
        assert target_of(FORM_BODY) == "dev"
        assert target_of("Zielanwendung: Research\n") == "research"
        assert target_of("Zielanwendung: admin\n") == "admin"  # the administration (ADMIN_ONLY)
        assert target_of("Zielanwendung: shell\n") is None  # not a place a task may run
        assert target_of("no target here") is None
        assert form_of(FORM_BODY) == ["Artikel", "Befund"]
        assert form_of("Ergebnisform:  Artikel |  | Befund |") == ["Artikel", "Befund"]
        assert form_of("no form") == []

    def test_an_administration_task_runs_only_for_an_administrator(self) -> None:
        check_right("admin", is_admin=True)  # no error
        check_right("research", is_admin=False)  # no error: only admin is restricted
        with pytest.raises(RunError, match="only for an administrator"):
            check_right("admin", is_admin=False)

    def test_a_result_needs_a_row_per_item_with_every_column(self) -> None:
        columns = ["Artikel", "Befund"]
        good = [{"Artikel": "a", "Befund": "stimmt"}, {"artikel": "b", "befund": "falsch"}]
        assert result_problem(columns, good, items=2) is None
        assert "no table" in (result_problem(columns, None, items=2) or "")
        assert "2 items" in (result_problem(columns, good[:1], items=2) or "")
        gap = [{"Artikel": "a", "Befund": ""}, {"Artikel": "b", "Befund": "x"}]
        assert "row 1 has no value for Befund" in (result_problem(columns, gap, items=2) or "")
        assert "not an object" in (result_problem(columns, ["text", good[0]], items=0) or "")

    def test_a_column_written_with_underscores_or_with_its_place_still_fits(self) -> None:
        # a small model writes Wiki_Aussage and Beleg_Pfad for "Wiki-Aussage | Beleg (Pfad/Zeile)"
        columns = ["Wiki-Aussage", "Beleg (Pfad/Zeile)"]
        row = {"Wiki_Aussage": "Neue Looks in look.ts", "Beleg_Pfad": "akte.md: Zeile 10 (E2)"}
        assert result_problem(columns, [row], items=1, evidence={"E2"}) is None
        # the form gets one evidence column, not a second one next to "Beleg (Pfad/Zeile)"
        assert with_evidence(columns) == columns
        assert with_evidence(["Wiki-Aussage"]) == ["Wiki-Aussage", "Beleg"]
        # a column named with its hint in brackets is matched by its name alone
        hinted = [
            "Wiki-Aussage",
            "Beleg (Pfad/Zeile)",
            "Ergebnis (stimmt / falsch / nicht prüfbar)",
        ]
        full = {"Wiki-Aussage": "x", "Beleg": "E2, Zeile 11", "Ergebnis": "stimmt"}
        assert result_problem(hinted, [full], items=1, evidence={"E2"}) is None
        # a row whose reads are not the run's own is still refused
        bad = {"Wiki_Aussage": "x", "Beleg_Pfad": "akte.md: Zeile 10"}
        assert "no evidence" in (result_problem(columns, [bad], items=1, evidence={"E2"}) or "")

    def test_the_prompt_names_the_form_and_the_rows_needed(self) -> None:
        prompt = run_prompt("Looks", FORM_BODY, ["Artikel", "Befund"], items=2)
        assert "Artikel | Befund" in prompt
        assert "The checklist has 2 item(s)" in prompt
        assert FORM_BODY in prompt

    def test_a_run_in_entwicklung_is_told_how_to_find_its_way(self) -> None:
        # live 2026-11-17: a survey guessed src/web/gui/…, never read the wiki, answered in English
        from topcoat_hub.project_runs import DEV_ORIENTATION

        body = "Zielanwendung: dev\nErgebnisform: Ergebnis | Beleg\n- [ ] Werkzeuge im Chat prüfen"
        dev = run_prompt("Chat Code lesen", body, ["Ergebnis"], items=1, target="dev")
        assert DEV_ORIENTATION in dev and "wiki.list" in dev
        assert "in German, the language of the task" in dev
        assert "sentences.\nsentences" not in dev  # the doubled line is gone
        assert DEV_ORIENTATION not in run_prompt("x", body, ["Ergebnis"], items=1, target="project")

    def test_code_work_gets_the_larger_frame_of_a_task(self) -> None:
        from topcoat_hub.agent_loop import Limits
        from topcoat_hub.config import Settings
        from topcoat_hub.project_runs import task_limits

        settings = Settings.model_construct(
            task_max_tool_calls=60,
            task_max_turns=90,
            task_time_budget_seconds=2700.0,
            task_context_chars=160000,
            task_observation_chars=8000,
        )
        base = Limits()
        dev = task_limits(settings, base, "dev")
        assert dev is not None
        assert (dev.max_tool_calls, dev.context_chars, dev.observation_chars) == (60, 160000, 8000)
        assert task_limits(settings, base, "research") is None  # Research has its own frame

    def test_a_row_must_name_a_read_the_run_kept(self) -> None:
        columns = ["Artikel", "Beleg"]
        kept = {"E1"}
        ok = [{"Artikel": "a", "Beleg": "E1"}]
        assert result_problem(columns, ok, items=1, evidence=kept) is None
        invented = [{"Artikel": "a", "Beleg": "E9"}]
        assert "no evidence" in (result_problem(columns, invented, items=1, evidence=kept) or "")
        bare = [{"Artikel": "a", "Beleg": "trust me"}]
        assert "no evidence" in (result_problem(columns, bare, items=1, evidence=kept) or "")

        assert with_evidence(["Artikel"]) == ["Artikel", "Beleg"]
        assert with_evidence(["Artikel", "beleg"]) == ["Artikel", "beleg"]

    def test_a_finish_table_reaches_the_diagnosis(self) -> None:
        rows = [{"Artikel": "a", "Befund": "stimmt"}]
        diagnosis = check_findings(Finish("ok", [], "", table=rows), {})
        assert diagnosis["table"] == rows
        assert "table" not in check_findings(Finish("ok", [], ""), {})


# --- the flow, through the hub's own store and routes ---------------------------------------------


class FakeLook:
    """Stands in for the second look: it confirms every row unless told otherwise."""

    def __init__(self, ok: bool = True, reason: str = "every row is supported") -> None:
        self.ok = ok
        self.reason = reason
        self.calls = 0

    async def check(
        self, question: str, table: Any, excerpts: Any, model: Any, reviewer: Any = None
    ) -> Any:
        self.calls += 1
        return SecondLookResult(self.ok, self.reason, "node-amd")


class FakeAgents:
    """Stands in for the agent service: a start writes a real task row and returns it."""

    def __init__(self, client: TestClient) -> None:
        self.client = client
        self.started: list[dict[str, Any]] = []
        self.refuse = False

    async def start(self, user: Any, prompt: str, model: Any, **kw: Any) -> Any:
        if self.refuse:
            raise RuntimeError("the queue refused")
        task_id = uuid.uuid4()
        async with self.client.app.state.sessionmaker() as db:
            db.add(
                AgentTask(
                    id=task_id,
                    user_id=USER,
                    prompt=prompt,
                    title="run",
                    via="gui",
                    status="running",
                    origin=kw.get("origin", "tasks"),
                    application=kw.get("application"),
                )
            )
            await db.commit()
        self.started.append({"id": task_id, "prompt": prompt, "model": model, **kw})
        return SimpleNamespace(id=task_id)


async def _set_finish(client: TestClient, task_id: uuid.UUID, table: Any, status: str) -> None:
    """The run ends with ``table``; each row names E1, a read the run kept."""
    rows = None
    if table is not None:
        rows = [{"Beleg": "E1", **r} if "Beleg" not in r else dict(r) for r in table]
    async with client.app.state.sessionmaker() as db:
        task = await db.get(AgentTask, task_id)
        task.status = status
        task.diagnosis = {"table": rows} if rows is not None else {}
        if rows is not None:
            db.add(
                AgentEvidence(
                    task_id=task_id,
                    user_id=USER,
                    ref="E1",
                    tag="web",
                    source={},
                    excerpt="a kept read",
                    bytes=12,
                )
            )
        await db.commit()


@pytest.fixture
def world(client: TestClient) -> Any:
    store = ProjectStore(client.app.state.sessionmaker)
    return store


@pytest.fixture(autouse=True)
def two_places(client: TestClient) -> Any:
    """No node is connected in the test hub: the batch gets two places, as two one-slot nodes."""
    asked: list[set[str]] = []

    async def places(targets: set[str]) -> int:
        asked.append(set(targets))
        return 2

    client.app.state.project_runs.places = places
    return asked


# --- how many runs at once: the slots of the rows' nodes ------------------------------------------


def _view(
    name: str,
    models: dict[str, bool],
    *,
    connected: bool = True,
    slots: int = 0,
    configured: dict[str, int] | None = None,
) -> Any:
    return SimpleNamespace(
        name=name,
        connected=connected,
        models=models,
        slots=lambda: slots or None,
        configured_parallel=configured or {},
    )


def test_a_node_with_nothing_loaded_counts_the_slots_its_config_names() -> None:
    policy = NodePolicy(applications={"dev": AppRow(nodes=("pc",))})
    idle = _view(
        "pc", {"Qwen3.5-9B-Q4_K_M": False, "gemma-4-26B": False}, configured={"gemma-4-26B": 2}
    )
    assert batch_places([idle], policy, {"dev"}) == 2
    # once a model is loaded, its real slots count (here it came up with one)
    loaded = _view("pc", {"Qwen3.5-9B-Q4_K_M": True}, slots=1, configured={"gemma-4-26B": 2})
    assert batch_places([loaded], policy, {"dev"}) == 1


def test_configured_slots_follow_the_nodes_own_rule() -> None:
    models = [{"name": "a"}, {"name": "b"}, {"name": "c"}]
    config = {"runner": {"n_parallel": 2, "model_n_parallel": {"a": 3, "c": 1}}}
    # per model first, else the node's default; one slot is the runner's default and not listed
    assert configured_slots(config, models) == {"a": 3, "b": 2}
    assert configured_slots({}, models) == {}
    assert configured_slots(None, models) == {}


def test_places_are_the_slots_of_the_connected_nodes_the_rows_name() -> None:
    policy = NodePolicy(
        applications={
            "dev": AppRow(nodes=("geo", "pc")),
            "research": AppRow(nodes=("geo", "laptop")),
        }
    )
    views = [
        _view("geo", {"Qwen3.5-9B-Q4_K_M": True}, slots=2),
        _view("pc", {"gemma-4-26B-A4B-it-UD-Q2_K_XL": False}),  # nothing loaded: one place
        _view("laptop", {"Qwen3.5-4B-Q4_K_M": False}, connected=False),
        _view("other", {"Qwen3.5-9B-Q4_K_M": True}, slots=4),  # in no row of these tasks
    ]
    assert batch_places(views, policy, {"dev"}) == 3
    assert batch_places(views, policy, {"research"}) == 2  # the laptop is not connected
    assert batch_places(views, policy, {"dev", "research"}) == 3  # a node counts once


def test_a_row_without_nodes_counts_every_chat_node_but_no_embedding_node() -> None:
    policy = NodePolicy(applications={"dev": AppRow(models=("x",))})
    views = [
        _view("geo", {"Qwen3.5-9B-Q4_K_M": True}, slots=2),
        _view("embed", {"bge-m3-q8_0": True}),
        _view("empty", {}),
    ]
    assert batch_places(views, policy, {"dev"}) == 2
    assert batch_places(views, NodePolicy(), set()) == 2


async def _project_with_tasks(store: ProjectStore, n: int) -> tuple[uuid.UUID, list[uuid.UUID]]:
    project = await store.create_project(USER, title=f"Runs {uuid.uuid4().hex[:10]}")
    ids = []
    for i in range(n):
        node = await store.create_node(
            USER, project.id, kind="task", title=f"Article {i}", body=FORM_BODY
        )
        ids.append(node.id)
    return project.id, ids


async def _node(client: TestClient, node_id: uuid.UUID) -> ProjectNode:
    async with client.app.state.sessionmaker() as db:
        return await db.scalar(select(ProjectNode).where(ProjectNode.id == node_id))


@pytest.mark.asyncio
async def test_independent_tasks_run_on_both_nodes_and_a_bad_result_stops_the_batch(
    client: TestClient, world: ProjectStore
) -> None:
    runs = client.app.state.project_runs
    fake = FakeAgents(client)
    runs._agents = fake
    runs.second_look = FakeLook()
    project_id, (first, second) = await _project_with_tasks(world, 2)

    batch = await runs.create_batch(USER, project_id, [first, second], max_runs=2)
    # two places, two runs; node and model are the gateway's choice from the row of the task's
    # application, so the batch names neither
    assert [s["model"] for s in fake.started] == [None, None]
    assert [s["application"] for s in fake.started] == ["dev", "dev"]
    assert batch.status == "active" and batch.runs == 2
    assert (await _node(client, first)).status == "in_progress"
    assert (await _node(client, second)).status == "in_progress"

    task1 = fake.started[0]["id"]
    await _set_finish(
        client, task1, [{"Artikel": "a", "Befund": "ok"}, {"Artikel": "b", "Befund": "ok"}], "done"
    )
    await runs.on_task_event(USER, task1, "done")
    assert (await _node(client, first)).status == "in_review"
    assert (await _node(client, second)).status == "in_progress"  # the other run goes on
    assert len(fake.started) == 2

    task2 = fake.started[1]["id"]
    await _set_finish(client, task2, [{"Artikel": "a"}, {"Artikel": "b"}], "done")
    await runs.on_task_event(USER, task2, "done")
    assert (await _node(client, second)).status == "blocked"
    stopped = (await runs.batches(USER, project_id))[0]
    assert stopped.status == "stopped" and "blocked" in (stopped.stop_reason or "")
    assert len(fake.started) == 2


@pytest.mark.asyncio
async def test_a_batch_runs_as_many_tasks_as_there_are_places_and_fills_a_freed_one(
    client: TestClient, world: ProjectStore, two_places: list[set[str]]
) -> None:
    runs = client.app.state.project_runs
    fake = FakeAgents(client)
    runs._agents = fake
    runs.second_look = FakeLook()
    project_id, ids = await _project_with_tasks(world, 3)

    await runs.create_batch(USER, project_id, ids, max_runs=5)
    assert len(fake.started) == 2  # two places: the third task waits
    assert two_places[-1] == {"dev"}  # the places asked for are those of the tasks' application
    assert (await _node(client, ids[2])).status == "todo"

    first = fake.started[0]["id"]
    await _set_finish(
        client, first, [{"Artikel": "a", "Befund": "ok"}, {"Artikel": "b", "Befund": "ok"}], "done"
    )
    await runs.on_task_event(USER, first, "done")
    assert len(fake.started) == 3  # the freed place takes the waiting task
    assert (await _node(client, ids[2])).status == "in_progress"


@pytest.mark.asyncio
async def test_an_end_heard_twice_at_once_is_judged_once(
    client: TestClient, world: ProjectStore
) -> None:
    runs = client.app.state.project_runs
    fake = FakeAgents(client)
    runs._agents = fake

    class SlowLook(FakeLook):
        async def check(self, *a: Any) -> Any:
            await asyncio.sleep(0.05)  # the second look takes a while: both ends arrive meanwhile
            return await super().check(*a)

    look = SlowLook()
    runs.second_look = look
    project_id, ids = await _project_with_tasks(world, 1)
    await runs.create_batch(USER, project_id, ids, max_runs=1)
    task = fake.started[0]["id"]
    await _set_finish(
        client, task, [{"Artikel": "a", "Befund": "ok"}, {"Artikel": "b", "Befund": "ok"}], "done"
    )
    # the run's own event and a dispatch that finds the run over, at the same time
    await asyncio.gather(runs.on_task_event(USER, task, "done"), runs.kick_all())
    assert look.calls == 1
    assert (await _node(client, ids[0])).status == "in_review"


@pytest.mark.asyncio
async def test_with_no_place_nothing_starts_and_the_batch_waits(
    client: TestClient, world: ProjectStore
) -> None:
    runs = client.app.state.project_runs
    fake = FakeAgents(client)
    runs._agents = fake

    async def none(targets: set[str]) -> int:
        return 0  # no node of the row is connected

    runs.places = none
    project_id, ids = await _project_with_tasks(world, 1)
    batch = await runs.create_batch(USER, project_id, ids, max_runs=2)
    assert fake.started == [] and batch.status == "active"  # a node that connects kicks it


@pytest.mark.asyncio
async def test_the_limit_of_runs_and_the_persons_stop_end_a_batch(
    client: TestClient, world: ProjectStore
) -> None:
    runs = client.app.state.project_runs
    fake = FakeAgents(client)
    runs._agents = fake
    runs.second_look = FakeLook()
    project_id, (a, b) = await _project_with_tasks(world, 2)
    await runs.create_batch(USER, project_id, [a, b], max_runs=1)
    assert len(fake.started) == 1  # the limit of one run: the second task does not start
    task = fake.started[0]["id"]
    await _set_finish(
        client, task, [{"Artikel": "a", "Befund": "ok"}, {"Artikel": "x", "Befund": "ok"}], "done"
    )
    await runs.on_task_event(USER, task, "done")
    assert len(fake.started) == 1
    done = (await runs.batches(USER, project_id))[0]
    assert done.status == "stopped" and "limit" in (done.stop_reason or "")

    project2, (c, d) = await _project_with_tasks(world, 2)
    batch2 = await runs.create_batch(USER, project2, [c, d], max_runs=5)
    await runs.stop_batch(USER, batch2.id)
    assert (await runs.batches(USER, project2))[0].status == "stopped"


@pytest.mark.asyncio
async def test_a_task_without_a_form_does_not_start(
    client: TestClient, world: ProjectStore
) -> None:
    runs = client.app.state.project_runs
    runs._agents = FakeAgents(client)
    project = await world.create_project(USER, title="No form")
    node = await world.create_node(USER, project.id, kind="task", title="Plain", body="just text")
    with pytest.raises(RunError):
        await runs.start_node(USER, node.id)
    assert (await _node(client, node.id)).status == "todo"


@pytest.mark.asyncio
async def test_a_chat_run_is_not_touched(client: TestClient, world: ProjectStore) -> None:
    runs = client.app.state.project_runs
    runs._agents = FakeAgents(client)
    await runs.on_task_event(USER, uuid.uuid4(), "done")  # a task that belongs to no node


@pytest.mark.asyncio
async def test_a_run_without_its_table_is_asked_once_more(
    client: TestClient, world: ProjectStore
) -> None:
    runs = client.app.state.project_runs
    fake = FakeAgents(client)
    runs._agents = fake
    runs.second_look = FakeLook()
    project_id, (a,) = await _project_with_tasks(world, 1)
    batch = await runs.create_batch(USER, project_id, [a], max_runs=1)

    first = fake.started[0]["id"]
    await _set_finish(client, first, None, "done")  # no table at all
    await runs.on_task_event(USER, first, "done")
    assert len(fake.started) == 2  # asked once more
    assert fake.started[1]["prompt"].startswith("[retry]")
    node = await _node(client, a)
    assert node.status == "in_progress" and node.task_id == fake.started[1]["id"]

    second = fake.started[1]["id"]
    await _set_finish(client, second, None, "done")  # still no table: the node is blocked
    await runs.on_task_event(USER, second, "done")
    assert (await _node(client, a)).status == "blocked"
    assert len(fake.started) == 2
    assert (await runs.batches(USER, project_id))[0].status == "stopped"
    assert batch.id
    # the project agent can say why (live 2026-11-10: "why is 2.1 blocked?" had no answer)
    from topcoat_hub.project_read_tools import run_read

    got = await run_read(
        ProjectStore(client.app.state.sessionmaker), USER, "project.node.get",
        {"node_id": str(a)}, zone="UTC",
    )  # fmt: skip
    change = got["last_status_change"]
    assert change["to"] == "blocked" and change["by"] == "system"
    assert change["reason"] == "the result has no table" and change["run"] == str(second)


@pytest.mark.asyncio
async def test_a_result_the_second_look_does_not_confirm_stays_out_of_review(
    client: TestClient, world: ProjectStore
) -> None:
    runs = client.app.state.project_runs
    fake = FakeAgents(client)
    runs._agents = fake
    runs.second_look = FakeLook(ok=False, reason="row 1 not supported by the cited reads")
    project_id, (a,) = await _project_with_tasks(world, 1)
    await runs.create_batch(USER, project_id, [a], max_runs=1)
    task = fake.started[0]["id"]
    await _set_finish(
        client, task, [{"Artikel": "a", "Befund": "ok"}, {"Artikel": "b", "Befund": "ok"}], "done"
    )
    await runs.on_task_event(USER, task, "done")
    assert (await _node(client, a)).status == "blocked"
    assert runs.second_look.calls == 1


@pytest.mark.asyncio
async def test_without_a_second_look_nothing_is_reviewed(
    client: TestClient, world: ProjectStore
) -> None:
    runs = client.app.state.project_runs
    fake = FakeAgents(client)
    runs._agents = fake
    runs.second_look = None
    project_id, (a,) = await _project_with_tasks(world, 1)
    await runs.create_batch(USER, project_id, [a], max_runs=1)
    task = fake.started[0]["id"]
    await _set_finish(
        client, task, [{"Artikel": "a", "Befund": "ok"}, {"Artikel": "b", "Befund": "ok"}], "done"
    )
    await runs.on_task_event(USER, task, "done")
    assert (await _node(client, a)).status == "blocked"


@pytest.mark.asyncio
async def test_a_task_waits_for_its_blocker_and_starts_when_the_blocker_is_in_review(
    client: TestClient, world: ProjectStore
) -> None:
    runs = client.app.state.project_runs
    fake = FakeAgents(client)
    runs._agents = fake
    runs.second_look = FakeLook()
    project_id, (first, second) = await _project_with_tasks(world, 2)
    await world.add_relation(USER, second, first, "blocked_by")

    await runs.create_batch(USER, project_id, [first, second], max_runs=2)
    assert len(fake.started) == 1  # the second task is blocked by the first: it waits
    assert (await _node(client, second)).status == "todo"

    task1 = fake.started[0]["id"]
    await _set_finish(
        client, task1, [{"Artikel": "a", "Befund": "ok"}, {"Artikel": "b", "Befund": "ok"}], "done"
    )
    await runs.on_task_event(USER, task1, "done")
    assert len(fake.started) == 2  # the blocker is in review: the second one starts now
    assert (await _node(client, second)).status == "in_progress"


@pytest.mark.asyncio
async def test_a_blocked_task_holds_back_only_its_dependants_and_the_batch_goes_on(
    client: TestClient, world: ProjectStore
) -> None:
    runs = client.app.state.project_runs
    fake = FakeAgents(client)
    runs._agents = fake
    runs.second_look = FakeLook()
    project_id, (a, b, c) = await _project_with_tasks(world, 3)
    await world.add_relation(USER, c, a, "blocked_by")  # c waits for a
    await runs.create_batch(USER, project_id, [a, b, c], max_runs=3)
    assert len(fake.started) == 2  # a and b run; c waits for a

    task_a = fake.started[0]["id"]
    await _set_finish(client, task_a, [{"Artikel": "x"}], "done")  # misses the form: blocked
    await runs.on_task_event(USER, task_a, "done")
    assert (await _node(client, a)).status == "blocked"
    assert (await _batch_status(client, project_id)) == "active"  # the batch goes on

    task_b = fake.started[1]["id"]
    await _set_finish(
        client, task_b, [{"Artikel": "a", "Befund": "ok"}, {"Artikel": "b", "Befund": "ok"}], "done"
    )
    await runs.on_task_event(USER, task_b, "done")
    assert (await _node(client, c)).status == "todo"  # still held back by the blocked a


@pytest.mark.asyncio
async def test_approving_a_blocked_task_again_puts_it_back_in_the_queue(
    client: TestClient, world: ProjectStore
) -> None:
    runs = client.app.state.project_runs
    fake = FakeAgents(client)
    runs._agents = fake
    runs.second_look = FakeLook()
    project_id, (a,) = await _project_with_tasks(world, 1)
    await runs.create_batch(USER, project_id, [a], max_runs=2)
    task_a = fake.started[0]["id"]
    await _set_finish(client, task_a, [{"Artikel": "x"}], "done")
    await runs.on_task_event(USER, task_a, "done")
    assert (await _node(client, a)).status == "blocked"

    await runs.create_batch(USER, project_id, [a], max_runs=2)  # approved again
    assert len(fake.started) == 2
    assert (await _node(client, a)).status == "in_progress"


async def _batch_status(client: TestClient, project_id: uuid.UUID) -> str:
    runs = client.app.state.project_runs
    return (await runs.batches(USER, project_id))[0].status


@pytest.mark.asyncio
async def test_the_result_of_a_run_is_filed_below_its_task(
    client: TestClient, world: ProjectStore
) -> None:
    """Live 2026-11-14: task 1.5 stood at "in review" and its result table was only in the agent
    run, which no page of the project shows."""
    runs = client.app.state.project_runs
    fake = FakeAgents(client)
    runs._agents = fake
    runs.second_look = FakeLook()
    project_id, (first,) = await _project_with_tasks(world, 1)
    await runs.create_batch(USER, project_id, [first], max_runs=1)
    task = fake.started[0]["id"]
    await _set_finish(
        client, task, [{"Artikel": "a", "Befund": "ok"}, {"Artikel": "b", "Befund": "x|y"}], "done"
    )
    await runs.on_task_event(USER, task, "done")
    async with client.app.state.sessionmaker() as db:
        reports = list(
            await db.scalars(
                select(ProjectNode).where(
                    ProjectNode.parent_id == first, ProjectNode.kind == "report"
                )
            )
        )
    assert len(reports) == 1 and reports[0].title.startswith("Ergebnis: ")
    body = reports[0].body
    assert "wartet auf Prüfung" in body and "| Artikel | Befund |" in body
    assert r"x\|y" in body  # a pipe in a value does not break the table
    assert str(task) in body
    await runs.on_task_event(USER, task, "done")  # heard twice: still one report
    async with client.app.state.sessionmaker() as db:
        again = await db.scalars(
            select(ProjectNode).where(ProjectNode.parent_id == first, ProjectNode.kind == "report")
        )
        assert len(list(again)) == 1


def test_a_run_that_left_nothing_files_nothing() -> None:
    from topcoat_hub.project_runs import result_document

    assert result_document({}, "blocked", "no table", "t") == ""
    text = result_document({"summary": "Kurz.", "next": "1.6 starten"}, "blocked", "", "t")
    assert text.startswith("**Stand:** blockiert") and "Nächster Schritt" in text


@pytest.mark.asyncio
async def test_a_task_gets_the_results_of_the_tasks_it_depends_on(
    client: TestClient, world: ProjectStore
) -> None:
    """Issue 2.3 (live 2026-11-18): a task split across applications (Verwaltung sees the
    machines, Research reads the web) left the task that needs both to look it all up again."""
    runs = client.app.state.project_runs
    runs._agents = FakeAgents(client)
    project = await world.create_project(USER, title=f"Deps {uuid.uuid4().hex[:8]}")
    admin_body = "Zielanwendung: admin\nErgebnisform: Frage | Antwort\n- [ ] Modelle auf node-amd?"
    done = await world.create_node(
        USER, project.id, kind="task", title="Modelle der Rechner", body=admin_body
    )
    await world.create_node(
        USER, project.id, kind="report", title="Ergebnis", body="node-amd: gpt-oss-20b",
        report_kind="document",
        parent_id=done.id,
    )  # fmt: skip
    done = await world.update_node(USER, done.id, revision=done.revision, status="done")
    open_one = await world.create_node(
        USER, project.id, kind="task", title="Offen", body=admin_body
    )
    later = await world.create_node(
        USER, project.id, kind="task", title="Planen", body=FORM_BODY,
        depends_on=[done.id, open_one.id],
    )  # fmt: skip
    await runs.start_node(USER, later.id)  # a batch would wait; a single start does not
    prompt = runs._agents.started[-1]["prompt"]
    assert "Results of the tasks this one depends on" in prompt
    assert "Modelle der Rechner\nnode-amd: gpt-oss-20b" in prompt and "Offen" not in prompt


def test_blocker_results_keep_only_finished_tasks_with_a_result_and_cut_long_ones() -> None:
    from topcoat_hub.project_runs import blocker_results

    def task(n: str, status: str) -> Any:
        return SimpleNamespace(id=uuid.uuid4(), number=n, title=f"T{n}", status=status)

    def result(parent: Any, body: str) -> Any:
        return SimpleNamespace(
            parent_id=parent.id, kind="report", trashed_at=None, iteration=1, created_at=None,
            body=body,
        )  # fmt: skip

    a, b, c = task("1", "done"), task("2", "todo"), task("3", "in_review")
    text = blocker_results([(a, [result(a, "x" * 5000)]), (b, [result(b, "no")]), (c, [])])
    assert text.endswith("x" * 3000) and "x" * 3001 not in text
    assert "T2" not in text and "T3" not in text
    assert blocker_results([]) == ""
