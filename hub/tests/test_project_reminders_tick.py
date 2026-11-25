"""04h against PostgreSQL: the reminder tick, quiet hours, the daily digest and completing a
repeating task. Time is a fake clock (``clock.set_clock``); the tick is called by hand.

What is tested: a due reminder reaches the inbox once and leaves the line ``reminder.fired`` made
by the system; nothing fires twice (also with two ticks at once); after downtime what is not too old
is delivered and the rest is closed with a line that says why; quiet hours (in the person's zone,
across midnight) hold things back; the digest is one item a day; a repeating task comes back with
moved dates, reminders and an unticked checklist and the finished occurrence stays in the Akte; the
scoping by person; migration 0083.
"""

import asyncio
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from alembic import command
from sqlalchemy import create_engine, select, text

from tests.conftest import AUTH, TEST_DB_URL, alembic_config
from tests.users import make_user
from topcoat_hub import background, clock
from topcoat_hub.config import Settings
from topcoat_hub.inbox import AUTO_RESOLVED
from topcoat_hub.models import DEV_ADMIN_ID, InboxItem
from topcoat_hub.project_store import Conflict, Invalid, NotFound, ProjectStore
from topcoat_hub.project_tick import (
    KIND_DIGEST,
    KIND_REMINDER,
    ReminderTick,
    in_quiet,
    parse_minutes,
    write_prefs,
)

T0 = datetime(2026, 10, 29, 9, 0, tzinfo=UTC)


class Clock:
    def __init__(self, now: datetime) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now

    def advance(self, **delta: float) -> datetime:
        self.now += timedelta(**delta)
        return self.now


@pytest.fixture
def fake(client) -> Clock:  # type: ignore[no-untyped-def]
    c = Clock(T0)
    clock.set_clock(c)
    yield c  # type: ignore[misc]
    clock.set_clock(None)


@pytest.fixture
def store(client) -> ProjectStore:  # type: ignore[no-untyped-def]
    return ProjectStore(client.app.state.sessionmaker)


@pytest.fixture
def tick(client) -> ReminderTick:  # type: ignore[no-untyped-def]
    return ReminderTick(client.app.state.sessionmaker, client.app.state.settings)


async def items(client, user_id: uuid.UUID, kind: str | None = None) -> list[InboxItem]:  # type: ignore[no-untyped-def]
    async with client.app.state.sessionmaker() as db:
        statement = select(InboxItem).where(InboxItem.user_id == user_id)
        if kind:
            statement = statement.where(InboxItem.kind == kind)
        return list(await db.scalars(statement.order_by(InboxItem.created_at)))


async def kinds(store: ProjectStore, project_id: uuid.UUID, user_id=DEV_ADMIN_ID) -> list[str]:  # type: ignore[no-untyped-def]
    return [e.kind for e in await store.timeline(user_id, project_id, limit=200)]


async def a_task(store: ProjectStore, user_id=DEV_ADMIN_ID, **fields):  # type: ignore[no-untyped-def]
    project = await store.create_project(user_id, title=f"P {uuid.uuid4().hex[:6]}")
    node = await store.create_node(
        user_id, project.id, kind="task", title="Pay the invoice", fields=fields
    )
    return project, node


# --- pure ---------------------------------------------------------------------------------------


def test_quiet_hours_with_and_without_midnight() -> None:
    def at(h: int, m: int = 0) -> datetime:
        return datetime(2026, 10, 29, h, m, tzinfo=UTC)

    assert in_quiet(at(23), 22 * 60, 7 * 60) and in_quiet(at(3), 22 * 60, 7 * 60)
    assert in_quiet(at(22), 22 * 60, 7 * 60)  # the start belongs to the span
    assert not in_quiet(at(7), 22 * 60, 7 * 60)  # the end does not
    assert not in_quiet(at(12), 22 * 60, 7 * 60)
    assert in_quiet(at(13), 12 * 60, 14 * 60) and not in_quiet(at(14), 12 * 60, 14 * 60)
    assert not in_quiet(at(23), None, None)
    assert not in_quiet(at(23), 5 * 60, 5 * 60)  # an empty span is no span
    assert not in_quiet(at(23), 22 * 60, None)


