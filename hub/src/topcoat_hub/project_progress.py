"""How far a node is (PPH-04, 04b), computed from what is stored, never stored itself.

* A task that is ``done`` is 100 %.
* A leaf with a GFM checklist in its body (``- [ ]`` / ``- [x]``) is as far as its checklist.
* Any other leaf is its own ``percent_done``.
* A node with children is the average of its children (dropped children do not count).

On repeat the checklist of the finished occurrence is reset (:func:`reset_checklist`).
"""

import re
import uuid
from collections.abc import Iterable
from typing import Any

_BOX = re.compile(r"^(\s*[-*+]\s+)\[([ xX])\]", re.MULTILINE)


def checklist(body: str) -> tuple[int, int]:
    """(ticked, all) boxes of the checklists in a body."""
    marks = [m.group(2) for m in _BOX.finditer(body or "")]
    return sum(1 for m in marks if m in "xX"), len(marks)


def reset_checklist(body: str) -> str:
    """The body with every box unticked."""
    return _BOX.sub(r"\1[ ]", body or "")


def progress(nodes: Iterable[Any]) -> dict[uuid.UUID, int]:
    """Percent done of every node given (rows or anything with ``id``, ``parent_id``, ``status``,
    ``percent_done`` and ``body``)."""
    nodes = list(nodes)
    children: dict[uuid.UUID | None, list[Any]] = {}
    for n in nodes:
        children.setdefault(n.parent_id, []).append(n)
    out: dict[uuid.UUID, int] = {}

    def value(n: Any) -> int:
        if n.id in out:
            return out[n.id]
        if getattr(n, "status", None) == "done":
            out[n.id] = 100
            return 100
        kids = [k for k in children.get(n.id, []) if getattr(k, "status", None) != "dropped"]
        if kids:
            result = round(sum(value(k) for k in kids) / len(kids))
        else:
            ticked, total = checklist(getattr(n, "body", ""))
            result = round(100 * ticked / total) if total else int(n.percent_done or 0)
        out[n.id] = result
        return result

    for n in nodes:
        value(n)
    return out
