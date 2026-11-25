"""The Akte of a project (PPH-04, 04a): events, the hash chain, versions of titles and bodies.

Needs the test database (see ``conftest.py``). What is tested: every change the store makes writes
a line in the same transaction (and a test lists the store's public methods so a new one cannot
slip by); the chain of a project verifies and notices a changed or removed line, but not a purged
payload; titles and bodies come back from snapshots and diffs for every revision; the timeline and
the history are scoped by person; parallel writers do not fork the chain.
"""

import asyncio
import inspect
import random
import uuid
from itertools import pairwise

import pytest
from sqlalchemy import create_engine, text

from tests.conftest import AUTH, TEST_DB_URL
from tests.users import make_user
from topcoat_hub import project_store
from topcoat_hub.models import DEV_ADMIN_ID
from topcoat_hub.project_events import KINDS, Actor, EventLog, payload_hash, verify
from topcoat_hub.project_store import Invalid, NotFound, ProjectStore
from topcoat_hub.project_versions import SNAPSHOT_EVERY, apply_diff, make_diff


@pytest.fixture
def store(client) -> ProjectStore:  # type: ignore[no-untyped-def]
    return ProjectStore(client.app.state.sessionmaker)


async def kinds_of(store: ProjectStore, project_id: uuid.UUID) -> list[str]:
    return [e.kind for e in await store.timeline(DEV_ADMIN_ID, project_id, limit=200)]


# --- one writer ---------------------------------------------------------------------------------


def test_every_public_method_of_the_store_is_classified() -> None:
    """A method that is neither a known reader nor a known writer could change data without a line
    in the Akte: adding one means deciding which it is (and testing that it records)."""
    public = {
        name
        for name, member in inspect.getmembers(ProjectStore)
        if inspect.isfunction(member) and not name.startswith("_")
    }
    classified = (
        set(project_store.MUTATORS) | set(project_store.READS) | set(project_store.EVENTLESS)
    )
    assert public == classified
    lists = [set(project_store.MUTATORS), set(project_store.READS), set(project_store.EVENTLESS)]
    assert sum(len(x) for x in lists) == len(classified)  # no method is in two lists


async def test_every_change_writes_a_line(store: ProjectStore) -> None:
    """One scenario that calls every mutator of the store; each must leave a line."""
    project = await store.create_project(DEV_ADMIN_ID, title="Akte", description="d")
    seen = {"create_project": await kinds_of(store, project.id)}
    count = len(seen["create_project"])
    assert seen["create_project"] == ["project.created"]

    async def step(name: str, call) -> None:  # type: ignore[no-untyped-def]
        nonlocal count
        await call
        kinds = await kinds_of(store, project.id)
        assert len(kinds) > count, f"{name} wrote no line"
        count = len(kinds)

    p = await store.get_project(DEV_ADMIN_ID, project.id)
    await step(
        "update_project",
        store.update_project(DEV_ADMIN_ID, p.id, revision=p.revision, description="e"),
    )
    p = await store.get_project(DEV_ADMIN_ID, project.id)
    await step(
        "set_scope",
        store.set_scope(
            DEV_ADMIN_ID, p.id, revision=p.revision, scope={"research": {"request_report": "free"}}
        ),
    )
    topic = await store.create_node(DEV_ADMIN_ID, p.id, kind="topic", title="T")
    task = await store.create_node(DEV_ADMIN_ID, p.id, kind="task", title="Do", parent_id=topic.id)
    await step(
        "create_node",
        store.create_node(DEV_ADMIN_ID, p.id, kind="report", title="Q?", report_kind="question"),
    )
    task = await store.get_node(DEV_ADMIN_ID, task.id)
    await step(
        "update_node",
        store.update_node(DEV_ADMIN_ID, task.id, revision=task.revision, status="in_progress"),
    )
    task = await store.get_node(DEV_ADMIN_ID, task.id)
    await step(
        "move_node", store.move_node(DEV_ADMIN_ID, task.id, revision=task.revision, parent_id=None)
    )
    question = next(n for n in await store.tree(DEV_ADMIN_ID, p.id) if n.report_kind == "question")
    await step("answer_question", store.answer_question(DEV_ADMIN_ID, question.id, "yes"))
    p = await store.get_project(DEV_ADMIN_ID, project.id)


