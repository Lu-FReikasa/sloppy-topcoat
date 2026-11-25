"""04b against PostgreSQL: fields, labels, relations across projects, reminders, the trash, replaced
reports. Every change is also checked for its line in the Akte and for the scoping by person."""

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import create_engine, text

from tests.conftest import AUTH, TEST_DB_URL
from tests.users import make_user
from topcoat_hub.models import DEV_ADMIN_ID
from topcoat_hub.project_events import Actor
from topcoat_hub.project_store import Invalid, NotFound, ProjectStore

T0 = datetime(2026, 10, 29, 9, 0, tzinfo=UTC)


@pytest.fixture
def store(client) -> ProjectStore:  # type: ignore[no-untyped-def]
    return ProjectStore(client.app.state.sessionmaker)


async def two_tasks(store: ProjectStore, title: str = "P"):  # type: ignore[no-untyped-def]
    project = await store.create_project(DEV_ADMIN_ID, title=title)
    a = await store.create_node(DEV_ADMIN_ID, project.id, kind="task", title="a")
    b = await store.create_node(DEV_ADMIN_ID, project.id, kind="task", title="b")
    return project, a, b


async def kinds(store: ProjectStore, project_id: uuid.UUID) -> list[str]:
    return [e.kind for e in await store.timeline(DEV_ADMIN_ID, project_id, limit=200)]


# --- fields -------------------------------------------------------------------------------------


async def test_a_task_keeps_its_fields_and_changes_are_lines(store: ProjectStore) -> None:
    project = await store.create_project(DEV_ADMIN_ID, title="Fields")
    node = await store.create_node(
        DEV_ADMIN_ID, project.id, kind="task", title="t",
        fields={"due_at": T0, "priority": 2, "color": "#00ff00", "estimate_minutes": 30},
    )  # fmt: skip
    again = await store.get_node(DEV_ADMIN_ID, node.id)
    assert again.due_at == T0 and again.priority == 2 and again.estimate_minutes == 30
    assert again.status == "todo" and again.percent_done == 0 and not again.is_milestone
    node = await store.update_node(
        DEV_ADMIN_ID, node.id, revision=node.revision, fields={"priority": 5, "due_at": None}
    )
    assert node.priority == 5 and node.due_at is None
    event = (await store.timeline(DEV_ADMIN_ID, project.id, limit=200))[-1]
    assert event.kind == "node.updated"
    assert event.payload == {"priority": [2, 5], "due_at": [T0.isoformat(), None]}


async def test_done_sets_the_time_and_reopening_clears_it(store: ProjectStore) -> None:
    project, a, _ = await two_tasks(store)
    a = await store.update_node(DEV_ADMIN_ID, a.id, revision=a.revision, status="in_review")
    assert a.status == "in_review" and a.done_at is None
    a = await store.update_node(DEV_ADMIN_ID, a.id, revision=a.revision, status="done")
    assert a.done_at is not None
    a = await store.update_node(DEV_ADMIN_ID, a.id, revision=a.revision, status="todo")
    assert a.done_at is None
    assert (await kinds(store, project.id))[-3:] == [
        "node.status_changed", "node.completed", "node.reopened",
    ]  # fmt: skip


async def test_a_milestone_needs_a_date(store: ProjectStore) -> None:
    _, a, _ = await two_tasks(store)
    with pytest.raises(Invalid, match="milestone"):
        await store.update_node(
            DEV_ADMIN_ID, a.id, revision=a.revision, fields={"is_milestone": True}
        )
    a = await store.update_node(
        DEV_ADMIN_ID, a.id, revision=a.revision, fields={"is_milestone": True, "end_at": T0}
    )
    assert a.is_milestone
    with pytest.raises(Invalid, match="milestone"):  # taking the date away breaks the rule
        await store.update_node(DEV_ADMIN_ID, a.id, revision=a.revision, fields={"end_at": None})


async def test_wrong_fields_are_refused_and_leave_no_line(store: ProjectStore) -> None:
    project, a, _ = await two_tasks(store)
    before = await kinds(store, project.id)
    for fields in (
        {"priority": 9},
        {"due_at": datetime(2026, 1, 1)},
        {"start_at": T0 + timedelta(days=2), "end_at": T0},
        {"nonsense": 1},
    ):
        with pytest.raises(Invalid):
            await store.update_node(DEV_ADMIN_ID, a.id, revision=a.revision, fields=fields)
    topic = await store.create_node(DEV_ADMIN_ID, project.id, kind="topic", title="folder")
    with pytest.raises(Invalid):
        await store.update_node(
            DEV_ADMIN_ID, topic.id, revision=topic.revision, fields={"priority": 1}
        )
    assert (await kinds(store, project.id))[: len(before)] == before