def test_a_time_of_day_is_parsed_strictly() -> None:
    assert parse_minutes("22:30") == 1350 and parse_minutes("00:00") == 0
    for bad in ("24:00", "7:5", "22", "22:60", "ab:cd", "22:30:00", "-1:00"):
        with pytest.raises(ValueError):
            parse_minutes(bad)


def test_the_tick_is_a_task_of_the_registry_and_reads_the_settings() -> None:
    settings = Settings(project_reminder_catchup_hours=12, project_digest_enabled=False)
    d = background.defaults("project_reminders", settings)
    assert d.limit("fire", 0) == 12 and d.step("fire").on and not d.step("digest").on


def test_the_new_inbox_kinds_are_not_resolved_by_the_sync() -> None:
    assert KIND_REMINDER not in AUTO_RESOLVED and KIND_DIGEST not in AUTO_RESOLVED


# --- firing -------------------------------------------------------------------------------------


async def test_a_due_reminder_reaches_the_inbox_once_and_leaves_a_line(
    client, store: ProjectStore, tick: ReminderTick, fake: Clock
) -> None:
    project, node = await a_task(store, due_at=T0 + timedelta(hours=2))
    reminder = await store.add_reminder(
        DEV_ADMIN_ID, node.id, relative_to="due", offset_seconds=-3600
    )
    assert reminder.fire_at == T0 + timedelta(hours=1)

    fake.now = T0 + timedelta(minutes=59)
    assert (await tick.run_once()).fired == 0 and not await items(client, DEV_ADMIN_ID)

    fake.now = T0 + timedelta(hours=1, seconds=30)
    result = await tick.run_once()
    assert (result.fired, result.expired, result.failed) == (1, 0, 0)
    [item] = await items(client, DEV_ADMIN_ID)
    assert item.kind == KIND_REMINDER and item.application == "project"
    assert item.title == "Pay the invoice" and not item.urgent
    assert item.ref["node_id"] == str(node.id) and item.ref["project_id"] == str(project.id)
    [row] = await store.reminders_of(DEV_ADMIN_ID, node.id)
    assert row.fired_at == fake.now

    event = (await store.timeline(DEV_ADMIN_ID, project.id, limit=200))[-1]
    assert event.kind == "reminder.fired" and event.actor_kind == "system"
    assert event.at == fake.now and event.node_id == node.id
    assert event.payload == {"fire_at": reminder.fire_at.isoformat(), "delivered": True}
    assert await store.verify_chain(DEV_ADMIN_ID, project.id) == []

    fake.advance(minutes=5)  # nothing fires twice
    assert (await tick.run_once()).fired == 0
    assert len(await items(client, DEV_ADMIN_ID)) == 1
    assert (await kinds(store, project.id)).count("reminder.fired") == 1


async def test_two_ticks_at_once_deliver_a_reminder_once(
    client, store: ProjectStore, tick: ReminderTick, fake: Clock
) -> None:
    project, node = await a_task(store)
    await store.add_reminder(DEV_ADMIN_ID, node.id, at=T0 + timedelta(minutes=1))
    fake.advance(minutes=2)
    other = ReminderTick(client.app.state.sessionmaker, client.app.state.settings)
    results = await asyncio.gather(tick.run_once(), other.run_once())
    assert sum(r.fired for r in results) == 1
    assert len(await items(client, DEV_ADMIN_ID)) == 1
    assert (await kinds(store, project.id)).count("reminder.fired") == 1