async def test_a_change_that_fails_leaves_no_line(store: ProjectStore) -> None:
    project = await store.create_project(DEV_ADMIN_ID, title="Fails")
    before = await kinds_of(store, project.id)
    with pytest.raises(Invalid):
        await store.create_node(DEV_ADMIN_ID, project.id, kind="task", title="   ")
    with pytest.raises(Invalid):
        await store.update_project(DEV_ADMIN_ID, project.id, revision=1, status="nonsense")
    other = await store.create_project(DEV_ADMIN_ID, title="Other")
    with pytest.raises(Invalid):
        await store.update_project(DEV_ADMIN_ID, other.id, revision=1, title="Fails")
    assert await kinds_of(store, project.id) == before
    assert await kinds_of(store, other.id) == ["project.created"]


async def test_a_duplicate_title_writes_no_line_and_no_project(store: ProjectStore) -> None:
    await store.create_project(DEV_ADMIN_ID, title="Twice")
    with pytest.raises(Invalid):
        await store.create_project(DEV_ADMIN_ID, title="Twice")
    engine = create_engine(TEST_DB_URL)
    with engine.connect() as conn:
        lines = conn.scalar(
            text("SELECT count(*) FROM project_events WHERE project_title = 'Twice'")
        )
    assert lines == 1


# --- what a line says ---------------------------------------------------------------------------


async def test_a_line_says_who_what_and_to_which_node(store: ProjectStore) -> None:
    project = await store.create_project(DEV_ADMIN_ID, title="Says")
    task_id = uuid.uuid4()
    agent = store.with_actor(Actor("agent", agent_task_id=task_id))
    node = await agent.create_node(DEV_ADMIN_ID, project.id, kind="task", title="Write")
    events = await store.timeline(DEV_ADMIN_ID, project.id)
    created = events[-1]
    assert created.kind == "node.created"
    assert (created.actor_kind, created.agent_task_id) == ("agent", task_id)
    assert created.node_id == node.id
    assert created.node_ref == "Says 1" and created.node_title == "Write"
    assert events[0].actor_kind == "system"  # the store's default is not a person


async def test_status_changes_are_told_apart(store: ProjectStore) -> None:
    project = await store.create_project(DEV_ADMIN_ID, title="Status")
    node = await store.create_node(DEV_ADMIN_ID, project.id, kind="task", title="A")
    for status in ("in_progress", "done", "todo"):
        node = await store.update_node(DEV_ADMIN_ID, node.id, revision=node.revision, status=status)
    kinds = await kinds_of(store, project.id)
    assert kinds[-3:] == ["node.status_changed", "node.completed", "node.reopened"]


async def test_one_update_of_several_fields_is_one_group(store: ProjectStore) -> None:
    project = await store.create_project(DEV_ADMIN_ID, title="Group")
    node = await store.create_node(DEV_ADMIN_ID, project.id, kind="task", title="A", body="x\n")
    await store.update_node(
        DEV_ADMIN_ID, node.id, revision=node.revision, title="B", body="y\n", status="done"
    )
    events = [e for e in await store.timeline(DEV_ADMIN_ID, project.id) if e.seq > 2]
    assert {e.kind for e in events} == {"node.completed", "node.updated", "body.changed"}
    assert len({e.group_id for e in events}) == 1 and events[0].group_id is not None


async def test_archiving_a_project_is_its_own_kind(store: ProjectStore) -> None:
    project = await store.create_project(DEV_ADMIN_ID, title="Old")
    await store.update_project(DEV_ADMIN_ID, project.id, revision=1, status="dropped")
    assert (await kinds_of(store, project.id))[-1] == "project.archived"


def test_every_kind_has_both_labels() -> None:
    for kind, labels in KINDS.items():
        assert labels.get("en") and labels.get("de"), kind
    assert "baseline" in KINDS and "changeset.applied" in KINDS and "mandate.used" in KINDS


async def test_an_unknown_kind_is_refused(store: ProjectStore) -> None:
    project = await store.create_project(DEV_ADMIN_ID, title="Kinds")
    async with store._sessionmaker() as db:
        with pytest.raises(ValueError):
            await EventLog.record(db, DEV_ADMIN_ID, project, "made.up")