async def test_a_repeat_rule_keeps_its_zone_and_its_anchor_day(store: ProjectStore) -> None:
    _, a, _ = await two_tasks(store)
    due = datetime(2026, 1, 31, 9, 0, tzinfo=UTC)
    a = await store.update_node(
        DEV_ADMIN_ID, a.id, revision=a.revision,
        fields={"due_at": due, "repeat_rule": {"every": 1, "unit": "month", "mode": "monthly"}},
    )  # fmt: skip
    assert a.repeat_rule == {
        "every": 1, "unit": "month", "mode": "monthly", "tz": "UTC", "anchor_day": 31,
    }  # fmt: skip


async def test_a_report_can_say_what_it_describes_and_be_replaced(store: ProjectStore) -> None:
    project = await store.create_project(DEV_ADMIN_ID, title="Facts")
    old = await store.create_node(
        DEV_ADMIN_ID, project.id, kind="report", title="Node X runs v1", report_kind="fact",
        fields={"as_of": T0, "origin_app": "admin"},
    )  # fmt: skip
    assert old.as_of == T0 and old.origin_app == "admin" and old.current
    new = await store.create_node(
        DEV_ADMIN_ID, project.id, kind="report", title="Node X runs v2", report_kind="fact"
    )
    new = await store.supersede_report(DEV_ADMIN_ID, old.id, by_id=new.id)
    old = await store.get_node(DEV_ADMIN_ID, old.id)
    assert new.supersedes_id == old.id and old.current is False  # the old one is kept
    assert (await kinds(store, project.id))[-1] == "report.superseded"
    with pytest.raises(Invalid):
        await store.supersede_report(DEV_ADMIN_ID, old.id, by_id=new.id)  # already replaced
    task = await store.create_node(DEV_ADMIN_ID, project.id, kind="task", title="t")
    with pytest.raises(Invalid):
        await store.supersede_report(DEV_ADMIN_ID, new.id, by_id=task.id)


# --- projects -----------------------------------------------------------------------------------


async def test_project_fields_identifier_and_parent(store: ProjectStore) -> None:
    parent = await store.create_project(DEV_ADMIN_ID, title="Parent", identifier="top")
    assert parent.identifier == "TOP"
    child = await store.create_project(
        DEV_ADMIN_ID, title="Child", parent_id=parent.id, favorite=True, color="#112233"
    )
    assert child.parent_id == parent.id and child.favorite and child.color == "#112233"
    with pytest.raises(Invalid):
        await store.create_project(DEV_ADMIN_ID, title="Other", identifier="TOP")
    with pytest.raises(Invalid):
        await store.create_project(DEV_ADMIN_ID, title="Bad", identifier="no spaces!")
    with pytest.raises(Invalid, match="below itself"):
        await store.update_project(
            DEV_ADMIN_ID, parent.id, revision=parent.revision, parent_id=child.id
        )
    child = await store.update_project(
        DEV_ADMIN_ID, child.id, revision=child.revision, parent_id=None, color=None
    )
    assert child.parent_id is None and child.color is None
    assert (await kinds(store, child.id))[-1] == "project.updated"


# --- labels -------------------------------------------------------------------------------------


async def test_labels_are_the_persons_own_and_their_use_is_a_line(store: ProjectStore) -> None:
    project, a, b = await two_tasks(store, "Labels")
    red = await store.create_label(DEV_ADMIN_ID, title="urgent", color="#ff0000")
    with pytest.raises(Invalid):
        await store.create_label(DEV_ADMIN_ID, title="urgent")
    blue = await store.create_label(DEV_ADMIN_ID, title="later", description="not now")
    got = await store.set_node_labels(DEV_ADMIN_ID, a.id, add=[red.id, blue.id, red.id])
    assert [label.title for label in got] == ["later", "urgent"]
    await store.set_node_labels(DEV_ADMIN_ID, a.id, remove=[blue.id])
    assert [label.title for label in await store.labels_of(DEV_ADMIN_ID, a.id)] == ["urgent"]
    assert (await kinds(store, project.id)).count("label.added") == 2
    assert (await kinds(store, project.id)).count("label.removed") == 1
    many = await store.labels_for_nodes(DEV_ADMIN_ID, [a.id, b.id])
    assert len(many[a.id]) == 1 and many[b.id] == []
    renamed = await store.update_label(DEV_ADMIN_ID, red.id, title="critical")
    assert renamed.title == "critical" and renamed.color == "#ff0000"
    await store.delete_label(DEV_ADMIN_ID, red.id)
    assert await store.labels_of(DEV_ADMIN_ID, a.id) == []
    last = (await store.timeline(DEV_ADMIN_ID, project.id, limit=200))[-1]
    assert last.kind == "label.removed" and last.payload["label_deleted"] is True