async def test_after_downtime_the_recent_is_delivered_and_the_old_is_closed_with_a_reason(
    client, store: ProjectStore, tick: ReminderTick, fake: Clock
) -> None:
    project, node = await a_task(store)
    recent = await store.add_reminder(DEV_ADMIN_ID, node.id, at=T0 + timedelta(hours=40))
    old = await store.add_reminder(DEV_ADMIN_ID, node.id, at=T0 + timedelta(hours=1))
    fake.now = T0 + timedelta(hours=50)  # the hub was down meanwhile; the window is 48 hours

    result = await tick.run_once()
    assert (result.fired, result.expired) == (1, 1)
    assert [i.ref["reminder_id"] for i in await items(client, DEV_ADMIN_ID)] == [str(recent.id)]
    # the item opens the page in the project, on the task (what the inbox hands to the module)
    (item,) = await items(client, DEV_ADMIN_ID)
    assert item.ref["module"] == "page.project"
    assert item.ref["props"] == {"project": str(project.id), "node": str(node.id)}
    fired = [
        e
        for e in await store.timeline(DEV_ADMIN_ID, project.id, limit=200)
        if e.kind == "reminder.fired"
    ]
    by_delivery = {e.payload["delivered"]: e.payload for e in fired}
    assert by_delivery[False]["reason"] == "too_old"
    assert by_delivery[False]["fire_at"] == old.at.isoformat()
    assert (await tick.run_once()).expired == 0  # closed for good


async def test_the_window_comes_from_the_setting(client, store: ProjectStore, fake: Clock) -> None:
    settings = client.app.state.settings.model_copy(update={"project_reminder_catchup_hours": 1})
    short = ReminderTick(client.app.state.sessionmaker, settings)
    _, node = await a_task(store)
    await store.add_reminder(DEV_ADMIN_ID, node.id, at=T0 + timedelta(minutes=1))
    fake.now = T0 + timedelta(hours=3)
    assert (await short.run_once()).expired == 1
    assert not await items(client, DEV_ADMIN_ID)


async def test_a_reminder_of_finished_or_trashed_work_is_closed_without_a_message(
    client, store: ProjectStore, tick: ReminderTick, fake: Clock
) -> None:
    project, done = await a_task(store)
    gone = await store.create_node(DEV_ADMIN_ID, project.id, kind="task", title="gone")
    for n in (done, gone):
        await store.add_reminder(DEV_ADMIN_ID, n.id, at=T0 + timedelta(minutes=1))
    await store.update_node(DEV_ADMIN_ID, done.id, revision=done.revision, status="done")
    await store.trash_node(DEV_ADMIN_ID, gone.id)
    fake.advance(minutes=5)
    result = await tick.run_once()
    assert (result.fired, result.skipped) == (0, 2)
    assert not await items(client, DEV_ADMIN_ID)
    last = (await store.timeline(DEV_ADMIN_ID, project.id, limit=200))[-1]
    assert last.kind == "reminder.fired" and last.payload["reason"] == "not_open"


async def test_a_reminder_fires_again_when_the_date_moves_and_is_a_new_item(
    client, store: ProjectStore, tick: ReminderTick, fake: Clock
) -> None:
    _, node = await a_task(store, due_at=T0 + timedelta(hours=1))
    await store.add_reminder(DEV_ADMIN_ID, node.id, relative_to="due", offset_seconds=0)
    fake.advance(hours=2)
    assert (await tick.run_once()).fired == 1
    node = await store.get_node(DEV_ADMIN_ID, node.id)
    await store.update_node(
        DEV_ADMIN_ID, node.id, revision=node.revision, fields={"due_at": T0 + timedelta(hours=5)}
    )
    fake.advance(hours=4)
    assert (await tick.run_once()).fired == 1
    assert len(await items(client, DEV_ADMIN_ID, KIND_REMINDER)) == 2


# --- people -------------------------------------------------------------------------------------


