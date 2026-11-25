"""Large tasks (vorhaben.py, D109, D110, issue #32): a decomposition is filed as child tasks under
the grant or proposed as a change set; the coordinator starts what is ready, one writer at a time
and writers only under the workshop scope; a sub-task that failed twice the same way gets its
branch replanned once, then goes to the person; the scope is the admin's click alone."""

import json
import uuid
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi.testclient import TestClient

from tests.conftest import AUTH, TEST_DB_URL
from tests.users import make_user
from topcoat_hub import decompose as dc
from topcoat_hub import vorhaben as vh
from topcoat_hub.issue_fixes import LIVE_REPO
from topcoat_hub.models import DEV_ADMIN_ID as USER
from topcoat_hub.project_base import Invalid
from topcoat_hub.project_changesets import ChangeSetStore
from topcoat_hub.project_events import PERSON
from topcoat_hub.project_mandates import WORKSHOP
from topcoat_hub.project_store import ProjectStore

CODE = {
    "hub/src/topcoat_hub/project_store.py": (
        "class ProjectStore:\n    def add(self):\n        pass\n"
    ),
    "hub/src/topcoat_hub/api/project.py": "def router():\n    pass\n",
}


async def read(paths: list[str]) -> dict[str, str | None]:
    return {p: CODE.get(p) for p in paths}


def sub(sid: str, **kw: Any) -> dict[str, Any]:
    base = {
        "id": sid,
        "title": f"Teil {sid}",
        "goal": f"Ziel {sid}",
        "layer": "store",
        "files": ["hub/src/topcoat_hub/project_store.py"],
        "symbols": ["ProjectStore.add"],
        "new_files": [],
        "accept": [f"test:hub/tests/test_{sid}.py"],
        "depends_on": [],
    }
    return {**base, **kw}


ANSWER = json.dumps({
    "summary": "Teilaufgaben",
    "subtasks": [
        sub("s1"),
        sub("s2", layer="api", files=["hub/src/topcoat_hub/api/project.py"], symbols=["router"],
            depends_on=["s1"]),
        sub("s3", layer="api", files=["hub/src/topcoat_hub/api/project.py"], symbols=["router"],
            depends_on=["s2"]),
        sub("r1", layer="research", files=[], symbols=[], accept=["person: gelesen"]),
    ],
})  # fmt: skip


class Pipeline:
    def __init__(self) -> None:
        self.started: list[uuid.UUID] = []
        self.by_node: dict[str, list[Any]] = {}

    async def start(self, user_id: uuid.UUID, node_id: uuid.UUID) -> None:
        self.started.append(node_id)

    async def fixes(self, user_id: uuid.UUID, node_ids: Any) -> dict[str, list[Any]]:
        return {str(n): self.by_node.get(str(n), []) for n in node_ids}


def failed_fix() -> Any:
    output = "E   AssertionError: no rows\nFAILED hub/tests/test_s2.py::test_x\n1 failed"
    return SimpleNamespace(
        id=uuid.uuid4(),
        state="failed",
        summary="",
        error="the tests ended failed",
        tests={"output": output},
    )


async def _setup(client: TestClient, grant: str | None = "free") -> tuple[Any, Any, Any, Pipeline]:
    sm = client.app.state.sessionmaker
    store = ProjectStore(sm, PERSON)
    project = await store.create_project(USER, title=f"Vorhaben {uuid.uuid4().hex[:6]}")
    if grant:
        project = await store.set_scope(USER, project.id, revision=project.revision,
                                        scope={"project": {"decompose": grant}})  # fmt: skip
    large = await store.create_node(USER, project.id, kind="task", title="Teilaufgaben anzeigen",
                                    body="Zielanwendung: dev\nDas Feature 3.2.1.2")  # fmt: skip
    pipe = Pipeline()
    coord = vh.Coordinator(store, ChangeSetStore(sm), pipe, repo=LIVE_REPO, contract="Dev")
    return store, large, coord, pipe


def asker(*answers: str) -> Any:
    calls: list[dict[str, Any]] = []

    async def ask(name: str, inputs: dict[str, Any]) -> tuple[str, str]:
        calls.append(inputs)
        return answers[min(len(calls), len(answers)) - 1], "coder@node-amd"

    ask.calls = calls  # type: ignore[attr-defined]
    return ask