# --- the chain ----------------------------------------------------------------------------------


async def make_history(store: ProjectStore, title: str) -> uuid.UUID:
    project = await store.create_project(DEV_ADMIN_ID, title=title)
    node = await store.create_node(DEV_ADMIN_ID, project.id, kind="task", title="A", body="one\n")
    node = await store.update_node(DEV_ADMIN_ID, node.id, revision=node.revision, body="two\n")
    await store.update_node(DEV_ADMIN_ID, node.id, revision=node.revision, status="done")
    return project.id


async def test_the_chain_verifies(store: ProjectStore) -> None:
    pid = await make_history(store, "Chain")
    assert await store.verify_chain(DEV_ADMIN_ID, pid) == []
    events = await store.timeline(DEV_ADMIN_ID, pid, limit=200)
    assert [e.seq for e in events] == list(range(1, len(events) + 1))
    assert events[0].prev_hash == "0" * 64
    assert all(a.hash == b.prev_hash for a, b in pairwise(events))


async def test_a_changed_line_is_noticed(store: ProjectStore) -> None:
    pid = await make_history(store, "Tamper")
    engine = create_engine(TEST_DB_URL)
    with engine.begin() as conn:
        conn.execute(
            text("UPDATE project_events SET node_title = 'Z' WHERE project_id = :p AND seq = 3"),
            {"p": pid},
        )
    problems = await store.verify_chain(DEV_ADMIN_ID, pid)
    assert any("seq 3" in p and "changed" in p for p in problems)


async def test_a_changed_payload_is_noticed(store: ProjectStore) -> None:
    pid = await make_history(store, "Payload")
    engine = create_engine(TEST_DB_URL)
    with engine.begin() as conn:
        conn.execute(
            text(
                'UPDATE project_events SET payload = CAST(\'{"title": "other"}\' AS jsonb)'
                " WHERE project_id = :p AND seq = 1"
            ),
            {"p": pid},
        )
    assert any("payload" in p for p in await store.verify_chain(DEV_ADMIN_ID, pid))


async def test_a_removed_line_is_noticed(store: ProjectStore) -> None:
    pid = await make_history(store, "Removed")
    engine = create_engine(TEST_DB_URL)
    with engine.begin() as conn:
        conn.execute(
            text("DELETE FROM project_events WHERE project_id = :p AND seq = 2"), {"p": pid}
        )
    assert any("chain is broken" in p for p in await store.verify_chain(DEV_ADMIN_ID, pid))


async def test_a_purged_payload_keeps_the_chain(store: ProjectStore) -> None:
    """Purge keeps the timeline (D4-3): payload and reason go, their hash stays."""
    pid = await make_history(store, "Purged")
    engine = create_engine(TEST_DB_URL)
    with engine.begin() as conn:
        conn.execute(
            text("UPDATE project_events SET payload = NULL, reason = '' WHERE project_id = :p"),
            {"p": pid},
        )
    assert await store.verify_chain(DEV_ADMIN_ID, pid) == []


async def test_the_lines_survive_the_node(store: ProjectStore) -> None:
    pid = await make_history(store, "Gone")
    engine = create_engine(TEST_DB_URL)
    with engine.begin() as conn:
        conn.execute(text("DELETE FROM project_nodes WHERE project_id = :p"), {"p": pid})
    events = await store.timeline(DEV_ADMIN_ID, pid, limit=200)
    assert any(e.node_ref == "Gone 1" and e.node_title == "A" for e in events)
    assert await store.verify_chain(DEV_ADMIN_ID, pid) == []


def test_the_payload_hash_covers_payload_and_reason() -> None:
    assert payload_hash({"a": 1}, "") == payload_hash({"a": 1}, "")
    assert payload_hash({"a": 1}, "") != payload_hash({"a": 2}, "")
    assert payload_hash({"a": 1}, "") != payload_hash({"a": 1}, "because")
    assert verify([]) == []


async def test_parallel_writers_do_not_fork_the_chain(store: ProjectStore) -> None:
    project = await store.create_project(DEV_ADMIN_ID, title="Parallel")
    await asyncio.gather(
        *[
            store.create_node(DEV_ADMIN_ID, project.id, kind="task", title=f"N{i}")
            for i in range(12)
        ]
    )
    events = await store.timeline(DEV_ADMIN_ID, project.id, limit=200)
    assert [e.seq for e in events] == list(range(1, 14))
    assert await store.verify_chain(DEV_ADMIN_ID, project.id) == []


