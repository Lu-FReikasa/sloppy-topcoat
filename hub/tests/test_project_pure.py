"""The pure parts of 04b: time zones and the clock, repeat arithmetic, the checks of the node
fields,
progress, loops across projects, reminder times. No database."""

import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from topcoat_hub import clock, project_repeat
from topcoat_hub.project_fields import FieldError, check_dates, normalize
from topcoat_hub.project_progress import checklist, progress, reset_checklist
from topcoat_hub.project_reminders import fire_time
from topcoat_hub.project_tree import TreeError, check_blocking

BERLIN = ZoneInfo("Europe/Berlin")


def at(y: int, m: int, d: int, h: int = 9, mi: int = 0, tz: ZoneInfo = BERLIN) -> datetime:
    return datetime(y, m, d, h, mi, tzinfo=tz)


# --- the clock ----------------------------------------------------------------------------------


def test_a_naive_time_is_refused_and_an_offset_one_is_kept() -> None:
    with pytest.raises(ValueError):
        clock.aware(datetime(2026, 10, 29, 9, 0))
    assert clock.parse("2026-10-29T09:00:00Z").utcoffset() == timedelta(0)
    assert clock.parse("2026-10-29T09:00:00+01:00").utcoffset() == timedelta(hours=1)
    with pytest.raises(ValueError):
        clock.parse("2026-10-29T09:00:00")
    with pytest.raises(ValueError):
        clock.parse("tomorrow")


def test_zones_fall_back_to_the_hub_and_then_to_utc() -> None:
    assert str(clock.zone("Europe/Berlin")) == "Europe/Berlin"
    assert str(clock.zone(None, "Europe/Berlin")) == "Europe/Berlin"
    assert str(clock.zone("Nowhere/Land", "Europe/Berlin")) == "Europe/Berlin"
    assert str(clock.zone("Nowhere/Land", "Also/Nowhere")) == "UTC"
    assert (
        clock.valid_zone("Europe/Berlin")
        and not clock.valid_zone("Mars/Base")
        and not clock.valid_zone("")
    )


def test_hub_time_and_user_time_are_one_instant_on_two_wall_clocks() -> None:
    clock.set_clock(lambda: datetime(2026, 7, 1, 12, 0, tzinfo=UTC))
    try:
        hub, berlin = clock.hub_now(), clock.user_now("Europe/Berlin")
        assert hub == berlin
        assert hub.utcoffset() == timedelta(0) and berlin.utcoffset() == timedelta(hours=2)
        assert clock.user_now(None, "Asia/Tokyo").hour == 21
    finally:
        clock.set_clock(None)
    assert clock.hub_now().tzinfo is not None


# --- repeat -------------------------------------------------------------------------------------


def test_a_rule_is_checked() -> None:
    assert project_repeat.normalize({"every": 2, "unit": "week"}) == {
        "every": 2,
        "unit": "week",
        "mode": "after",
    }
    for bad in (
        {"every": 0, "unit": "day"},
        {"every": 1001, "unit": "day"},
        {"every": 1, "unit": "fortnight"},
        {"every": 1, "unit": "day", "mode": "sometimes"},
        {"every": 1, "unit": "day", "mode": "monthly"},
        {"every": 1, "unit": "day", "tz": "Mars/Base"},
        {"every": True, "unit": "day"},
        {"every": 1, "unit": "day", "extra": 1},
        "daily",
    ):
        with pytest.raises(project_repeat.RepeatError):
            project_repeat.normalize(bad)


def test_the_31st_comes_back_as_the_last_day_and_then_as_the_31st_again() -> None:
    rule = project_repeat.normalize(
        {"every": 1, "unit": "month", "mode": "monthly", "tz": "Europe/Berlin"}
    )
    due = at(2026, 1, 31)
    rule = project_repeat.with_anchor(rule, due)
    assert rule["anchor_day"] == 31
    assert project_repeat.with_anchor(rule, at(2026, 2, 3))["anchor_day"] == 31  # kept once set
    seen = []
    now = at(2026, 1, 31, 10)
    for _ in range(4):  # each occurrence starts from the date the last one landed on
        new = project_repeat.advance(rule, {"due_at": due}, now=now)["due_at"]
        seen.append(new.astimezone(BERLIN).date().isoformat())
        due, now = new, new + timedelta(hours=1)
    assert seen == ["2026-02-28", "2026-03-31", "2026-04-30", "2026-05-31"]  # no drift to the 28th
    assert project_repeat.with_anchor({"every": 1, "unit": "day", "mode": "after"}, due) == {
        "every": 1,
        "unit": "day",
        "mode": "after",
    }


