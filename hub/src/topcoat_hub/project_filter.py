"""Filters on nodes (PPH-04, 04c): a small query language, parsed by hand, turned into a SQL
condition.

    status = in_review && (priority >= 3 || dueDate < now+7d) && labels in (urgent, "next up")

* **Fields** (`FIELDS`): done, status, kind, reportKind, priority, percentDone, dueDate, startDate,
  endDate, doneAt, created, updated, labels, project, number, milestone, blocked, ready, superseded,
  unanswered, trashed, reminders, childCount, openChildCount, q (free text in the title and the
  body).
* **Operators**: `=` `!=` `>` `>=` `<` `<=` `like` `in` `not in`; `&&`, `||` and parentheses.
* **Values**: a word or a "quoted string", a number, `true`/`false`, `null`, a list `(a, b)`, a
  date.
* **Dates** are ISO 8601, or `now` with modifiers: `+7d`, `-2w`, `+3b` (business days), `/d` (round
  down to the start of the day). Units: s m h d w M y, b. They are read in the zone the caller
  names.
* A node without a value for a field is not matched by a comparison on it (`dueDate < now` does not
  find tasks without a due date); `= null` asks for exactly those.

The result is a condition that the caller **always** ANDs with the scoping by person and
application; a filter can narrow what a person sees, never widen it. Text goes into the query as a
bound parameter, never as SQL. The size and the depth of a filter are limited, and every error names
the field or the operator and points to ``project.filter.help``.
"""

import re
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import ColumnElement, and_, exists, func, not_, or_, select
from sqlalchemy.orm import aliased

from topcoat_hub import clock
from topcoat_hub.models import (
    Project,
    ProjectLabel,
    ProjectNode,
    ProjectNodeLabel,
    ProjectRelation,
    ProjectReminder,
)
from topcoat_hub.project_repeat import add_months

MAX_LENGTH = 1000
MAX_DEPTH = 8
MAX_TERMS = 50
HELP_HINT = "call project.filter.help for the syntax"

FIELDS: dict[str, str] = {
    "done": "bool",
    "status": "word",
    "kind": "word",
    "reportKind": "word",
    "priority": "number",
    "percentDone": "number",
    "dueDate": "date",
    "startDate": "date",
    "endDate": "date",
    "doneAt": "date",
    "created": "date",
    "updated": "date",
    "labels": "set",
    "project": "word",
    "number": "word",
    "milestone": "bool",
    "blocked": "bool",
    "ready": "bool",
    "superseded": "bool",
    "unanswered": "bool",
    "trashed": "bool",
    "reminders": "date",
    "childCount": "number",
    "openChildCount": "number",
    "q": "text",
}
OPERATORS = ("=", "!=", ">=", "<=", ">", "<", "like", "not in", "in")
_ORDERED = (">", ">=", "<", "<=")
_COLUMNS = {
    "status": ProjectNode.status,
    "kind": ProjectNode.kind,
    "reportKind": ProjectNode.report_kind,
    "priority": ProjectNode.priority,
    "percentDone": ProjectNode.percent_done,
    "dueDate": ProjectNode.due_at,
    "startDate": ProjectNode.start_at,
    "endDate": ProjectNode.end_at,
    "doneAt": ProjectNode.done_at,
    "created": ProjectNode.created_at,
    "updated": ProjectNode.updated_at,
    "milestone": ProjectNode.is_milestone,
}
_STATUSES = ("todo", "in_progress", "in_review", "blocked", "done", "dropped")
_KINDS = ("topic", "task", "report")


class FilterError(ValueError):
    """The filter cannot be read or does not mean anything; the message says why."""

    def __init__(self, message: str) -> None:
        super().__init__(f"{message} ({HELP_HINT})")


# --- reading ------------------------------------------------------------------------------------

_TOKEN = re.compile(
    r"""\s*(?:
        (?P<op>&&|\|\||!=|>=|<=|=|>|<|\(|\)|,)
      | (?P<str>"(?:[^"\\]|\\.)*")
      | (?P<word>[^\s()&|=!<>,"]+)
    )""",
    re.X,
)


@dataclass
class Token:
    kind: str  # op | str | word
    text: str
    pos: int


