"""How often the nodes had to change their loaded model, and why (plan "Aufgaben statt Anwendungen",
phase P2a: measure first, change nothing).

Pure functions over the figures of the job records (``JobRecord``): no person, no content. A
*load* is a request whose model was not loaded on its node when it was served (``model_loaded``
false). A *switch* is a request whose model differs from the model the same node served before it.
Unloads are not written to the job records, so they are not counted here.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterable, Mapping
from typing import Any

# the work label of the second look (chat_review.py); it is the one the plan wants to see alone
REVIEW_WORK = "review"


def summarize(rows: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
    """Loads and switches per day and node, with the work that caused the loads.

    ``rows`` carry ``created_at`` (aware datetime), ``chosen_node``, ``chosen_model``,
    ``model_loaded``, ``work``, ``reason`` and ``waited_ms``. Rows without a node or a model are
    refused by the gateway and say nothing about the nodes; they are skipped."""
    served = sorted(
        (r for r in rows if r.get("chosen_node") and r.get("chosen_model")),
        key=lambda r: r["created_at"],
    )
    previous: dict[str, str] = {}
    cells: dict[tuple[str, str], dict[str, Any]] = {}
    totals: dict[str, Any] = {
        "requests": 0,
        "loads": 0,
        "switches": 0,
        "review_requests": 0,
        "review_loads": 0,
        "waited_seconds_on_loads": 0.0,
    }
    loads_by_work: Counter[str] = Counter()
    loads_by_reason: Counter[str] = Counter()

    for r in served:
        node = r["chosen_node"]
        model = r["chosen_model"]
        day = r["created_at"].date().isoformat()
        cell = cells.setdefault(
            (day, node),
            {
                "date": day,
                "node": node,
                "requests": 0,
                "loads": 0,
                "switches": 0,
                "review_loads": 0,
                "loads_by_work": Counter(),
            },
        )
        review = r.get("work") == REVIEW_WORK
        loaded = bool(r.get("model_loaded", True))
        switched = node in previous and previous[node] != model
        previous[node] = model

        cell["requests"] += 1
        totals["requests"] += 1
        if review:
            totals["review_requests"] += 1
        if not loaded:
            cell["loads"] += 1
            cell["loads_by_work"][r.get("work") or "-"] += 1
            totals["loads"] += 1
            totals["waited_seconds_on_loads"] += float(r.get("waited_ms") or 0) / 1000.0
            loads_by_work[r.get("work") or "-"] += 1
            loads_by_reason[r.get("reason") or "-"] += 1
            if review:
                cell["review_loads"] += 1
                totals["review_loads"] += 1
        if switched:
            cell["switches"] += 1
            totals["switches"] += 1

    totals["waited_seconds_on_loads"] = round(totals["waited_seconds_on_loads"], 1)
    totals["loads_by_work"] = dict(loads_by_work)
    totals["loads_by_reason"] = dict(loads_by_reason)
    per_cell = []
    for key in sorted(cells):
        c = cells[key]
        c["loads_by_work"] = dict(c["loads_by_work"])
        per_cell.append(c)
    return {"totals": totals, "cells": per_cell}


def review_load_share(totals: Mapping[str, Any]) -> float | None:
    """Share of the loads that the second look caused (None without loads)."""
    loads = totals.get("loads") or 0
    if not loads:
        return None
    return round(float(totals.get("review_loads") or 0) / float(loads), 2)


__all__ = ["REVIEW_WORK", "review_load_share", "summarize"]