async def test_nobody_uses_a_label_of_somebody_else(store: ProjectStore) -> None:
    _, a, _ = await two_tasks(store, "Mine")
    mine = await store.create_label(DEV_ADMIN_ID, title="mine")
    other, _headers = make_user(TEST_DB_URL, "label-other")
    theirs = await store.create_label(other, title="mine")  # the same title is fine for them
    assert theirs.id != mine.id
    with pytest.raises(NotFound):
        await store.set_node_labels(other, a.id, add=[theirs.id])
    with pytest.raises(NotFound):
        await store.set_node_labels(DEV_ADMIN_ID, a.id, add=[theirs.id])
    assert [label.id for label in await store.list_labels(other)] == [theirs.id]
    with pytest.raises(NotFound):
        await store.update_label(other, mine.id, title="x")


# --- relations ----------------------------------------------------------------------------------


async def test_a_task_can_be_blocked_by_a_task_of_another_project(store: ProjectStore) -> None:
    p1, a, _ = await two_tasks(store, "One")
    p2, c, _ = await two_tasks(store, "Two")
    rel = await store.add_relation(DEV_ADMIN_ID, a.id, c.id, "blocked_by")
    assert (await store.get_node(DEV_ADMIN_ID, a.id)).depends_on == [str(c.id)]
    # one line in each project's Akte, tied together
    line1 = (await store.timeline(DEV_ADMIN_ID, p1.id, limit=200))[-1]
    line2 = (await store.timeline(DEV_ADMIN_ID, p2.id, limit=200))[-1]
    assert (line1.kind, line2.kind) == ("relation.added", "relation.added")
    assert line1.group_id == line2.group_id and line1.node_id == a.id and line2.node_id == c.id
    assert line1.payload["other"] == "Two 1"
    assert await store.verify_chain(DEV_ADMIN_ID, p1.id) == []
    assert await store.verify_chain(DEV_ADMIN_ID, p2.id) == []
    with pytest.raises(Invalid, match="exists"):
        await store.add_relation(DEV_ADMIN_ID, a.id, c.id, "blocked_by")
    # seen from the other side, too
    on_c = await store.relations_of(DEV_ADMIN_ID, c.id)
    assert [(r.kind, n.id) for r, n in on_c] == [("blocked_by", a.id)] and on_c[0][0].id == rel.id


async def test_a_loop_of_blockers_across_projects_is_refused(store: ProjectStore) -> None:
    _, a, _ = await two_tasks(store, "One")
    _, c, _ = await two_tasks(store, "Two")
    await store.add_relation(DEV_ADMIN_ID, a.id, c.id, "blocked_by")
    with pytest.raises(Invalid, match="cyclic"):
        await store.add_relation(DEV_ADMIN_ID, c.id, a.id, "blocked_by")
    with pytest.raises(Invalid, match="cyclic"):
        await store.add_relation(
            DEV_ADMIN_ID, a.id, c.id, "precedes"
        )  # a before c = c blocked by a
    await store.add_relation(DEV_ADMIN_ID, c.id, a.id, "precedes")  # c before a: the same as before
    with pytest.raises(Invalid, match="cannot be related to itself"):
        await store.add_relation(DEV_ADMIN_ID, a.id, a.id, "related")


async def test_an_ended_relation_stays_in_the_history(store: ProjectStore) -> None:
    project, a, b = await two_tasks(store, "History")
    rel = await store.add_relation(DEV_ADMIN_ID, a.id, b.id, "blocked_by")
    ended = await store.end_relation(DEV_ADMIN_ID, rel.id)
    assert ended.valid_to is not None and ended.valid_from <= ended.valid_to
    assert (await store.get_node(DEV_ADMIN_ID, a.id)).depends_on == []
    assert await store.relations_of(DEV_ADMIN_ID, a.id) == []
    history = await store.relations_of(DEV_ADMIN_ID, a.id, include_ended=True)
    assert [r.id for r, _ in history] == [rel.id]
    assert (await kinds(store, project.id))[-1] == "relation.ended"
    with pytest.raises(NotFound):
        await store.end_relation(DEV_ADMIN_ID, rel.id)
    again = await store.add_relation(DEV_ADMIN_ID, a.id, b.id, "blocked_by")  # may hold again
    assert again.id != rel.id