def tokenize(source: str) -> list[Token]:
    if len(source) > MAX_LENGTH:
        raise FilterError(f"the filter is longer than {MAX_LENGTH} characters")
    tokens: list[Token] = []
    i = 0
    while i < len(source):
        if source[i:].strip() == "":
            break
        m = _TOKEN.match(source, i)
        if not m or m.end() == i:
            raise FilterError(f"unexpected character at position {i + 1}: {source[i : i + 1]!r}")
        kind = m.lastgroup or "word"
        text = m.group(kind)
        if kind == "str":
            text = (
                bytes(text[1:-1], "utf-8").decode("unicode_escape") if "\\" in text else text[1:-1]
            )
        tokens.append(Token(kind, text, m.start(kind)))
        i = m.end()
    return tokens


# the syntax tree: ("and"|"or", [children]) or ("cmp", field, op, value)
Node = tuple[Any, ...]


class Parser:
    def __init__(self, tokens: list[Token]) -> None:
        self.t = tokens
        self.i = 0
        self.terms = 0

    def peek(self) -> Token | None:
        return self.t[self.i] if self.i < len(self.t) else None

    def take(self) -> Token:
        tok = self.peek()
        if tok is None:
            raise FilterError("the filter ends too early")
        self.i += 1
        return tok

    def parse(self) -> Node:
        if not self.t:
            raise FilterError("the filter is empty")
        tree = self.or_expr(0)
        if self.peek() is not None:
            raise FilterError(f"unexpected {self.peek().text!r} at position {self.peek().pos + 1}")  # type: ignore[union-attr]
        return tree

    def or_expr(self, depth: int) -> Node:
        parts = [self.and_expr(depth)]
        while (tok := self.peek()) and tok.kind == "op" and tok.text == "||":
            self.take()
            parts.append(self.and_expr(depth))
        return parts[0] if len(parts) == 1 else ("or", parts)

    def and_expr(self, depth: int) -> Node:
        parts = [self.term(depth)]
        while (tok := self.peek()) and tok.kind == "op" and tok.text == "&&":
            self.take()
            parts.append(self.term(depth))
        return parts[0] if len(parts) == 1 else ("and", parts)

    def term(self, depth: int) -> Node:
        tok = self.peek()
        if tok and tok.kind == "op" and tok.text == "(":
            if depth + 1 > MAX_DEPTH:
                raise FilterError(f"the filter nests deeper than {MAX_DEPTH} levels")
            self.take()
            inner = self.or_expr(depth + 1)
            close = self.take()
            if close.text != ")" or close.kind != "op":
                raise FilterError(f"expected ) at position {close.pos + 1}")
            return inner
        return self.comparison()

    def comparison(self) -> Node:
        self.terms += 1
        if self.terms > MAX_TERMS:
            raise FilterError(f"the filter has more than {MAX_TERMS} conditions")
        field_tok = self.take()
        if field_tok.kind != "word" or field_tok.text not in FIELDS:
            raise FilterError(
                f"unknown field {field_tok.text!r} at position {field_tok.pos + 1}; "
                f"the fields are {', '.join(FIELDS)}"
            )
        op = self.operator()
        value = self.value(op)
        return ("cmp", field_tok.text, op, value)

    def operator(self) -> str:
        tok = self.take()
        text = tok.text.lower()
        if tok.kind == "word" and text == "not":
            nxt = self.take()
            if nxt.text.lower() != "in":
                raise FilterError(f"expected 'in' after 'not' at position {nxt.pos + 1}")
            return "not in"
        if text in OPERATORS:
            return text
        raise FilterError(f"unknown operator {tok.text!r} at position {tok.pos + 1}")

    def value(self, op: str) -> Any:
        tok = self.take()
        if op in ("in", "not in"):
            if tok.text != "(" or tok.kind != "op":
                raise FilterError(f"{op} takes a list like (a, b) at position {tok.pos + 1}")
            items: list[Token] = []
            while True:
                item = self.take()
                if item.kind == "op":
                    raise FilterError(f"expected a value in the list at position {item.pos + 1}")
                items.append(item)
                sep = self.take()
                if sep.text == ")" and sep.kind == "op":
                    break
                if sep.text != "," or sep.kind != "op":
                    raise FilterError(f"expected , or ) at position {sep.pos + 1}")
            if len(items) > 50:
                raise FilterError("a list has at most 50 values")
            return [(i.kind, i.text) for i in items]
        if tok.kind == "op":
            raise FilterError(f"expected a value at position {tok.pos + 1}")
        return (tok.kind, tok.text)


# --- dates --------------------------------------------------------------------------------------

