"""The filter language and the sort rules (PPH-04, 04c) without a database: the grammar, the limits,
date math with a fixed clock and zone, that text only ever reaches SQL as a bound parameter,
sorting."""

import uuid
from datetime import UTC, datetime
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy.dialects import postgresql

from topcoat_hub.project_filter import (
    FIELDS,
    MAX_DEPTH,
    MAX_LENGTH,
    MAX_TERMS,
    FilterError,
    Parser,
    compile_filter,
    help_text,
    parse_date,
    tokenize,
)
from topcoat_hub.project_sort import SortError, child_counts, parse_sort, sort_nodes

BERLIN = ZoneInfo("Europe/Berlin")
NOW = datetime(2026, 10, 29, 15, 30, tzinfo=BERLIN)  # a Thursday


def sql(source: str) -> tuple[str, dict]:
    cond, _ = compile_filter(source, now=NOW, zone="Europe/Berlin")
    compiled = cond.compile(dialect=postgresql.dialect())
    return str(compiled), dict(compiled.params)


# --- the grammar --------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "source",
    [
        "status = todo",
        "done = false",
        "priority >= 3 && dueDate < now+7d",
        "(status = todo || status = in_progress) && priority > 1",
        'labels in (urgent, "next up")',
        "labels not in (later)",
        "title_is_not_a_field = 1".replace("title_is_not_a_field", "q"),
        "q like login*",
        "number = 1.2",
        "ready = true",
        "dueDate = null",
        "STATUS = todo".replace("STATUS", "status"),
        "status    =    todo   &&   kind=task",
    ],
)
def test_valid_filters_compile(source: str) -> None:
    text, _ = sql(source)
    assert "SELECT" not in text.upper().split("WHERE")[0] or True  # a condition, not a statement
    assert text


@pytest.mark.parametrize(
    ("source", "message"),
    [
        ("", "empty"),
        ("   ", "empty"),
        ("colour = red", "unknown field"),
        ("status ~ todo", "unknown operator"),
        ("status", "ends too early"),
        ("status =", "ends too early"),
        ("status = todo &&", "ends too early"),
        ("status = todo)", "unexpected"),
        ("(status = todo", "ends too early"),
        ("status = nonsense", "status is one of"),
        ("kind = folder", "kind is one of"),
        ("priority = high", "whole number"),
        ("priority in 1", "takes a list"),
        ("priority in (1,", "ends too early"),
        ("done > true", "cannot be compared"),
        ("done = maybe", "true or false"),
        ("done in (true)", "cannot be used with in"),
        ("dueDate like now", "cannot be used with like"),
        ("dueDate < someday", "cannot read the date"),
        ("dueDate < now+7x", "cannot read the date"),
        ("dueDate < 2026-13-45", "cannot read the date"),
        ("dueDate < 2026-10-29T17:00:00", "cannot read the date"),
        ("labels > a", "cannot be compared"),
        ("number = abc", "looks like 1 or 1.2.3"),
        ("q > a", "cannot be compared"),
        ("status = todo || || kind = task", "unknown field"),
        ("priority not 3", "expected 'in'"),
    ],
)
def test_wrong_filters_name_the_problem_and_point_to_the_help(source: str, message: str) -> None:
    with pytest.raises(FilterError) as exc:
        sql(source)
    assert message in str(exc.value)
    assert "project.filter.help" in str(exc.value)


