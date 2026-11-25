"""Sorting nodes (PPH-04, 04c; decision 9 of PPH-04 section 1): there are no stored positions, only
rules. A rule is a list of keys, each ascending or descending, applied in order::

    dueDate:asc,priority:desc,number

Keys: number (1.10 after 1.9), status (in the order of the workflow), done (``done:asc`` = open
first, ``done:desc`` = done first), childCount, openChildCount, dueDate, startDate, endDate,
priority, title, created, updated, percent. A node without a date comes last, whichever way the date
is sorted.

In a tree or a list the rule applies **within siblings**: the nesting stays and every level is
sorted; in a table, a board or a calendar it applies to all nodes together. The sort is stable, so
equal nodes keep the order of the keys after them and finally the order of their numbers.
"""

import uuid
from collections.abc import Callable, Iterable, Mapping
from typing import Any

from topcoat_hub.project_base import number_key

KEYS = (
    "number", "status", "done", "childCount", "openChildCount", "dueDate", "startDate", "endDate",
    "priority", "title", "created", "updated", "percent",
)  # fmt: skip
STATUS_ORDER = ("todo", "in_progress", "in_review", "blocked", "done", "dropped")
DEFAULT: list[tuple[str, bool]] = [("number", False)]
MAX_KEYS = 6


class SortError(ValueError):
    pass


def parse_sort(spec: str | Iterable[str] | None) -> list[tuple[str, bool]]:
    """``"dueDate:asc,priority:desc"`` as [(key, descending)]; empty = by number."""
    if spec is None or spec == "" or spec == []:
        return list(DEFAULT)
    parts = [p.strip() for p in (spec.split(",") if isinstance(spec, str) else spec) if p.strip()]
    if len(parts) > MAX_KEYS:
        raise SortError(f"a sort has at most {MAX_KEYS} keys")
    out = []
    for part in parts:
        key, _, way = part.partition(":")
        if key not in KEYS:
            raise SortError(f"unknown sort key {key!r}; the keys are {', '.join(KEYS)}")
        if way not in ("", "asc", "desc"):
            raise SortError(f"a sort way is asc or desc, not {way!r}")
        out.append((key, way == "desc"))
    return out


def _value(key: str, counts: Mapping[uuid.UUID, tuple[int, int]]) -> Callable[[Any], Any]:
    def by(n: Any) -> Any:
        if key == "number":
            return number_key(n.number)
        if key == "status":
            return STATUS_ORDER.index(n.status) if n.status in STATUS_ORDER else None
        if key == "done":
            return n.status == "done"
        if key == "childCount":
            return counts.get(n.id, (0, 0))[0]
        if key == "openChildCount":
            return counts.get(n.id, (0, 0))[1]
        if key == "dueDate":
            return n.due_at
        if key == "startDate":
            return n.start_at
        if key == "endDate":
            return n.end_at
        if key == "priority":
            return n.priority
        if key == "title":
            return (n.title or "").casefold()
        if key == "created":
            return getattr(n, "created_at", None)
        if key == "updated":
            return n.updated_at
        return n.percent_done  # percent

    return by


def child_counts(nodes: Iterable[Any]) -> dict[uuid.UUID, tuple[int, int]]:
    """For every node that has children: (how many, how many of them are still open)."""
    out: dict[uuid.UUID, list[int]] = {}
    for n in nodes:
        if n.parent_id is None or getattr(n, "trashed_at", None) is not None:
            continue
        c = out.setdefault(n.parent_id, [0, 0])
        c[0] += 1
        if n.status not in ("done", "dropped"):
            c[1] += 1
    return {k: (v[0], v[1]) for k, v in out.items()}


def sort_nodes(
    nodes: Iterable[Any],
    keys: list[tuple[str, bool]] | None = None,
    *,
    counts: Mapping[uuid.UUID, tuple[int, int]] | None = None,
    siblings: bool = False,
) -> list[Any]:
    """The nodes in the order of the rule; with ``siblings`` as a tree (every level sorted)."""
    rows = list(nodes)
    rule = keys or list(DEFAULT)
    counts = counts if counts is not None else child_counts(rows)

    def ordered(group: list[Any]) -> list[Any]:
        group = sorted(group, key=lambda n: number_key(n.number))  # the last tie-break
        for key, desc in reversed(rule):
            get = _value(key, counts)
            have = [n for n in group if get(n) is not None]
            none = [n for n in group if get(n) is None]
            have.sort(key=get, reverse=desc)  # sorted() is stable also with reverse=True
            group = have + none
        return group

    if not siblings:
        return ordered(rows)
    by_parent: dict[uuid.UUID | None, list[Any]] = {}
    ids = {n.id for n in rows}
    for n in rows:
        parent = n.parent_id if n.parent_id in ids else None
        by_parent.setdefault(parent, []).append(n)
    out: list[Any] = []

    def walk(parent: uuid.UUID | None) -> None:
        for n in ordered(by_parent.get(parent, [])):
            out.append(n)
            walk(n.id)

    walk(None)
    return out