async def test_a_reminder_goes_to_its_own_person_only(
    client, store: ProjectStore, tick: ReminderTick, fake: Clock
) -> None:
    other, _ = make_user(TEST_DB_URL, f"second-{uuid.uuid4().hex[:6]}")
    _, mine = await a_task(store)
    _, theirs = await a_task(store, other)
    await store.add_reminder(DEV_ADMIN_ID, mine.id, at=T0 + timedelta(minutes=1))
    await store.add_reminder(other, theirs.id, at=T0 + timedelta(minutes=1))
    fake.advance(minutes=5)
    assert (await tick.run_once()).fired == 2
    assert [i.ref["node_id"] for i in await items(client, DEV_ADMIN_ID)] == [str(mine.id)]
    assert [i.ref["node_id"] for i in await items(client, other)] == [str(theirs.id)]


async def test_quiet_hours_hold_a_reminder_back_in_the_zone_of_the_person(
    client, store: ProjectStore, tick: ReminderTick, fake: Clock
) -> None:
    user, _ = make_user(TEST_DB_URL, f"quiet-{uuid.uuid4().hex[:6]}")
    sm = client.app.state.sessionmaker
    async with sm() as db:
        await db.execute(
            text("UPDATE users SET timezone = 'Europe/Berlin' WHERE id = :u"), {"u": user}
        )
        await db.commit()
    await write_prefs(sm, user, quiet_start="22:00", quiet_end="07:00", digest_hour=None)
    _, node = await a_task(store, user)
    # 21:30 UTC is 22:30 in Berlin on this date (winter time): quiet, though not in UTC
    await store.add_reminder(user, node.id, at=datetime(2026, 10, 29, 21, 30, tzinfo=UTC))

    fake.now = datetime(2026, 10, 29, 21, 45, tzinfo=UTC)
    result = await tick.run_once()
    assert (result.fired, result.deferred) == (0, 1) and not await items(client, user)
    fake.now = datetime(2026, 10, 30, 5, 59, tzinfo=UTC)  # 06:59 in Berlin
    assert (await tick.run_once()).deferred == 1
    fake.now = datetime(2026, 10, 30, 6, 0, tzinfo=UTC)  # 07:00 in Berlin: the span has ended
    assert (await tick.run_once()).fired == 1
    assert len(await items(client, user)) == 1


async def test_quiet_hours_do_not_keep_a_reminder_that_is_too_old_anyway(
    client, store: ProjectStore, tick: ReminderTick, fake: Clock
) -> None:
    sm = client.app.state.sessionmaker
    await write_prefs(sm, DEV_ADMIN_ID, quiet_start="00:00", quiet_end="23:59", digest_hour=None)
    _, node = await a_task(store)
    await store.add_reminder(DEV_ADMIN_ID, node.id, at=T0 - timedelta(days=5))
    result = await tick.run_once()
    assert (result.expired, result.deferred) == (1, 0)


async def test_the_prefs_are_validated_and_stored(client) -> None:  # type: ignore[no-untyped-def]
    sm = client.app.state.sessionmaker
    stored = await write_prefs(
        sm, DEV_ADMIN_ID, quiet_start="22:00", quiet_end="07:30", digest_hour=8
    )
    assert stored == {"quiet_start": "22:00", "quiet_end": "07:30", "digest_hour": 8}
    for bad in (
        {"quiet_start": "22:00", "quiet_end": None, "digest_hour": None},  # half a span
        {"quiet_start": "25:00", "quiet_end": "07:00", "digest_hour": None},
        {"quiet_start": None, "quiet_end": None, "digest_hour": 24},
    ):
        with pytest.raises(ValueError):
            await write_prefs(sm, DEV_ADMIN_ID, **bad)
    cleared = await write_prefs(
        sm, DEV_ADMIN_ID, quiet_start=None, quiet_end=None, digest_hour=None
    )
    assert cleared == {"quiet_start": None, "quiet_end": None, "digest_hour": None}


# --- the digest ---------------------------------------------------------------------------------