# --- versions -----------------------------------------------------------------------------------


def test_a_diff_turns_the_old_text_into_the_new_one() -> None:
    rng = random.Random(3)  # noqa: S311 - test data
    words = ["alpha\n", "beta\n", "gamma\n", "delta\n", "\n", "eps\n", "no newline"]
    for _ in range(200):
        old = "".join(rng.choice(words) for _ in range(rng.randint(0, 12)))
        new = "".join(rng.choice(words) for _ in range(rng.randint(0, 12)))
        assert apply_diff(old, make_diff(old, new)) == new


async def test_every_revision_comes_back(store: ProjectStore) -> None:
    """Random edits: the body at every revision is what it was, over several keyframes."""
    rng = random.Random(11)  # noqa: S311 - test data
    project = await store.create_project(DEV_ADMIN_ID, title="Versions")
    node = await store.create_node(DEV_ADMIN_ID, project.id, kind="topic", title="T0", body="")
    expected = {node.revision: ("T0", "")}
    title, body = "T0", ""
    for i in range(SNAPSHOT_EVERY * 2 + 7):
        lines = body.splitlines(keepends=True)
        action = rng.choice(["insert", "delete", "replace", "append"])
        position = rng.randint(0, len(lines))
        if action == "insert" or not lines:
            lines.insert(position, f"line {i}\n")
        elif action == "delete":
            lines.pop(min(position, len(lines) - 1))
        elif action == "replace":
            lines[min(position, len(lines) - 1)] = f"changed {i}\n"
        else:
            lines.append(f"tail {i}\n")
        new_body = "".join(lines)
        change: dict[str, str] = {"body": new_body}
        if i % 9 == 0:
            title = f"T{i}"
            change["title"] = title
        if new_body == body and "title" not in change:
            change["body"] = new_body + "x\n"
            new_body += "x\n"
        node = await store.update_node(DEV_ADMIN_ID, node.id, revision=node.revision, **change)
        body = new_body
        expected[node.revision] = (title, body)
    for rev, (want_title, want_body) in expected.items():
        state = await store.node_at(DEV_ADMIN_ID, node.id, rev)
        assert (state["title"], state["body"]) == (want_title, want_body), rev
    history = await store.node_history(DEV_ADMIN_ID, node.id)
    kinds = [v.kind for v, _ in history]
    assert kinds[0] == "snapshot"
    assert [i for i, k in enumerate(kinds) if k == "snapshot"][:3] == [
        0,
        SNAPSHOT_EVERY,
        SNAPSHOT_EVERY * 2,
    ]
    assert all(at is not None for _, at in history)


async def test_a_revision_between_two_versions_is_the_earlier_text(store: ProjectStore) -> None:
    project = await store.create_project(DEV_ADMIN_ID, title="Between")
    node = await store.create_node(DEV_ADMIN_ID, project.id, kind="task", title="A", body="v1\n")
    first = node.revision
    node = await store.update_node(DEV_ADMIN_ID, node.id, revision=node.revision, status="done")
    assert (await store.node_at(DEV_ADMIN_ID, node.id, node.revision))["body"] == "v1\n"
    node = await store.update_node(DEV_ADMIN_ID, node.id, revision=node.revision, body="v2\n")
    assert (await store.node_at(DEV_ADMIN_ID, node.id, first + 1))["body"] == "v1\n"
    assert (await store.node_at(DEV_ADMIN_ID, node.id, node.revision))["body"] == "v2\n"
    with pytest.raises(NotFound):
        await store.node_at(DEV_ADMIN_ID, node.id, 0)


async def test_a_diff_between_revisions(store: ProjectStore) -> None:
    project = await store.create_project(DEV_ADMIN_ID, title="Diff")
    node = await store.create_node(DEV_ADMIN_ID, project.id, kind="task", title="A", body="a\nb\n")
    first = node.revision
    node = await store.update_node(DEV_ADMIN_ID, node.id, revision=node.revision, body="a\nc\n")
    diff = await store.node_diff(DEV_ADMIN_ID, node.id, first, node.revision)
    changed = [d for d in diff["body"] if d["tag"] != "equal"]
    assert changed == [{"tag": "replace", "old": ["b\n"], "new": ["c\n"]}]