def test_branch_scope_text_and_the_spec_in_the_body() -> None:
    assert vh.branch_of("3.2.1") == "topcoat/v-3-2-1"
    text = vh.scope_text("3.2.1", "Teilaufgaben", LIVE_REPO)
    assert "topcoat/v-3-2-1" in text and "Push" in text and "14 Tage" in text
    s = dc.Subtask(
        "s2", "Teil", "Ein Satz.", "api", ["hub/a.py"], ["f"], [], ["test:hub/tests/t.py"], ["s1"]
    )
    body = vh.body_of(s, "3.2")
    assert "Art: issue" in body and "Vorhaben: 3.2" in body and vh.large_of(body) == "3.2"
    back = vh.spec_of("Teil", body)
    assert back == s
    assert vh.spec_of("x", "no spec here") is None
    research = vh.body_of(dc.Subtask("r", "R", "g", "research"), "1")
    assert "Zielanwendung: research" in research and "Art: issue" not in research


@pytest.mark.parametrize(
    ("status", "deps_done", "fix", "state"),
    [
        ("done", True, None, "done"),
        ("blocked", True, "failed", "blocked"),
        ("todo", False, None, "waiting"),
        ("todo", True, None, "waiting"),
        ("in_progress", True, None, "running"),
        ("todo", True, "testing", "running"),
        ("todo", True, "failed", "failed"),
    ],
)
def test_the_state_of_a_sub_task(status: str, deps_done: bool, fix: str | None, state: str) -> None:
    assert vh.state_of(status, deps_done, fix) == state


@pytest.mark.asyncio
async def test_under_the_free_grant_the_sub_tasks_are_filed_as_a_dag(client: TestClient) -> None:
    _, large, coord, _ = await _setup(client)
    out = await coord.decompose(USER, large.id, asker(ANSWER), read)
    assert len(out["filed"]) == 4 and out["change_set"] is None
    shown = await coord.view(USER, large.id)
    by_id = {s["id"]: s for s in shown["subtasks"]}
    assert by_id["s2"]["depends_on"] == ["s1"] and by_id["s3"]["depends_on"] == ["s2"]
    assert by_id["s1"]["state"] == "waiting" and by_id["s1"]["layer"] == "store"
    assert shown["large_task"]["branch"] == vh.branch_of(large.number)
    assert shown["large_task"]["state"] == "running" and shown["rollout"] == {"ready": False}
    with pytest.raises(vh.DecomposeError, match="sub-tasks already"):
        await coord.decompose(USER, large.id, asker(ANSWER), read)


@pytest.mark.asyncio
async def test_without_the_grant_the_decomposition_is_a_change_set(client: TestClient) -> None:
    _, large, coord, _ = await _setup(client, grant=None)
    out = await coord.decompose(USER, large.id, asker(ANSWER), read)
    assert out["filed"] == [] and out["change_set"]
    assert (await coord.view(USER, large.id))["subtasks"] == []


@pytest.mark.asyncio
async def test_a_refused_decomposition_files_nothing(client: TestClient) -> None:
    _, large, coord, _ = await _setup(client)
    with pytest.raises(vh.DecomposeError) as err:
        await coord.decompose(USER, large.id, asker("{}"), read)
    assert err.value.problems == ["the answer has no sub-tasks"]
    assert (await coord.view(USER, large.id))["subtasks"] == []


@pytest.mark.asyncio
async def test_writers_start_one_at_a_time_and_only_under_the_scope(client: TestClient) -> None:
    store, large, coord, _ = await _setup(client)
    await coord.decompose(USER, large.id, asker(ANSWER), read)
    tick = await coord.tick(USER, large.id)
    # no workshop scope: the reading sub-task starts, the writer does not
    assert tick.started == ["r1"] and tick.stopped.startswith("no active workshop scope")
    branch = vh.branch_of(large.number)
    await store.create_mandate(
        USER, large.project_id, repo=LIVE_REPO, branch=branch, actions=[WORKSHOP], push=False
    )
    items = {i.spec.id: i for i in await coord.items(USER, large)}
    await store.update_node(USER, items["r1"].node.id, revision=items["r1"].node.revision,
                            status="in_progress")  # fmt: skip
    tick = await coord.tick(USER, large.id)
    assert tick.started == ["s1"] and tick.stopped == ""
    items = {i.spec.id: i for i in await coord.items(USER, large)}
    await store.update_node(USER, items["s1"].node.id, revision=items["s1"].node.revision,
                            status="in_progress")  # fmt: skip
    assert (await coord.tick(USER, large.id)).started == []  # s2 waits for s1
    for sid in ("s1", "r1"):
        node = await store.get_node(USER, items[sid].node.id)
        await store.update_node(USER, node.id, revision=node.revision, status="done")
    assert (await coord.tick(USER, large.id)).started == ["s2"]