async def test_the_digest_is_one_item_a_day_and_only_when_something_is_overdue(
    client, store: ProjectStore, tick: ReminderTick, fake: Clock
) -> None:
    sm = client.app.state.sessionmaker
    await write_prefs(sm, DEV_ADMIN_ID, quiet_start=None, quiet_end=None, digest_hour=8)
    project, late = await a_task(store, due_at=T0 - timedelta(days=2))
    later = await store.create_node(
        DEV_ADMIN_ID,
        project.id,
        kind="task",
        title="later",
        fields={"due_at": T0 - timedelta(hours=1)},
    )
    await store.create_node(
        DEV_ADMIN_ID,
        project.id,
        kind="task",
        title="not yet",
        fields={"due_at": T0 + timedelta(days=1)},
    )
    finished = await store.create_node(
        DEV_ADMIN_ID,
        project.id,
        kind="task",
        title="finished",
        fields={"due_at": T0 - timedelta(days=9)},
    )
    await store.update_node(DEV_ADMIN_ID, finished.id, revision=finished.revision, status="done")

    fake.now = datetime(2026, 10, 29, 7, 59, tzinfo=UTC)  # before the hour
    assert (await tick.run_once()).digests == 0
    fake.now = datetime(2026, 10, 29, 8, 5, tzinfo=UTC)
    assert (await tick.run_once()).digests == 1
    [item] = await items(client, DEV_ADMIN_ID, KIND_DIGEST)
    assert item.title == "2 overdue tasks" and not item.urgent
    assert item.summary.splitlines()[0].startswith(
        f"{project.title} {late.number}: Pay the invoice"
    )
    assert f"{later.title}" in item.summary and "finished" not in item.summary
    assert item.source_ref == "2026-10-29"

    fake.advance(minutes=1)
    assert (await tick.run_once()).digests == 0  # today's is made
    fake.now = datetime(2026, 10, 30, 8, 5, tzinfo=UTC)
    assert (await tick.run_once()).digests == 1  # and tomorrow there is another
    assert len(await items(client, DEV_ADMIN_ID, KIND_DIGEST)) == 2


async def test_no_digest_without_overdue_work_without_an_hour_or_inside_quiet_hours(
    client, store: ProjectStore, tick: ReminderTick, fake: Clock
) -> None:
    sm = client.app.state.sessionmaker
    await a_task(store, due_at=T0 - timedelta(days=1))
    fake.now = datetime(2026, 10, 29, 8, 30, tzinfo=UTC)
    assert (await tick.run_once()).digests == 0  # no hour chosen: no digest
    await write_prefs(sm, DEV_ADMIN_ID, quiet_start="08:00", quiet_end="09:00", digest_hour=8)
    assert (await tick.run_once()).digests == 0  # quiet
    fake.now = datetime(2026, 10, 29, 9, 0, tzinfo=UTC)
    assert (await tick.run_once()).digests == 1  # the span has ended
    await write_prefs(sm, DEV_ADMIN_ID, quiet_start=None, quiet_end=None, digest_hour=8)
    fake.now = datetime(2026, 11, 2, 9, 0, tzinfo=UTC)  # a new day, but a task that is not overdue
    async with sm() as db:
        await db.execute(
            text("UPDATE project_nodes SET due_at = :d"), {"d": fake.now + timedelta(days=3)}
        )
        await db.commit()
    assert (await tick.run_once()).digests == 0


# --- completing ---------------------------------------------------------------------------------