_MODIFIER = re.compile(r"([+-])(\d{1,5})([smhdwMyb])|/([hdwMy])")
_UNIT_SECONDS = {"s": 1, "m": 60, "h": 3600}


def _add_business_days(day: datetime, n: int, tz: ZoneInfo) -> datetime:
    local = day.astimezone(tz)
    step = 1 if n >= 0 else -1
    left = abs(n)
    while left:
        local += timedelta(days=step)
        if local.weekday() < 5:
            left -= 1
    return local


def _floor(local: datetime, unit: str) -> datetime:
    if unit == "h":
        return local.replace(minute=0, second=0, microsecond=0)
    local = local.replace(hour=0, minute=0, second=0, microsecond=0)
    if unit == "w":
        return local - timedelta(days=local.weekday())  # weeks start on Monday
    if unit == "M":
        return local.replace(day=1)
    if unit == "y":
        return local.replace(month=1, day=1)
    return local


def parse_date(text: str, *, now: datetime, tz: ZoneInfo) -> datetime:
    """A date value: ISO 8601 (a date alone is its start in ``tz``), or ``now`` with modifiers."""
    if text.startswith("now"):
        local = now.astimezone(tz)
        rest = text[3:]
        pos = 0
        while pos < len(rest):
            m = _MODIFIER.match(rest, pos)
            if not m:
                raise FilterError(
                    f"cannot read the date {text!r}: modifiers look like +7d, -2w, /d"
                )
            pos = m.end()
            if m.group(4):
                local = _floor(local, m.group(4))
                continue
            sign = 1 if m.group(1) == "+" else -1
            n, unit = int(m.group(2)), m.group(3)
            if unit in _UNIT_SECONDS:
                local += timedelta(seconds=sign * n * _UNIT_SECONDS[unit])
            elif unit == "d":
                local = (local.replace(tzinfo=None) + timedelta(days=sign * n)).replace(tzinfo=tz)
            elif unit == "w":
                local = (local.replace(tzinfo=None) + timedelta(weeks=sign * n)).replace(tzinfo=tz)
            elif unit == "b":
                local = _add_business_days(local, sign * n, tz)
            else:
                local = add_months(local, sign * n * (12 if unit == "y" else 1), tz).astimezone(tz)
        return local
    try:
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", text):
            return datetime.fromisoformat(text).replace(tzinfo=tz)
        return clock.parse(text)
    except ValueError as exc:
        raise FilterError(
            f"cannot read the date {text!r}: use 2026-10-29, 2026-10-29T17:00:00+01:00 or now+7d"
        ) from exc


# --- turning the tree into a condition ------------------------------------------------------------


def _truthy(kind: str, text: str, field: str) -> bool:
    low = text.lower()
    if kind == "word" and low in ("true", "1", "yes"):
        return True
    if kind == "word" and low in ("false", "0", "no"):
        return False
    raise FilterError(f"{field} is true or false")


def _number(kind: str, text: str, field: str) -> int:
    try:
        return int(text)
    except ValueError as exc:
        raise FilterError(f"{field} needs a whole number, not {text!r}") from exc


def _like(column: ColumnElement[Any], pattern: str) -> ColumnElement[bool]:
    """A ``like`` where ``*`` is the wildcard and everything else is literal."""
    escaped = pattern.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return column.ilike(escaped.replace("*", "%"), escape="\\")


def _blocked_correlated() -> ColumnElement[bool]:
    blocker = aliased(ProjectNode)
    by_blocked = exists().where(
        ProjectRelation.node_id == ProjectNode.id,
        ProjectRelation.kind == "blocked_by",
        ProjectRelation.valid_to.is_(None),
        ProjectRelation.other_id == blocker.id,
        blocker.trashed_at.is_(None),
        blocker.status.notin_(("done", "dropped")),
    )
    by_precedes = exists().where(
        ProjectRelation.other_id == ProjectNode.id,
        ProjectRelation.kind == "precedes",
        ProjectRelation.valid_to.is_(None),
        ProjectRelation.node_id == blocker.id,
        blocker.trashed_at.is_(None),
        blocker.status.notin_(("done", "dropped")),
    )
    return or_(by_blocked, by_precedes)