async def test_the_kinds_of_relation_have_their_rules(store: ProjectStore) -> None:
    project, a, b = await two_tasks(store, "Kinds")
    topic = await store.create_node(DEV_ADMIN_ID, project.id, kind="topic", title="folder")
    note1 = await store.create_node(
        DEV_ADMIN_ID, project.id, kind="report", title="n1", report_kind="note"
    )
    note2 = await store.create_node(
        DEV_ADMIN_ID, project.id, kind="report", title="n2", report_kind="note"
    )
    with pytest.raises(Invalid, match="two tasks"):
        await store.add_relation(DEV_ADMIN_ID, a.id, topic.id, "blocked_by")
    with pytest.raises(Invalid, match="two reports"):
        await store.add_relation(DEV_ADMIN_ID, a.id, b.id, "supersedes")
    await store.add_relation(DEV_ADMIN_ID, note1.id, note2.id, "supersedes")
    await store.add_relation(DEV_ADMIN_ID, a.id, topic.id, "related")
    with pytest.raises(Invalid, match="exists"):  # related is symmetric: the other way is the same
        await store.add_relation(DEV_ADMIN_ID, topic.id, a.id, "related")
    with pytest.raises(Invalid):
        await store.add_relation(DEV_ADMIN_ID, a.id, b.id, "friends_with")  # type: ignore[arg-type]


async def test_blocked_by_through_depends_on_is_the_same_thing(store: ProjectStore) -> None:
    project, a, b = await two_tasks(store, "Compat")
    c = await store.create_node(
        DEV_ADMIN_ID, project.id, kind="task", title="c", depends_on=[a.id, b.id]
    )
    assert sorted(c.depends_on) == sorted([str(a.id), str(b.id)])
    assert [r.kind for r, _ in await store.relations_of(DEV_ADMIN_ID, c.id)] == ["blocked_by"] * 2
    c = await store.update_node(DEV_ADMIN_ID, c.id, revision=c.revision, depends_on=[b.id])
    assert c.depends_on == [str(b.id)]
    assert (await kinds(store, project.id)).count("relation.ended") == 1
    c = await store.update_node(DEV_ADMIN_ID, c.id, revision=c.revision, depends_on=[])
    assert c.depends_on == []
    topic = await store.create_node(DEV_ADMIN_ID, project.id, kind="topic", title="t")
    with pytest.raises(Invalid, match="only a task"):
        await store.update_node(DEV_ADMIN_ID, topic.id, revision=topic.revision, depends_on=[a.id])


async def test_nobody_relates_to_a_node_of_somebody_else(store: ProjectStore) -> None:
    _, a, _ = await two_tasks(store, "Mine")
    other, _ = make_user(TEST_DB_URL, "relation-other")
    theirs_project = await store.create_project(other, title="Theirs")
    theirs = await store.create_node(other, theirs_project.id, kind="task", title="x")
    with pytest.raises(NotFound):
        await store.add_relation(DEV_ADMIN_ID, a.id, theirs.id, "blocked_by")
    with pytest.raises(NotFound):
        await store.add_relation(other, theirs.id, a.id, "blocked_by")
    with pytest.raises(NotFound):
        await store.create_node(
            DEV_ADMIN_ID, (await store.list_projects(DEV_ADMIN_ID))[0].id, kind="task", title="y",
            depends_on=[theirs.id],
        )  # fmt: skip


# --- reminders ----------------------------------------------------------------------------------