async def test_completing_a_plain_task_is_the_ordinary_done(
    store: ProjectStore, fake: Clock
) -> None:
    project, node = await a_task(store)
    done = await store.complete_node(DEV_ADMIN_ID, node.id, revision=node.revision)
    assert done.status == "done" and done.done_at == fake.now
    assert (await kinds(store, project.id))[-1] == "node.completed"
    with pytest.raises(Invalid):
        await store.complete_node(DEV_ADMIN_ID, node.id, revision=done.revision)  # done already
    topic = await store.create_node(DEV_ADMIN_ID, project.id, kind="topic", title="t")
    with pytest.raises(Invalid):
        await store.complete_node(DEV_ADMIN_ID, topic.id, revision=topic.revision)
    _, other = await a_task(store)
    with pytest.raises(Conflict):
        await store.complete_node(DEV_ADMIN_ID, other.id, revision=other.revision + 1)
    dropped = await store.create_node(DEV_ADMIN_ID, project.id, kind="task", title="d")
    await store.update_node(DEV_ADMIN_ID, dropped.id, revision=dropped.revision, status="dropped")
    with pytest.raises(Invalid):
        dropped = await store.get_node(DEV_ADMIN_ID, dropped.id)
        await store.complete_node(DEV_ADMIN_ID, dropped.id, revision=dropped.revision)


async def test_completing_a_task_of_another_person_is_not_found(
    store: ProjectStore, fake: Clock
) -> None:
    other, _ = make_user(TEST_DB_URL, f"third-{uuid.uuid4().hex[:6]}")
    _, node = await a_task(store, other)
    with pytest.raises(NotFound):
        await store.complete_node(DEV_ADMIN_ID, node.id, revision=node.revision)


async def test_a_repeating_task_comes_back_with_everything_moved(
    store: ProjectStore, fake: Clock
) -> None:
    due = T0 + timedelta(hours=2)
    project, node = await a_task(
        store,
        start_at=due - timedelta(hours=1),
        due_at=due,
        end_at=due + timedelta(hours=1),
        percent_done=60,
        repeat_rule={"every": 1, "unit": "day", "mode": "after", "tz": "UTC"},
    )
    node = await store.update_node(
        DEV_ADMIN_ID,
        node.id,
        revision=node.revision,
        body="Steps\n- [x] open\n- [x] pay\n- [ ] file\n",
    )
    relative = await store.add_reminder(
        DEV_ADMIN_ID, node.id, relative_to="due", offset_seconds=-1800
    )
    absolute = await store.add_reminder(DEV_ADMIN_ID, node.id, at=T0)
    fake.now = T0 + timedelta(hours=1, minutes=30)  # the relative one has fired in between
    async with store._sessionmaker() as db:
        row = await db.get(type(relative), relative.id)
        row.fired_at = fake.now - timedelta(minutes=5)
        await db.commit()

    again = await store.complete_node(DEV_ADMIN_ID, node.id, revision=node.revision)
    assert again.status == "todo" and again.done_at is None and again.percent_done == 0
    assert again.revision == node.revision + 1
    assert (again.start_at, again.due_at, again.end_at) == (
        due - timedelta(hours=1) + timedelta(days=1),
        due + timedelta(days=1),
        due + timedelta(hours=1) + timedelta(days=1),
    )
    assert again.body == "Steps\n- [ ] open\n- [ ] pay\n- [ ] file\n"

    by_id = {r.id: r for r in await store.reminders_of(DEV_ADMIN_ID, node.id)}
    assert by_id[relative.id].fire_at == due + timedelta(days=1) - timedelta(minutes=30)
    assert by_id[relative.id].fired_at is None  # it will announce the next occurrence
    assert by_id[absolute.id].at == T0 + timedelta(days=1)  # the same distance to the due date
    assert (
        by_id[absolute.id].fire_at == T0 + timedelta(days=1) and by_id[absolute.id].fired_at is None
    )

    events = await store.timeline(DEV_ADMIN_ID, project.id, limit=200)
    tail = [e for e in events if e.group_id is not None and e.kind != "node.updated"][-3:]
    assert [e.kind for e in tail] == ["node.completed", "node.reopened", "body.changed"]
    assert len({e.group_id for e in tail}) == 1  # one change
    completed, reopened, _ = tail
    assert completed.payload["status"] == ["todo", "done"] and completed.payload["repeat"] is True
    assert completed.payload["done_at"] == fake.now.isoformat()
    assert reopened.payload["status"] == ["done", "todo"]
    assert reopened.payload["dates"]["due_at"] == [
        due.isoformat(),
        (due + timedelta(days=1)).isoformat(),
    ]
    assert reopened.payload["reminders"] == {"moved": 1, "passed": 0}
    assert reopened.payload["checklist_reset"] is True
    assert await store.verify_chain(DEV_ADMIN_ID, project.id) == []
    old = await store.node_at(DEV_ADMIN_ID, node.id, node.revision)  # the ticked list is history
    assert "[x] pay" in old["body"]