def _child_count(open_only: bool) -> Any:
    child = aliased(ProjectNode)
    where = [child.parent_id == ProjectNode.id, child.trashed_at.is_(None)]
    if open_only:
        where.append(child.status.notin_(("done", "dropped")))
    return (
        select(func.count())
        .select_from(child)
        .where(*where)
        .correlate(ProjectNode)
        .scalar_subquery()
    )


def compile_filter(
    source: str, *, now: datetime | None = None, zone: str = clock.DEFAULT_ZONE
) -> tuple[ColumnElement[bool], bool]:
    """The SQL condition of a filter, and whether it asks for trashed nodes itself (otherwise the
    caller leaves the trashed ones out). ``now`` and ``zone`` make date math testable."""
    tree = Parser(tokenize(source)).parse()
    tz = clock.zone(zone)
    moment = now or clock.hub_now()
    mentions = {"trashed": False}
    condition = _build(tree, moment, tz, mentions)
    return condition, mentions["trashed"]


def _build(
    tree: Node, now: datetime, tz: ZoneInfo, mentions: dict[str, bool]
) -> ColumnElement[bool]:
    if tree[0] == "and":
        return and_(*[_build(t, now, tz, mentions) for t in tree[1]])
    if tree[0] == "or":
        return or_(*[_build(t, now, tz, mentions) for t in tree[1]])
    _, field, op, value = tree
    if field == "trashed":
        mentions["trashed"] = True
    return _compare(field, op, value, now, tz)


def _compare(field: str, op: str, value: Any, now: datetime, tz: ZoneInfo) -> ColumnElement[bool]:
    kind = FIELDS[field]
    many = op in ("in", "not in")
    values = value if many else [value]
    if many and kind in ("bool", "date", "text"):
        raise FilterError(f"{field} cannot be used with {op}")
    if op in _ORDERED and kind not in ("number", "date"):
        raise FilterError(f"{field} cannot be compared with {op}")
    if op == "like" and kind not in ("word", "text", "set"):
        raise FilterError(f"{field} cannot be used with like")
    if kind == "bool":
        if op not in ("=", "!="):
            raise FilterError(f"{field} is true or false: use = or !=")
        wanted = _truthy(value[0], value[1], field) == (op == "=")
        return _bool_field(field, wanted)
    if kind == "text":  # q
        if op not in ("=", "like"):
            raise FilterError("q is searched with = or like")
        needle = f"*{value[1]}*"
        return or_(_like(ProjectNode.title, needle), _like(ProjectNode.body, needle))
    if kind == "set":  # labels
        return _labels(op, values)
    if field == "project":
        return _project(op, values)
    if field == "number":
        return _number_prefix(op, values)
    if field == "reminders":
        return _reminders(op, value, now, tz)
    if field in ("childCount", "openChildCount"):
        column: Any = _child_count(field == "openChildCount")
    else:
        column = _COLUMNS[field]
    return _scalar(field, kind, column, op, values, now, tz)


def _bool_field(field: str, wanted: bool) -> ColumnElement[bool]:
    if field == "done":
        cond = ProjectNode.status == "done"
    elif field == "milestone":
        cond = ProjectNode.is_milestone.is_(True)
    elif field == "blocked":
        cond = and_(ProjectNode.kind == "task", _blocked_correlated())
    elif field == "ready":
        cond = and_(
            ProjectNode.kind == "task", ProjectNode.status == "todo", not_(_blocked_correlated())
        )
    elif field == "superseded":
        cond = ProjectNode.current.is_(False)
    elif field == "unanswered":
        cond = and_(ProjectNode.report_kind == "question", ProjectNode.answer.is_(None))
    else:  # trashed
        cond = ProjectNode.trashed_at.is_not(None)
    return cond if wanted else not_(cond)


def _scalar(
    field: str, kind: str, column: Any, op: str, values: list[Any], now: datetime, tz: ZoneInfo
) -> ColumnElement[bool]:
    def one(v: tuple[str, str]) -> Any:
        k, text = v
        if k == "word" and text.lower() == "null":
            return None
        if kind == "number":
            return _number(k, text, field)
        if kind == "date":
            return parse_date(text, now=now, tz=tz)
        if field == "status" and text not in _STATUSES:
            raise FilterError(f"status is one of {', '.join(_STATUSES)}")
        if field == "kind" and text not in _KINDS:
            raise FilterError(f"kind is one of {', '.join(_KINDS)}")
        return text

    parsed = [one(v) for v in values]
    if op == "like":
        return _like(column, str(parsed[0]))
    if op in ("in", "not in"):
        cond = column.in_([p for p in parsed if p is not None])
        return cond if op == "in" else not_(cond)  # a node without a value matches neither
    target = parsed[0]
    if target is None:
        if op == "=":
            return column.is_(None)
        if op == "!=":
            return column.is_not(None)
        raise FilterError(f"{field} cannot be compared with null using {op}")
    return {
        "=": lambda: column == target,
        "!=": lambda: column != target,
        ">": lambda: column > target,
        ">=": lambda: column >= target,
        "<": lambda: column < target,
        "<=": lambda: column <= target,
    }[op]()