async def test_reminders_follow_the_dates_of_the_task(store: ProjectStore) -> None:
    project, a, _ = await two_tasks(store, "Reminders")
    fixed = await store.add_reminder(DEV_ADMIN_ID, a.id, at=T0)
    before_due = await store.add_reminder(
        DEV_ADMIN_ID, a.id, relative_to="due", offset_seconds=-3600
    )
    assert fixed.fire_at == T0 and before_due.fire_at is None  # waits for a due date
    a = await store.update_node(
        DEV_ADMIN_ID, a.id, revision=a.revision, fields={"due_at": T0 + timedelta(days=1)}
    )
    got = {r.id: r for r in await store.reminders_of(DEV_ADMIN_ID, a.id)}
    assert got[before_due.id].fire_at == T0 + timedelta(hours=23) and got[fixed.id].fire_at == T0
    a = await store.update_node(
        DEV_ADMIN_ID, a.id, revision=a.revision, fields={"due_at": T0 + timedelta(days=2)}
    )
    got = {r.id: r for r in await store.reminders_of(DEV_ADMIN_ID, a.id)}
    assert got[before_due.id].fire_at == T0 + timedelta(days=1, hours=23)
    a = await store.update_node(DEV_ADMIN_ID, a.id, revision=a.revision, fields={"due_at": None})
    assert (await store.reminders_of(DEV_ADMIN_ID, a.id))[-1].fire_at is None
    await store.remove_reminder(DEV_ADMIN_ID, fixed.id)
    assert (await kinds(store, project.id)).count("reminder.added") == 2
    assert (await kinds(store, project.id))[-1] == "reminder.removed"


async def test_a_reminder_that_fired_comes_again_for_a_new_date(store: ProjectStore) -> None:
    _, a, _ = await two_tasks(store, "Again")
    a = await store.update_node(DEV_ADMIN_ID, a.id, revision=a.revision, fields={"due_at": T0})
    r = await store.add_reminder(DEV_ADMIN_ID, a.id, relative_to="due", offset_seconds=0)
    engine = create_engine(TEST_DB_URL)
    with engine.begin() as conn:
        conn.execute(
            text("UPDATE project_reminders SET fired_at = :t WHERE id = :id"),
            {"t": T0 + timedelta(minutes=1), "id": r.id},
        )
    a = await store.update_node(
        DEV_ADMIN_ID, a.id, revision=a.revision, fields={"due_at": T0 + timedelta(days=3)}
    )
    again = (await store.reminders_of(DEV_ADMIN_ID, a.id))[0]
    assert again.fired_at is None and again.fire_at == T0 + timedelta(days=3)


async def test_wrong_reminders_are_refused(store: ProjectStore) -> None:
    project, a, _ = await two_tasks(store, "Wrong")
    topic = await store.create_node(DEV_ADMIN_ID, project.id, kind="topic", title="t")
    for kwargs in (
        {},
        {"at": T0, "relative_to": "due", "offset_seconds": 0},
        {"relative_to": "due"},
        {"relative_to": "someday", "offset_seconds": 1},
        {"at": datetime(2026, 1, 1)},
        {"relative_to": "due", "offset_seconds": 10**9},
    ):
        with pytest.raises(Invalid):
            await store.add_reminder(DEV_ADMIN_ID, a.id, **kwargs)
    with pytest.raises(Invalid, match="only a task"):
        await store.add_reminder(DEV_ADMIN_ID, topic.id, at=T0)
    for i in range(10):
        await store.add_reminder(DEV_ADMIN_ID, a.id, at=T0 + timedelta(minutes=i))
    with pytest.raises(Invalid, match="at most"):
        await store.add_reminder(DEV_ADMIN_ID, a.id, at=T0)


# --- the trash ----------------------------------------------------------------------------------


async def tree_with_branch(store: ProjectStore, title: str):  # type: ignore[no-untyped-def]
    project = await store.create_project(DEV_ADMIN_ID, title=title)
    top = await store.create_node(DEV_ADMIN_ID, project.id, kind="topic", title="top")
    child = await store.create_node(
        DEV_ADMIN_ID, project.id, kind="task", title="child", parent_id=top.id, body="secret text"
    )
    other = await store.create_node(DEV_ADMIN_ID, project.id, kind="task", title="other")
    return project, top, child, other


