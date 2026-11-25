"""The project tools take the words a person uses (2026-11-10, C5 of the chat review).

A person says "Aufgabe 2.1 im Projekt Topcoat-Hub Wiki-Abgleich", and the model passes that on:
``node_id: "2.1"``, ``project_id: "Topcoat-Hub Wiki-Abgleich (Test)"`` or the first eight
characters of an id. The tools want ids. Before the input is checked, a reference that is not
an id is resolved, when exactly one project or node fits:

- a project: its id's beginning (6 hex digits or more), its title (case does not count), or a
  unique part of its title;
- a node: its number in the project ("2.1"), its id's beginning, or its exact title; within the
  call's project, or across all projects when only one node fits.

Nothing is guessed: a reference that fits nothing or several things stays as it was, and the
tool's own check names the problem.
"""

from __future__ import annotations

import re
import uuid
from typing import Any

PROJECT_KEYS = ("project_id",)
NODE_KEYS = ("node_id", "parent_id", "node", "parent", "target_id")
LIST_KEYS = ("depends_on", "node_ids", "blocked_by")
_HEX = re.compile(r"^[0-9a-f]{6,}$")
_NUMBER = re.compile(r"^\d+(?:\.\d+)*$")


def needs_lookup(tool_input: dict[str, Any]) -> bool:
    """The input holds a project or node reference that is no id."""

    def odd(value: Any) -> bool:
        return isinstance(value, str) and bool(value) and not is_uuid(value)

    def in_obj(obj: dict[str, Any]) -> bool:
        if any(odd(obj.get(k)) for k in (*PROJECT_KEYS, *NODE_KEYS)):
            return True
        return any(isinstance(obj.get(k), list) and any(odd(v) for v in obj[k]) for k in LIST_KEYS)

    ops = tool_input.get("operations")
    return in_obj(tool_input) or (
        isinstance(ops, list) and any(isinstance(op, dict) and in_obj(op) for op in ops)
    )


def is_uuid(value: Any) -> bool:
    try:
        uuid.UUID(str(value))
    except ValueError:
        return False
    return True


def pick_project(ref: str, projects: list[Any]) -> str | None:
    """The id of the one project ``ref`` names, or None."""
    low = ref.strip().casefold()
    if not low:
        return None
    hits = [p for p in projects if _HEX.match(low) and str(p.id).startswith(low)]
    hits = hits or [p for p in projects if p.title.casefold() == low]
    hits = hits or [p for p in projects if low in p.title.casefold()]
    return str(hits[0].id) if len(hits) == 1 else None


def pick_node(ref: str, nodes: list[Any]) -> str | None:
    """The id of the one node ``ref`` names, or None (``nodes`` carry id, number and title)."""
    raw = ref.strip()
    low = raw.casefold()
    if not low:
        return None
    hits = [n for n in nodes if _NUMBER.match(raw) and n.number == raw]
    hits = hits or [n for n in nodes if _HEX.match(low) and str(n.id).startswith(low)]
    hits = hits or [n for n in nodes if n.title.casefold() == low]
    return str(hits[0].id) if len(hits) == 1 else None


async def resolve(tool_input: dict[str, Any], projects: list[Any], nodes_of: Any) -> dict[str, Any]:
    """``tool_input`` with references resolved to ids. ``nodes_of(project_id)`` returns the nodes
    of one project (awaitable). Works on the top level and on ``operations`` of a change set."""
    out = dict(tool_input)
    project = out.get("project_id")
    if isinstance(project, str) and project and not is_uuid(project):
        out["project_id"] = pick_project(project, projects) or project
    scope = out.get("project_id") if is_uuid(out.get("project_id")) else None

    cache: dict[str | None, list[Any]] = {}

    async def candidates() -> list[Any]:
        if scope not in cache:
            if scope is not None:
                cache[scope] = list(await nodes_of(scope))
            else:
                every: list[Any] = []
                for p in projects:
                    every.extend(await nodes_of(str(p.id)))
                cache[scope] = every
        return cache[scope]

    async def fix(value: Any) -> Any:
        if not isinstance(value, str) or not value or is_uuid(value):
            return value
        return pick_node(value, await candidates()) or value

    async def fix_obj(obj: dict[str, Any]) -> dict[str, Any]:
        obj = dict(obj)
        for key in NODE_KEYS:
            if key in obj:
                obj[key] = await fix(obj[key])
        for key in LIST_KEYS:
            if isinstance(obj.get(key), list):
                obj[key] = [await fix(v) for v in obj[key]]
        return obj

    out = await fix_obj(out)
    if isinstance(out.get("operations"), list):
        # a node created earlier in the same set is named by its own name: never re-pointed
        created = {
            str(op.get(k))
            for op in out["operations"]
            if isinstance(op, dict)
            for k in ("name", "title")
            if op.get(k)
        }
        fixed = []
        for op in out["operations"]:
            if not isinstance(op, dict):
                fixed.append(op)
                continue
            keep = {k: op[k] for k in (*NODE_KEYS, *LIST_KEYS) if k in op}
            new = await fix_obj(op)
            for key, value in keep.items():
                if isinstance(value, str) and value in created:
                    new[key] = value
                elif isinstance(value, list):
                    new[key] = [
                        v if isinstance(v, str) and v in created else n
                        for v, n in zip(value, new[key], strict=False)
                    ]
            fixed.append(new)
        out["operations"] = fixed
    return out