async def test_a_task_left_for_weeks_comes_back_once_not_as_a_pile(
    store: ProjectStore, fake: Clock
) -> None:
    _, node = await a_task(
        store,
        due_at=T0 - timedelta(days=40),
        repeat_rule={"every": 1, "unit": "day", "mode": "after", "tz": "UTC"},
    )
    again = await store.complete_node(DEV_ADMIN_ID, node.id, revision=node.revision)
    assert again.due_at == T0 + timedelta(days=1) and again.status == "todo"


async def test_a_repeat_from_done_counts_from_the_moment_of_completion(
    store: ProjectStore, fake: Clock
) -> None:
    _, node = await a_task(
        store,
        due_at=T0 + timedelta(days=1),
        repeat_rule={"every": 3, "unit": "day", "mode": "from_done", "tz": "UTC"},
    )
    fake.advance(hours=5)
    again = await store.complete_node(DEV_ADMIN_ID, node.id, revision=node.revision)
    assert again.due_at == fake.now + timedelta(days=3)


async def test_a_daily_task_keeps_its_wall_clock_across_the_change_to_winter_time(
    store: ProjectStore, fake: Clock
) -> None:
    due = datetime(2026, 10, 24, 7, 0, tzinfo=UTC)  # 09:00 in Berlin (summer time)
    _, node = await a_task(
        store,
        due_at=due,
        repeat_rule={"every": 1, "unit": "day", "mode": "after", "tz": "Europe/Berlin"},
    )
    fake.now = datetime(2026, 10, 24, 8, 0, tzinfo=UTC)
    again = await store.complete_node(DEV_ADMIN_ID, node.id, revision=node.revision)
    assert again.due_at == datetime(2026, 10, 25, 8, 0, tzinfo=UTC)  # still 09:00, now winter time


async def test_a_repeating_task_without_dates_only_reopens(
    store: ProjectStore, fake: Clock
) -> None:
    project, node = await a_task(
        store, repeat_rule={"every": 1, "unit": "week", "mode": "after", "tz": "UTC"}
    )
    again = await store.complete_node(DEV_ADMIN_ID, node.id, revision=node.revision)
    assert again.status == "todo" and again.due_at is None
    assert (await kinds(store, project.id))[-2:] == ["node.completed", "node.reopened"]


async def test_a_stale_revision_changes_nothing_in_a_repeating_task(
    store: ProjectStore, fake: Clock
) -> None:
    project, node = await a_task(
        store,
        due_at=T0 + timedelta(hours=1),
        repeat_rule={"every": 1, "unit": "day", "mode": "after", "tz": "UTC"},
    )
    before = await kinds(store, project.id)
    with pytest.raises(Conflict):
        await store.complete_node(DEV_ADMIN_ID, node.id, revision=node.revision + 3)
    assert await kinds(store, project.id) == before
    assert (await store.get_node(DEV_ADMIN_ID, node.id)).due_at == T0 + timedelta(hours=1)


# --- REST ---------------------------------------------------------------------------------------