def _labels(op: str, values: list[Any]) -> ColumnElement[bool]:
    titles = [v[1] for v in values]
    if op == "like":
        match = _like(ProjectLabel.title, titles[0])
    else:
        match = (
            ProjectLabel.title.in_(titles)
            if op in ("in", "not in")
            else ProjectLabel.title == titles[0]
        )
    has = exists().where(
        ProjectNodeLabel.node_id == ProjectNode.id,
        ProjectNodeLabel.label_id == ProjectLabel.id,
        match,
    )
    return not_(has) if op in ("!=", "not in") else has


def _project(op: str, values: list[Any]) -> ColumnElement[bool]:
    ids: list[uuid.UUID] = []
    names: list[str] = []
    for _, text in values:
        try:
            ids.append(uuid.UUID(text))
        except ValueError:
            names.append(text)
    if op == "like":
        match: Any = _like(Project.title, names[0] if names else values[0][1])
    else:
        match = or_(
            Project.id.in_(ids),
            Project.title.in_(names),
            Project.identifier.in_([n.upper() for n in names]),
        )
    sub = select(Project.id).where(match)
    cond = ProjectNode.project_id.in_(sub)
    return not_(cond) if op in ("!=", "not in") else cond


def _number_prefix(op: str, values: list[Any]) -> ColumnElement[bool]:
    """`number = 1.2` is the node 1.2 and everything below it."""
    if op == "like":
        return _like(ProjectNode.number, values[0][1])
    parts = []
    for _, text in values:
        if not re.fullmatch(r"\d+(\.\d+)*", text):
            raise FilterError("a number looks like 1 or 1.2.3")
        parts.append(
            or_(ProjectNode.number == text, ProjectNode.number.like(text.replace("%", "") + ".%"))
        )
    cond = or_(*parts)
    return not_(cond) if op in ("!=", "not in") else cond


def _reminders(op: str, value: tuple[str, str], now: datetime, tz: ZoneInfo) -> ColumnElement[bool]:
    if op not in (*_ORDERED, "=", "!="):
        raise FilterError(f"reminders cannot be used with {op}")
    when = parse_date(value[1], now=now, tz=tz)
    column = ProjectReminder.fire_at
    cmp: dict[str, Callable[[], ColumnElement[bool]]] = {
        "=": lambda: column == when, "!=": lambda: column != when, ">": lambda: column > when,
        ">=": lambda: column >= when, "<": lambda: column < when, "<=": lambda: column <= when,
    }  # fmt: skip
    return exists().where(
        ProjectReminder.node_id == ProjectNode.id, ProjectReminder.fired_at.is_(None), cmp[op]()
    )


def help_text() -> str:
    """What the agent reads when a filter was wrong (``project.filter.help``)."""
    return (
        "Filter syntax: field operator value, joined with && and ||, grouped with ( ).\n"
        f"Fields: {', '.join(FIELDS)}.\n"
        "Operators: = != > >= < <= like (with * as wildcard) in (a, b) not in (a, b).\n"
        'Values: a word or "a quoted string", a number, true/false, null, a list (a, b), a date.\n'
        "Dates: 2026-10-29, 2026-10-29T17:00:00+01:00, or now with modifiers: now+7d, now-2w, "
        "now+3b (business days), now/d (start of today), now/w (start of the week). "
        "Units: s m h d w M y.\n"
        f"Status is one of {', '.join(_STATUSES)}; kind is one of {', '.join(_KINDS)}.\n"
        "Examples: status = in_review; dueDate < now && done = false; "
        'labels in (urgent, "next up") && priority >= 3; ready = true; number = 1.2; '
        "q like *login*.\n"
        "A node without a value is not found by a comparison on it: use = null to ask for those."
    )


__all__ = ["FIELDS", "FilterError", "compile_filter", "help_text", "parse_date"]