async def test_a_branch_goes_to_the_trash_and_comes_back(store: ProjectStore) -> None:
    project, top, child, other = await tree_with_branch(store, "Trash")
    await store.trash_node(DEV_ADMIN_ID, top.id)
    assert [n.title for n in await store.tree(DEV_ADMIN_ID, project.id)] == ["other"]
    assert len(await store.tree(DEV_ADMIN_ID, project.id, include_trashed=True)) == 3
    projects, nodes = await store.list_trash(DEV_ADMIN_ID)
    assert projects == [] and [n.title for n, _ in nodes] == ["top"]  # only the root of the branch
    with pytest.raises(Invalid, match="in the trash already"):
        await store.trash_node(DEV_ADMIN_ID, top.id)
    with pytest.raises(Invalid, match="below which it sat"):
        await store.restore_node(DEV_ADMIN_ID, child.id)
    await store.restore_node(DEV_ADMIN_ID, top.id)
    assert len(await store.tree(DEV_ADMIN_ID, project.id)) == 3
    assert (await kinds(store, project.id))[-2:] == ["node.trashed", "node.restored"]
    # a relation to a trashed node is hidden, not lost
    await store.add_relation(DEV_ADMIN_ID, other.id, child.id, "blocked_by")
    await store.trash_node(DEV_ADMIN_ID, top.id)
    assert (await store.get_node(DEV_ADMIN_ID, other.id)).depends_on == []
    await store.restore_node(DEV_ADMIN_ID, top.id)
    assert (await store.get_node(DEV_ADMIN_ID, other.id)).depends_on == [str(child.id)]


async def test_a_trashed_node_cannot_be_related(store: ProjectStore) -> None:
    _, top, _, other = await tree_with_branch(store, "Hidden")
    await store.trash_node(DEV_ADMIN_ID, top.id)
    with pytest.raises(Invalid, match="trash"):
        await store.add_relation(DEV_ADMIN_ID, other.id, top.id, "related")


async def test_a_purge_needs_the_trash_and_the_title(store: ProjectStore) -> None:
    project, top, _, _ = await tree_with_branch(store, "Confirm")
    with pytest.raises(Invalid, match="in the trash"):
        await store.purge_node(DEV_ADMIN_ID, top.id, confirm_title="top")
    await store.trash_node(DEV_ADMIN_ID, top.id)
    with pytest.raises(Invalid, match="type the title"):
        await store.purge_node(DEV_ADMIN_ID, top.id, confirm_title="wrong")
    await store.trash_project(DEV_ADMIN_ID, project.id)
    with pytest.raises(Invalid, match="type the title"):
        await store.purge_project(DEV_ADMIN_ID, project.id, confirm_title="nope")
    with pytest.raises(Invalid, match="mode"):
        await store.purge_project(DEV_ADMIN_ID, project.id, confirm_title="Confirm", mode="x")  # type: ignore[arg-type]


async def test_purging_a_node_keeps_the_lines_but_not_the_texts(store: ProjectStore) -> None:
    project, top, child, other = await tree_with_branch(store, "Keep")
    child = await store.update_node(
        DEV_ADMIN_ID, child.id, revision=child.revision, body="even more secret text"
    )
    await store.add_relation(DEV_ADMIN_ID, other.id, child.id, "related")
    await store.trash_node(DEV_ADMIN_ID, top.id)
    await store.purge_node(DEV_ADMIN_ID, top.id, confirm_title="top")
    assert [n.title for n in await store.tree(DEV_ADMIN_ID, project.id, include_trashed=True)] == [
        "other"
    ]
    events = await store.timeline(DEV_ADMIN_ID, project.id, limit=200)
    lines = [e for e in events if e.node_title in ("top", "child") and e.kind != "node.purged"]
    assert lines and all(e.payload is None and e.reason == "" for e in lines)  # texts gone
    assert any(e.node_ref == "Keep 1.1" for e in lines)  # the line is still readable
    assert events[-1].kind == "node.purged" and events[-1].payload == {"nodes": 2}
    assert await store.verify_chain(DEV_ADMIN_ID, project.id) == []
    engine = create_engine(TEST_DB_URL)
    with engine.connect() as conn:
        assert not conn.scalar(
            text("SELECT count(*) FROM project_node_versions WHERE node_id IN (:a, :b)"),
            {"a": top.id, "b": child.id},
        )


async def test_purging_a_project_keeps_the_timeline_or_removes_everything(
    store: ProjectStore,
) -> None:
    keep, _, _, _ = await tree_with_branch(store, "Keep it")
    gone, _, _, _ = await tree_with_branch(store, "Gone")
    for p in (keep, gone):
        await store.trash_project(DEV_ADMIN_ID, p.id)
    assert {p.title for p in (await store.list_trash(DEV_ADMIN_ID))[0]} == {"Keep it", "Gone"}
    assert all(p.title not in ("Keep it", "Gone") for p in await store.list_projects(DEV_ADMIN_ID))
    await store.purge_project(DEV_ADMIN_ID, keep.id, confirm_title="Keep it")
    await store.purge_project(DEV_ADMIN_ID, gone.id, confirm_title="Gone", mode="remove_everything")
    engine = create_engine(TEST_DB_URL)
    with engine.connect() as conn:
        left = conn.execute(
            text("SELECT kind, payload, project_title FROM project_events WHERE project_id = :p"),
            {"p": keep.id},
        ).all()
        assert left and all(r.payload is None or r.kind == "project.purged" for r in left)
        assert left[-1].kind == "project.purged"
        assert not conn.scalar(
            text("SELECT count(*) FROM project_events WHERE project_id = :p"), {"p": gone.id}
        )
        for table in ("projects",):
            assert not conn.scalar(
                text(f"SELECT count(*) FROM {table} WHERE id IN (:a, :b)"),  # noqa: S608
                {"a": keep.id, "b": gone.id},
            )
        assert not conn.scalar(
            text("SELECT count(*) FROM project_nodes WHERE project_id IN (:a, :b)"),
            {"a": keep.id, "b": gone.id},
        )