async def test_the_rest_routes(client, store: ProjectStore, fake: Clock) -> None:  # type: ignore[no-untyped-def]
    _, node = await a_task(
        store,
        due_at=T0 + timedelta(hours=1),
        repeat_rule={"every": 1, "unit": "day", "mode": "after", "tz": "UTC"},
    )
    stale = client.post(
        f"/api/project/nodes/{node.id}/complete", json={"revision": node.revision + 1}, headers=AUTH
    )
    assert stale.status_code == 409
    ok = client.post(
        f"/api/project/nodes/{node.id}/complete", json={"revision": node.revision}, headers=AUTH
    )
    assert ok.status_code == 200 and ok.json()["reopened"] is True and ok.json()["status"] == "todo"
    plain = await store.create_node(
        DEV_ADMIN_ID,
        (await store.get_node(DEV_ADMIN_ID, node.id)).project_id,
        kind="task",
        title="x",
    )
    done = client.post(
        f"/api/project/nodes/{plain.id}/complete", json={"revision": plain.revision}, headers=AUTH
    )
    assert done.status_code == 200 and done.json()["reopened"] is False
    again = client.post(
        f"/api/project/nodes/{plain.id}/complete",
        json={"revision": plain.revision + 1},
        headers=AUTH,
    )
    assert again.status_code == 422  # done already
    _, headers = make_user(TEST_DB_URL, f"rest-{uuid.uuid4().hex[:6]}")
    assert (
        client.post(
            f"/api/project/nodes/{node.id}/complete", json={"revision": 1}, headers=headers
        ).status_code
        == 404
    )

    url = "/api/project/notification-settings"
    assert client.get(url, headers=AUTH).json() == {
        "quiet_start": None, "quiet_end": None, "digest_hour": None,
    }  # fmt: skip
    body = {"quiet_start": "22:00", "quiet_end": "07:00", "digest_hour": 8}
    assert client.put(url, json=body, headers=AUTH).json() == body
    assert client.get(url, headers=AUTH).json() == body
    assert client.get(url, headers=headers).json()["digest_hour"] is None  # each person's own
    for bad in (
        {"quiet_start": "22:00"},
        {"digest_hour": 30},
        {"quiet_start": "x", "quiet_end": "y"},
    ):
        assert client.put(url, json=bad, headers=AUTH).status_code == 422


# --- migration ----------------------------------------------------------------------------------


def test_0083_adds_the_columns_checks_them_and_goes_back(migrated_db: str) -> None:
    cfg = alembic_config()
    engine = create_engine(TEST_DB_URL)

    def columns() -> set[str]:
        with engine.connect() as conn:
            return {
                r[0]
                for r in conn.execute(
                    text(
                        "SELECT column_name FROM information_schema.columns"
                        " WHERE table_name = 'users'"
                    )
                )
            }

    wanted = {"project_quiet_start", "project_quiet_end", "project_digest_hour"}
    try:
        assert wanted <= columns()
        with engine.begin() as conn:
            conn.execute(
                text(
                    "UPDATE users SET project_quiet_start = 1320, project_quiet_end = 420,"
                    " project_digest_hour = 8 WHERE id = :u"
                ),
                {"u": DEV_ADMIN_ID},
            )
        for bad in (
            "project_quiet_end = NULL",  # half a span
            "project_quiet_start = 1440, project_quiet_end = 0",
            "project_digest_hour = 24",
        ):
            with pytest.raises(Exception, match="ck_users_project"), engine.begin() as conn:
                conn.execute(text(f"UPDATE users SET {bad} WHERE id = :u"), {"u": DEV_ADMIN_ID})  # noqa: S608
        command.downgrade(cfg, "0082")
        assert not wanted & columns()
        command.upgrade(cfg, "0083")
        assert wanted <= columns()
    finally:
        command.upgrade(cfg, "head")
        with engine.begin() as conn:
            conn.execute(
                text(
                    "UPDATE users SET project_quiet_start = NULL, project_quiet_end = NULL,"
                    " project_digest_hour = NULL WHERE id = :u"
                ),
                {"u": DEV_ADMIN_ID},
            )
        engine.dispose()