def test_a_task_left_for_ten_years_is_moved_in_one_step() -> None:
    rule = project_repeat.normalize({"every": 1, "unit": "day", "tz": "Europe/Berlin"})
    due = at(2016, 1, 1)
    now = at(2026, 10, 29, 12)
    new = project_repeat.advance(rule, {"due_at": due}, now=now)["due_at"]
    assert new > now and new - now <= timedelta(days=1)
    assert new.astimezone(BERLIN).hour == 9  # the same wall-clock time
    rule = project_repeat.normalize({"every": 1, "unit": "minute"})
    new = project_repeat.advance(rule, {"due_at": at(2016, 1, 1)}, now=now)["due_at"]
    assert now < new <= now + timedelta(minutes=1)


def test_a_huge_interval_does_not_loop_or_overflow() -> None:
    rule = project_repeat.normalize({"every": 1000, "unit": "year"})
    new = project_repeat.advance(rule, {"due_at": at(2026, 1, 1)}, now=at(2026, 6, 1))["due_at"]
    assert new.year == 3026


def test_the_dates_keep_their_distance() -> None:
    rule = project_repeat.normalize({"every": 1, "unit": "week", "tz": "Europe/Berlin"})
    dates = {"start_at": at(2026, 10, 5), "due_at": at(2026, 10, 9), "end_at": None}
    out = project_repeat.advance(rule, dates, now=at(2026, 10, 20))
    assert out["due_at"] - out["start_at"] == timedelta(days=4)
    assert out["end_at"] is None and out["due_at"] > at(2026, 10, 20)


def test_a_wall_clock_time_survives_the_change_to_summer_time() -> None:
    rule = project_repeat.normalize({"every": 1, "unit": "day", "tz": "Europe/Berlin"})
    before = at(2026, 3, 28, 9)  # CET; the clocks go forward on 29 March
    out = project_repeat.advance(rule, {"due_at": before}, now=at(2026, 3, 28, 10))["due_at"]
    assert out.astimezone(BERLIN).strftime("%Y-%m-%d %H:%M") == "2026-03-29 09:00"
    assert out - before == timedelta(hours=23)  # a day shorter by the hour that went missing
    after = project_repeat.advance(rule, {"due_at": out}, now=out + timedelta(hours=1))["due_at"]
    assert after.astimezone(BERLIN).strftime("%H:%M") == "09:00"


def test_hours_and_minutes_are_fixed_lengths_of_time() -> None:
    rule = project_repeat.normalize({"every": 24, "unit": "hour", "tz": "Europe/Berlin"})
    out = project_repeat.advance(rule, {"due_at": at(2026, 3, 28, 9)}, now=at(2026, 3, 28, 10))[
        "due_at"
    ]
    assert out - at(2026, 3, 28, 9) == timedelta(hours=24)
    assert out.astimezone(BERLIN).hour == 10  # 24 hours later on the wall clock after the change


def test_from_done_counts_from_the_moment_it_was_done() -> None:
    rule = project_repeat.normalize(
        {"every": 3, "unit": "day", "mode": "from_done", "tz": "Europe/Berlin"}
    )
    done = at(2026, 10, 29, 17)
    out = project_repeat.advance(rule, {"due_at": at(2026, 10, 1)}, now=done, done_at=done)
    assert out["due_at"] == at(2026, 11, 1, 17)


def test_a_task_without_a_date_only_reopens() -> None:
    rule = project_repeat.normalize({"every": 1, "unit": "day"})
    out = project_repeat.advance(
        rule, {"start_at": None, "due_at": None, "end_at": None}, now=at(2026, 10, 29)
    )
    assert out == {"start_at": None, "due_at": None, "end_at": None}


def test_the_number_of_skipped_intervals_is_exact_at_the_edges() -> None:
    rule = project_repeat.normalize({"every": 1, "unit": "day", "tz": "UTC"})
    tz = ZoneInfo("UTC")
    anchor = datetime(2026, 1, 1, 9, tzinfo=tz)
    assert (
        project_repeat.intervals_to_skip(rule, anchor, datetime(2026, 1, 1, 8, tzinfo=tz), tz) == 1
    )
    assert (
        project_repeat.intervals_to_skip(rule, anchor, datetime(2026, 1, 1, 9, tzinfo=tz), tz) == 1
    )
    assert (
        project_repeat.intervals_to_skip(rule, anchor, datetime(2026, 1, 2, 9, tzinfo=tz), tz) == 2
    )
    assert (
        project_repeat.intervals_to_skip(rule, anchor, datetime(2026, 1, 2, 8, 59, tzinfo=tz), tz)
        == 1
    )