# --- scoping ------------------------------------------------------------------------------------


async def test_another_person_sees_no_line(store: ProjectStore) -> None:
    pid = await make_history(store, "Mine")
    other, _ = make_user(TEST_DB_URL, "akte-other")
    with pytest.raises(NotFound):
        await store.timeline(other, pid)
    with pytest.raises(NotFound):
        await store.verify_chain(other, pid)
    node = (await store.tree(DEV_ADMIN_ID, pid))[0]
    for call in (store.node_history(other, node.id), store.node_at(other, node.id, 1)):
        with pytest.raises(NotFound):
            await call


# --- the routes ---------------------------------------------------------------------------------


def test_the_timeline_route_pages_and_filters(client) -> None:  # type: ignore[no-untyped-def]
    pid = client.post("/api/project/projects", headers=AUTH, json={"title": "Routes"}).json()["id"]
    for i in range(5):
        client.post(
            f"/api/project/projects/{pid}/nodes",
            headers=AUTH,
            json={"kind": "task", "title": f"N{i}"},
        )
    page = client.get(f"/api/project/projects/{pid}/timeline?limit=3", headers=AUTH).json()
    assert [e["seq"] for e in page["events"]] == [1, 2, 3] and page["next_after"] == 3
    rest = client.get(f"/api/project/projects/{pid}/timeline?after=3", headers=AUTH).json()
    assert [e["seq"] for e in rest["events"]] == [4, 5, 6] and rest["next_after"] is None
    only = client.get(
        f"/api/project/projects/{pid}/timeline?kinds=project.created", headers=AUTH
    ).json()
    assert [e["kind"] for e in only["events"]] == ["project.created"]
    assert only["events"][0]["actor"]["kind"] == "person"
    assert (
        client.get(f"/api/project/projects/{pid}/timeline?kinds=nonsense", headers=AUTH).status_code
        == 422
    )
    assert client.get(f"/api/project/projects/{pid}/timeline/verify", headers=AUTH).json() == {
        "ok": True,
        "problems": [],
    }
    assert (
        client.get(f"/api/project/projects/{uuid.uuid4()}/timeline", headers=AUTH).status_code
        == 404
    )


def test_the_history_routes(client) -> None:  # type: ignore[no-untyped-def]
    pid = client.post("/api/project/projects", headers=AUTH, json={"title": "History"}).json()["id"]
    node = client.post(
        f"/api/project/projects/{pid}/nodes",
        headers=AUTH,
        json={"kind": "task", "title": "A", "body": "x\n"},
    ).json()
    changed = client.patch(
        f"/api/project/nodes/{node['id']}",
        headers=AUTH,
        json={"revision": node["revision"], "body": "y\n"},
    ).json()
    history = client.get(f"/api/project/nodes/{node['id']}/history", headers=AUTH).json()
    assert [v["rev"] for v in history["versions"]] == [node["revision"], changed["revision"]]
    assert history["versions"][0]["kind"] == "snapshot" and history["versions"][1]["kind"] == "diff"
    at = client.get(
        f"/api/project/nodes/{node['id']}/at?rev={node['revision']}", headers=AUTH
    ).json()
    assert at["body"] == "x\n"
    diff = client.get(
        f"/api/project/nodes/{node['id']}/diff?from={node['revision']}&to={changed['revision']}",
        headers=AUTH,
    ).json()
    assert diff["body"][-1]["new"] == ["y\n"]
    assert client.get(f"/api/project/nodes/{uuid.uuid4()}/history", headers=AUTH).status_code == 404


def test_the_size_route_is_for_the_admin(client) -> None:  # type: ignore[no-untyped-def]
    size = client.get("/api/project/akte/size", headers=AUTH)
    assert size.status_code == 200
    body = size.json()
    assert body["events"] >= 0 and body["events_bytes"] > 0 and body["warning"] is False
    _, headers = make_user(TEST_DB_URL, "akte-size")
    assert client.get("/api/project/akte/size", headers=headers).status_code == 403