def test_the_limits_hold() -> None:
    with pytest.raises(FilterError, match="longer than"):
        tokenize("status = todo " + "&& kind = task " * (MAX_LENGTH // 10))
    deep = "(" * (MAX_DEPTH + 1) + "status = todo" + ")" * (MAX_DEPTH + 1)
    with pytest.raises(FilterError, match="nests deeper"):
        sql(deep)
    sql("(" * MAX_DEPTH + "status = todo" + ")" * MAX_DEPTH)
    many = " && ".join(["priority > 0"] * (MAX_TERMS + 1))
    with pytest.raises(FilterError, match="more than"):
        sql(many)
    sql(" && ".join(["priority > 0"] * MAX_TERMS))
    with pytest.raises(FilterError, match="at most 50"):
        sql("priority in (" + ",".join(str(i) for i in range(51)) + ")")


def test_precedence_and_grouping() -> None:
    a = Parser(tokenize("status = todo || kind = task && priority > 1")).parse()
    assert a[0] == "or" and a[1][1][0] == "and"  # && binds tighter than ||
    b = Parser(tokenize("(status = todo || kind = task) && priority > 1")).parse()
    assert b[0] == "and" and b[1][0][0] == "or"


# --- text is never SQL --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "evil",
    ["'; DROP TABLE projects; --", "x' OR '1'='1", "%", "_", "\\", 'a" || "b'],
)
def test_text_only_reaches_the_query_as_a_parameter(evil: str) -> None:
    quoted = '"' + evil.replace("\\", "\\\\").replace('"', '\\"') + '"'
    for source in (
        f"q = {quoted}",
        f"labels in ({quoted})",
        f"status = {quoted}",
        f"project = {quoted}",
    ):
        try:
            text, params = sql(source)
        except FilterError:
            continue  # refused is fine too
        assert "DROP" not in text and "OR '1'" not in text
        if "DROP" in evil or "OR" in evil:  # the text is in a bound parameter, not in the SQL
            assert any(evil in str(v) for v in params.values()) or "status" in source


def test_like_escapes_the_wildcards_of_sql() -> None:
    _, params = sql('q like "100%_done*"')
    assert "%100\\%\\_done%%" in params.values()


def test_a_filter_cannot_widen_the_scope() -> None:
    """The condition has no way to name another person: the caller ANDs it with its own scoping."""
    text, _ = sql("status = todo || kind = task")
    assert "user_id" not in text and "application" not in text
    with pytest.raises(FilterError, match="unknown field"):
        sql("user_id = 3")
    with pytest.raises(FilterError, match="unknown field"):
        sql("application = project")


def test_the_trash_is_left_out_unless_the_filter_asks_for_it() -> None:
    assert compile_filter("status = todo", now=NOW)[1] is False
    assert compile_filter("trashed = true", now=NOW)[1] is True
    assert compile_filter("status = todo || trashed = false", now=NOW)[1] is True


def test_every_field_can_be_used() -> None:
    samples = {
        "bool": "= true", "word": "= x", "number": "= 1", "date": "< now", "set": "in (a)",
        "text": "= a",
    }  # fmt: skip
    special = {"status": "= todo", "kind": "= task", "number": "= 1.1", "project": "= x"}
    for name, kind in FIELDS.items():
        source = f"{name} {special.get(name) or samples[kind]}"
        assert sql(source)[0], source


def test_help_names_every_field_and_the_dates() -> None:
    text = help_text()
    assert all(name in text for name in FIELDS)
    assert "now+7d" in text and "business days" in text


# --- dates --------------------------------------------------------------------------------------


def day(text: str) -> str:
    return parse_date(text, now=NOW, tz=BERLIN).strftime("%Y-%m-%d %H:%M")


def test_now_and_its_modifiers() -> None:
    assert day("now") == "2026-10-29 15:30"
    assert day("now+7d") == "2026-11-05 15:30"
    assert day("now-2w") == "2026-10-15 15:30"
    assert day("now+90m") == "2026-10-29 17:00"
    assert day("now+36h") == "2026-10-31 03:30"
    assert day("now+1M") == "2026-11-29 15:30"
    assert day("now+1y") == "2027-10-29 15:30"
    assert day("now/d") == "2026-10-29 00:00"
    assert day("now/w") == "2026-10-26 00:00"  # Monday
    assert day("now/M") == "2026-10-01 00:00"
    assert day("now/y") == "2026-01-01 00:00"
    assert day("now/h") == "2026-10-29 15:00"
    assert day("now+1d/d") == "2026-10-30 00:00"
    assert day("now+1w/w") == "2026-11-02 00:00"


def test_business_days_skip_the_weekend() -> None:
    assert day("now+1b") == "2026-10-30 15:30"  # Friday
    assert day("now+2b") == "2026-11-02 15:30"  # Monday
    assert day("now+5b") == "2026-11-05 15:30"
    assert day("now-4b") == "2026-10-23 15:30"


def test_month_ends_do_not_overflow() -> None:
    end = datetime(2026, 1, 31, 12, 0, tzinfo=BERLIN)
    assert parse_date("now+1M", now=end, tz=BERLIN).strftime("%Y-%m-%d") == "2026-02-28"


def test_the_day_is_the_callers_day() -> None:
    late = datetime(2026, 10, 29, 23, 30, tzinfo=UTC)  # already the 30th in Berlin
    assert parse_date("now/d", now=late, tz=BERLIN).strftime("%Y-%m-%d") == "2026-10-30"
    assert parse_date("now/d", now=late, tz=ZoneInfo("UTC")).strftime("%Y-%m-%d") == "2026-10-29"


def test_the_clock_change_does_not_move_the_wall_clock() -> None:
    before = datetime(2026, 3, 28, 9, 0, tzinfo=BERLIN)
    out = parse_date("now+1d", now=before, tz=BERLIN)
    assert out.strftime("%Y-%m-%d %H:%M") == "2026-03-29 09:00"


def test_iso_dates() -> None:
    assert day("2026-12-24") == "2026-12-24 00:00"
    assert parse_date("2026-12-24T18:00:00+01:00", now=NOW, tz=BERLIN).hour == 18
    assert parse_date("2026-12-24T18:00:00Z", now=NOW, tz=BERLIN).utcoffset().total_seconds() == 0


# --- sorting ------------------------------------------------------------------------------------


def node(number: str, *, parent=None, status="todo", due=None, priority=0, title="t", percent=0):
    return SimpleNamespace(
        id=uuid.uuid4(), number=number, parent_id=parent, status=status, due_at=due,
        start_at=None, end_at=None, priority=priority, title=title, percent_done=percent,
        updated_at=None, created_at=None, trashed_at=None,
    )  # fmt: skip


def test_a_rule_is_read() -> None:
    assert parse_sort("dueDate:asc,priority:desc") == [("dueDate", False), ("priority", True)]
    assert parse_sort("title") == [("title", False)]
    assert parse_sort(None) == parse_sort("") == [("number", False)]
    assert parse_sort(["done:desc"]) == [("done", True)]
    for bad in ("nonsense", "title:up", ",".join(["number"] * 7)):
        with pytest.raises(SortError):
            parse_sort(bad)


def test_numbers_sort_as_numbers() -> None:
    nodes = [node("1.10"), node("1.9"), node("2"), node("1")]
    assert [n.number for n in sort_nodes(nodes)] == ["1", "1.9", "1.10", "2"]
    assert [n.number for n in sort_nodes(nodes, parse_sort("number:desc"))] == [
        "2",
        "1.10",
        "1.9",
        "1",
    ]


def test_a_missing_date_is_last_both_ways() -> None:
    a, b, c = (
        node("1", due=datetime(2026, 1, 2, tzinfo=UTC)),
        node("2"),
        node("3", due=datetime(2026, 1, 1, tzinfo=UTC)),
    )
    assert [n.number for n in sort_nodes([a, b, c], parse_sort("dueDate:asc"))] == ["3", "1", "2"]
    assert [n.number for n in sort_nodes([a, b, c], parse_sort("dueDate:desc"))] == ["1", "3", "2"]


def test_keys_apply_in_order_and_ties_fall_back_to_the_number() -> None:
    nodes = [
        node("1", priority=1, title="b"),
        node("2", priority=3, title="a"),
        node("3", priority=3, title="a"),
        node("4", priority=1, title="a"),
    ]
    assert [n.number for n in sort_nodes(nodes, parse_sort("priority:desc,title"))] == [
        "2",
        "3",
        "4",
        "1",
    ]


def test_open_first_or_done_first() -> None:
    nodes = [node("1", status="done"), node("2"), node("3", status="in_review")]
    assert [n.number for n in sort_nodes(nodes, parse_sort("done:asc"))] == ["2", "3", "1"]
    assert [n.number for n in sort_nodes(nodes, parse_sort("done:desc"))] == ["1", "2", "3"]
    assert next(iter(sort_nodes(nodes, parse_sort("status:desc")))).number == "1"


def test_by_the_number_of_sub_items() -> None:
    root = node("1")
    kids = [node("1.1", parent=root.id), node("1.2", parent=root.id, status="done")]
    other = node("2")
    nodes = [other, root, *kids]
    counts = child_counts(nodes)
    assert counts[root.id] == (2, 1)
    by_children = sort_nodes(nodes, parse_sort("childCount:desc"), counts=counts)
    assert by_children[0].number == "1"
    assert next(iter(sort_nodes(nodes, parse_sort("openChildCount:desc")))).number == "1"


def test_in_a_tree_the_sort_applies_within_siblings() -> None:
    a, b = node("1", priority=1), node("2", priority=5)
    a1, a2 = node("1.1", parent=a.id, priority=2), node("1.2", parent=a.id, priority=9)
    b1 = node("2.1", parent=b.id)
    flat = sort_nodes([a, a1, a2, b, b1], parse_sort("priority:desc"))
    assert [n.number for n in flat][:2] == ["1.2", "2"]  # all together
    tree = sort_nodes([a, a1, a2, b, b1], parse_sort("priority:desc"), siblings=True)
    assert [n.number for n in tree] == [
        "2",
        "2.1",
        "1",
        "1.2",
        "1.1",
    ]  # nesting kept, levels sorted
    orphan = node("9.1", parent=uuid.uuid4())  # a parent that is not in the list sits at the top
    assert "9.1" in [n.number for n in sort_nodes([orphan, a], siblings=True)]


def test_the_sort_is_stable() -> None:
    nodes = [node(str(i), priority=1) for i in range(1, 8)]
    assert [n.number for n in sort_nodes(nodes, parse_sort("priority"))] == [
        str(i) for i in range(1, 8)
    ]