# --- fields -------------------------------------------------------------------------------------


def test_the_fields_are_normalized_by_kind() -> None:
    out = normalize(
        "task",
        {
            "due_at": "2026-10-29T17:00:00+01:00",
            "priority": 3,
            "percent_done": 40,
            "color": "#ff0000",
            "estimate_minutes": 90,
            "is_milestone": True,
            "repeat_rule": {"every": 1, "unit": "week"},
        },
    )
    assert out["due_at"].utcoffset() == timedelta(hours=1) and out["priority"] == 3
    assert out["repeat_rule"]["mode"] == "after"
    assert (
        normalize("report", {"as_of": "2026-10-01T00:00:00Z", "origin_app": "research"})[
            "origin_app"
        ]
        == "research"
    )
    assert normalize("topic", {"favorite": True}) == {"favorite": True}
    assert normalize("task", {"due_at": None}) == {"due_at": None}


@pytest.mark.parametrize(
    ("kind", "fields"),
    [
        ("task", {"priority": 6}),
        ("task", {"priority": True}),
        ("task", {"percent_done": 101}),
        ("task", {"due_at": "2026-10-29T17:00:00"}),
        ("task", {"due_at": "soon"}),
        ("task", {"color": "javascript:alert(1)"}),
        ("task", {"estimate_minutes": -1}),
        ("task", {"is_milestone": "yes"}),
        ("task", {"repeat_rule": {"every": 0, "unit": "day"}}),
        ("task", {"title": "x"}),
        ("topic", {"due_at": "2026-10-29T17:00:00Z"}),
        ("report", {"priority": 1}),
    ],
)
def test_a_wrong_field_is_refused(kind: str, fields: dict) -> None:
    with pytest.raises(FieldError):
        normalize(kind, fields)


def test_the_dates_of_a_task_must_make_sense() -> None:
    a, b = at(2026, 10, 1), at(2026, 10, 2)
    check_dates(a, None, b, milestone=False)
    check_dates(None, b, None, milestone=True)
    with pytest.raises(FieldError):
        check_dates(b, None, a, milestone=False)
    with pytest.raises(FieldError):
        check_dates(a, None, None, milestone=True)


# --- progress and checklists --------------------------------------------------------------------


def node(
    parent: uuid.UUID | None = None,
    *,
    status: str | None = "todo",
    percent: int = 0,
    body: str = "",
) -> SimpleNamespace:
    return SimpleNamespace(
        id=uuid.uuid4(), parent_id=parent, status=status, percent_done=percent, body=body
    )


def test_a_checklist_counts_its_boxes() -> None:
    body = "- [x] one\n- [ ] two\n  * [X] three\nplain [ ] text\n"
    assert checklist(body) == (2, 3)
    assert reset_checklist(body).count("[ ]") == 4 and "[x]" not in reset_checklist(body)
    assert checklist("") == (0, 0)


def test_progress_comes_from_the_children_the_checklist_or_the_field() -> None:
    root = node(status=None)
    done = node(root.id, status="done")
    half = node(root.id, percent=50)
    boxes = node(root.id, body="- [x] a\n- [ ] b\n- [ ] c\n- [ ] d\n")
    dropped = node(root.id, status="dropped", percent=0)
    out = progress([root, done, half, boxes, dropped])
    assert out[done.id] == 100 and out[half.id] == 50 and out[boxes.id] == 25
    assert out[root.id] == round((100 + 50 + 25) / 3)  # the dropped one does not count
    assert progress([node(status="done", percent=0)])  # a done task is 100 whatever the field says


# --- blocking across projects -------------------------------------------------------------------


def test_a_blocking_loop_is_found_over_the_whole_graph() -> None:
    graph = {"b": {"a"}, "c": {"b"}}  # b is blocked by a, c by b
    check_blocking("d", ["c"], graph)
    with pytest.raises(TreeError):
        check_blocking("a", ["c"], graph)  # a blocked by c would close the loop a <- b <- c
    with pytest.raises(TreeError):
        check_blocking("a", ["a"], graph)


# --- reminder times -----------------------------------------------------------------------------


def test_a_reminder_fires_at_a_time_or_relative_to_a_date() -> None:
    due = at(2026, 10, 29, 17)
    task = SimpleNamespace(due_at=due, start_at=None, end_at=at(2026, 11, 2))
    assert fire_time(at(2026, 10, 1), None, None, task) == at(2026, 10, 1)
    assert fire_time(None, "due", -3600, task) == due - timedelta(hours=1)
    assert fire_time(None, "end", 86400, task) == at(2026, 11, 3)
    assert fire_time(None, "start", -60, task) is None  # no start date yet: it waits