async def test_a_restored_project_comes_back_and_its_nodes_wait_for_it(store: ProjectStore) -> None:
    project, top, _, _ = await tree_with_branch(store, "Both")
    await store.trash_node(DEV_ADMIN_ID, top.id)
    await store.trash_project(DEV_ADMIN_ID, project.id)
    with pytest.raises(Invalid, match="restore it first"):
        await store.restore_node(DEV_ADMIN_ID, top.id)
    await store.restore_project(DEV_ADMIN_ID, project.id)
    await store.restore_node(DEV_ADMIN_ID, top.id)
    assert {p.title for p in await store.list_projects(DEV_ADMIN_ID)} >= {"Both"}


async def test_the_trash_is_the_persons_own(store: ProjectStore) -> None:
    project, top, _, _ = await tree_with_branch(store, "Private")
    other, _ = make_user(TEST_DB_URL, "trash-other")
    for call in (
        store.trash_project(other, project.id),
        store.trash_node(other, top.id),
        store.restore_node(other, top.id),
        store.purge_node(other, top.id, confirm_title="top"),
        store.purge_project(other, project.id, confirm_title="Private"),
    ):
        with pytest.raises(NotFound):
            await call
    assert await store.list_trash(other) == ([], [])


# --- the routes ---------------------------------------------------------------------------------


def test_fields_come_and_go_through_the_routes_with_offsets(client) -> None:  # type: ignore[no-untyped-def]
    pid = client.post("/api/project/projects", headers=AUTH, json={"title": "Routes04b"}).json()[
        "id"
    ]
    node = client.post(
        f"/api/project/projects/{pid}/nodes",
        headers=AUTH,
        json={"kind": "task", "title": "t", "due_at": "2026-10-29T17:00:00+01:00", "priority": 4},
    ).json()
    assert node["due_at"].endswith("+01:00") and node["priority"] == 4 and node["progress"] == 0
    changed = client.patch(
        f"/api/project/nodes/{node['id']}",
        headers=AUTH,
        json={"revision": node["revision"], "due_at": None, "status": "in_review"},
    ).json()
    assert changed["due_at"] is None and changed["status"] == "in_review"
    naive = client.patch(
        f"/api/project/nodes/{node['id']}",
        headers=AUTH,
        json={"revision": changed["revision"], "due_at": "2026-10-29T17:00:00"},
    )
    assert naive.status_code == 422
    tree = client.get(f"/api/project/projects/{pid}", headers=AUTH).json()
    assert tree["nodes"][0]["labels"] == []