@pytest.mark.asyncio
async def test_a_failure_twice_replans_its_branch_once_then_blocks(client: TestClient) -> None:
    store, large, coord, pipe = await _setup(client)
    await coord.decompose(USER, large.id, asker(ANSWER), read)
    items = {i.spec.id: i for i in await coord.items(USER, large)}
    node = await store.get_node(USER, items["s1"].node.id)
    await store.update_node(USER, node.id, revision=node.revision, status="done")
    pipe.by_node[str(items["s2"].node.id)] = [failed_fix(), failed_fix()]
    shown = {s["id"]: s for s in (await coord.view(USER, large.id))["subtasks"]}
    assert shown["s2"]["state"] == "failed" and shown["s2"]["next"] == "wird neu geplant"
    new = json.dumps({"summary": "neu", "subtasks": [
        sub("s4", layer="api", files=["hub/src/topcoat_hub/api/project.py"], symbols=["router"],
            depends_on=["s1"]),
    ]})  # fmt: skip
    ask = asker(new)
    tick = await coord.tick(USER, large.id, ask, read)
    assert tick.replanned == ["s2"]
    assert "REPLAN ONE BRANCH" in ask.calls[0]["feedback"] and "s2, s3" in ask.calls[0]["feedback"]
    after = {i.spec.id: i for i in await coord.items(USER, large)}
    assert set(after) == {"s1", "s4", "r1"}  # s2 and s3 are closed, not deleted
    assert (await store.get_node(USER, items["s3"].node.id)).status == "dropped"
    assert [str(d) for d in after["s4"].node.depends_on] == [str(items["s1"].node.id)]
    # the replanned sub-task fails twice as well: it goes to the person
    pipe.by_node[str(after["s4"].node.id)] = [failed_fix(), failed_fix()]
    tick = await coord.tick(USER, large.id, asker(new), read)
    assert tick.blocked == ["s4"]
    assert (await store.get_node(USER, after["s4"].node.id)).status == "blocked"
    # only now the person hears of it: one inbox item for the sub-task
    from sqlalchemy import select

    from topcoat_hub.models import InboxItem

    async with client.app.state.sessionmaker() as db:
        rows = list(await db.scalars(select(InboxItem).where(
            InboxItem.user_id == USER, InboxItem.source_type == "large_task",
            InboxItem.source_ref == str(after["s4"].node.id),
        )))  # fmt: skip
    assert len(rows) == 1 and rows[0].kind == "fix_blocked"
    assert "Teilaufgabe blockiert" in rows[0].title


@pytest.mark.asyncio
async def test_a_replan_may_not_reuse_an_id(client: TestClient) -> None:
    _, large, coord, pipe = await _setup(client)
    await coord.decompose(USER, large.id, asker(ANSWER), read)
    items = {i.spec.id: i for i in await coord.items(USER, large)}
    pipe.by_node[str(items["s1"].node.id)] = [failed_fix(), failed_fix()]
    again = json.dumps({"summary": "x", "subtasks": [sub("s2")]})
    tick = await coord.tick(USER, large.id, asker(again), read)
    assert tick.blocked == ["s1"] and tick.replanned == []


@pytest.mark.asyncio
async def test_a_workshop_scope_never_pushes_and_covers_nothing_else(client: TestClient) -> None:
    store = ProjectStore(client.app.state.sessionmaker, PERSON)
    project = await store.create_project(USER, title=f"Scope {uuid.uuid4().hex[:6]}")
    for actions, push in (([WORKSHOP], True), ([WORKSHOP, "fix"], False)):
        with pytest.raises(Invalid):
            await store.create_mandate(USER, project.id, repo=LIVE_REPO, branch="topcoat/v-1",
                                       actions=actions, push=push)  # fmt: skip


@pytest.mark.asyncio
async def test_the_card_makes_the_scope_and_the_view_shows_it(client: TestClient) -> None:
    _, large, _, _ = await _setup(client)
    _, admin_key = make_user(TEST_DB_URL, "vorhaben key", role="admin")
    url = f"/api/project/nodes/{large.id}/workshop-scope"
    assert client.post(url, headers=admin_key).status_code in (401, 403)
    made = client.post(url, headers=AUTH)
    assert made.status_code == 201, made.text
    m = made.json()["mandate"]
    assert m["actions"] == [WORKSHOP] and m["push"] is False and m["repo"] == LIVE_REPO
    assert m["branch"] == vh.branch_of(large.number) and "14 Tage" in m["text"]
    shown = client.get(f"/api/project/nodes/{large.id}/large-task", headers=AUTH)
    assert shown.status_code == 200, shown.text
    body = shown.json()
    assert (
        body["large_task"]["scope"]["id"] == m["id"] and body["large_task"]["state"] == "planning"
    )
    assert body["subtasks"] == [] and body["rollout"] == {"ready": False}