def test_labels_relations_reminders_and_trash_routes(client) -> None:  # type: ignore[no-untyped-def]
    p1 = client.post("/api/project/projects", headers=AUTH, json={"title": "R-one"}).json()["id"]
    p2 = client.post("/api/project/projects", headers=AUTH, json={"title": "R-two"}).json()["id"]
    mk = lambda pid, title: client.post(  # noqa: E731
        f"/api/project/projects/{pid}/nodes", headers=AUTH, json={"kind": "task", "title": title}
    ).json()
    a, b = mk(p1, "a"), mk(p2, "b")
    label = client.post("/api/project/labels", headers=AUTH, json={"title": "L"}).json()
    assert client.post("/api/project/labels", headers=AUTH, json={"title": "L"}).status_code == 422
    put = client.put(
        f"/api/project/nodes/{a['id']}/labels", headers=AUTH, json={"add": [label["id"]]}
    )
    assert [x["title"] for x in put.json()["labels"]] == ["L"]
    rel = client.post(
        f"/api/project/nodes/{a['id']}/relations",
        headers=AUTH,
        json={"other_id": b["id"], "kind": "blocked_by"},
    )
    assert rel.status_code == 201
    got = rel.json()["relations"][0]
    assert got["direction"] == "out" and got["other"]["ref"] == "R-two 1"
    back = client.get(f"/api/project/nodes/{b['id']}/relations", headers=AUTH).json()["relations"]
    assert back[0]["direction"] == "in"
    loop = client.post(
        f"/api/project/nodes/{b['id']}/relations",
        headers=AUTH,
        json={"other_id": a["id"], "kind": "blocked_by"},
    )
    assert loop.status_code == 422
    assert client.post(f"/api/project/relations/{got['id']}/end", headers=AUTH).status_code == 200
    ended = client.get(f"/api/project/nodes/{a['id']}/relations?ended=true", headers=AUTH).json()
    assert ended["relations"][0]["valid_to"] is not None
    rem = client.post(
        f"/api/project/nodes/{a['id']}/reminders",
        headers=AUTH,
        json={"at": "2026-12-01T09:00:00+01:00"},
    )
    assert rem.status_code == 201 and rem.json()["fire_at"].startswith("2026-12-01T09:00")
    assert (
        client.post(
            f"/api/project/nodes/{a['id']}/reminders",
            headers=AUTH,
            json={"at": "2026-12-01T09:00:00"},
        ).status_code
        == 422
    )
    assert (
        client.delete(f"/api/project/reminders/{rem.json()['id']}", headers=AUTH).status_code == 204
    )
    # trash and purge
    assert client.post(f"/api/project/nodes/{a['id']}/trash", headers=AUTH).status_code == 200
    trash = client.get("/api/project/trash", headers=AUTH).json()
    assert [n["title"] for n in trash["nodes"]] == ["a"] and trash["nodes"][0][
        "project_title"
    ] == "R-one"
    wrong = client.post(
        f"/api/project/nodes/{a['id']}/purge", headers=AUTH, json={"confirm_title": "x"}
    )
    assert wrong.status_code == 422
    done = client.post(
        f"/api/project/nodes/{a['id']}/purge", headers=AUTH, json={"confirm_title": "a"}
    )
    assert done.status_code == 204
    assert (
        client.post(
            f"/api/project/projects/{p2}/purge",
            headers=AUTH,
            json={"confirm_title": "R-two", "mode": "keep_timeline"},
        ).status_code
        == 422
    )  # not in the trash yet


def test_an_api_key_cannot_empty_the_trash(client) -> None:  # type: ignore[no-untyped-def]
    pid = client.post("/api/project/projects", headers=AUTH, json={"title": "Keyed"}).json()["id"]
    assert client.post(f"/api/project/projects/{pid}/trash", headers=AUTH).status_code == 200
    _, key = make_user(TEST_DB_URL, "trash-key")
    assert client.post(
        f"/api/project/projects/{pid}/purge", headers=key, json={"confirm_title": "Keyed"}
    ).status_code in (403, 404)


def test_the_time_zone_of_a_person(client) -> None:  # type: ignore[no-untyped-def]
    me = client.get("/api/me", headers=AUTH).json()
    assert me["timezone"] is None and me["effective_timezone"] == me["hub_timezone"] == "UTC"
    put = client.put("/api/me/timezone", headers=AUTH, json={"timezone": "Europe/Berlin"})
    assert put.json()["timezone"] == "Europe/Berlin"
    assert put.json()["effective_timezone"] == "Europe/Berlin"
    assert (
        client.put("/api/me/timezone", headers=AUTH, json={"timezone": "Mars/Base"}).status_code
        == 422
    )
    assert (
        client.put("/api/me/timezone", headers=AUTH, json={"timezone": None}).json()["timezone"]
        is None
    )


async def test_the_agent_is_recorded_as_the_actor_of_what_it_does(store: ProjectStore) -> None:
    project = await store.create_project(DEV_ADMIN_ID, title="Who")
    task = uuid.uuid4()
    agent = store.with_actor(Actor("agent", agent_task_id=task))
    assert agent.default_zone == "UTC"
    node = await agent.create_node(DEV_ADMIN_ID, project.id, kind="task", title="x")
    await agent.update_node(DEV_ADMIN_ID, node.id, revision=node.revision, fields={"priority": 1})
    events = await store.timeline(DEV_ADMIN_ID, project.id, limit=200)
    assert {e.actor_kind for e in events[1:]} == {"agent"} and events[-1].agent_task_id == task
