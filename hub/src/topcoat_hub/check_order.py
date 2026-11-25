"""The fixed order a chat gives a server check (pruefauftrag T3.2).

The chat model fills the fields of ``look_at_my_systems``; this module turns them into an
``Order`` the run can follow, or a problem the chat answers in plain text. It only narrows: the
groups and nodes come from the scope the chat already has (``apply_scope`` decides, never this
module), and an order never asks for more than the person may use. A field that does not fit is
corrected and the correction is written down, so nothing is refused for a small mistake (a small
model must not loop on a refusal)."""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any

from topcoat_hub.node_rename import earlier_names
from topcoat_hub.tool_groups import HUB_VIEWS, KEYS, WEB, clean_scope, group_of

RESULTS = ("diagnosis", "table", "yesno")
TABLE_COLUMNS = ("Aussage", "Beleg")  # the table form of a check order (pruefauftrag T3.3)
TABLE_ROWS = 20
DEFAULT_RESULT = "diagnosis"
DEFAULT_WINDOW = "24h"
LONG_WINDOW = "14d"
_LONG_PERIOD = re.compile(
    r"letzter? zeit|immer wieder|in den letzten|letzte woche|seit (einigen )?tagen|"
    r"lately|recently|for (a few )?days|the past (week|few days)",
    re.IGNORECASE,
)
_AS_TABLE = re.compile(
    r"\b(als|in einer|in) tabelle\b|\btabellarisch|\b(as|in) a table\b", re.IGNORECASE
)
GOAL_MAX = 300
# the brief the chat model writes for the run: what the run needs from the conversation
BRIEF_MAX = 4000
# the groups every check may read: cheap, and the evidence needs them (the notes, Topcoat itself
# and its wiki - the wiki only where the application's ceiling has it: Projekte and Verwaltung)
ALWAYS = ("topcoat", "notes", "wiki")
_WINDOW = re.compile(r"^(\d{1,4})\s*([mhd])$")
_MINUTES = {"m": 1, "h": 60, "d": 1440}
_MIN_WINDOW = 15  # minutes
_MAX_WINDOW = 30 * 1440  # thirty days


@dataclass(frozen=True)
class Order:
    target: str  # a node name, or "any" for whichever node fits
    tools: tuple[str, ...]  # group keys, the most the run may use
    goal: str
    result: str
    window: str
    corrections: tuple[str, ...] = field(default=())
    # what the chat model hands the run (2026-11-14): the facts of the conversation the run needs,
    # written by the model that read the conversation, instead of a cut copy of its last messages
    brief: str = ""


@dataclass(frozen=True)
class OrderProblem:
    reason: str  # shown to the person; the chat answers from text, no run starts


def _nodes(available: Iterable[dict[str, Any]]) -> list[str]:
    """The machines the scope offers, the hub's own views excluded (they are never a target)."""
    return sorted({t["node"] for t in available if t["node"] not in HUB_VIEWS})


def _groups_of(available: Iterable[dict[str, Any]], node: str | None) -> list[str]:
    have = {group_of(t["capability"]) for t in available if node is None or t["node"] == node}
    return [k for k in KEYS if k in have and k != WEB]


def _window(raw: str, notes: list[str]) -> str:
    if not raw:
        return DEFAULT_WINDOW
    m = _WINDOW.match(raw.strip().lower())
    if not m:
        notes.append(
            f"window {raw!r} is not a duration like 15m, 24h or 14d; using {DEFAULT_WINDOW}"
        )
        return DEFAULT_WINDOW
    minutes = int(m.group(1)) * _MINUTES[m.group(2)]
    if not _MIN_WINDOW <= minutes <= _MAX_WINDOW:
        notes.append(f"window {raw!r} is outside 15m to 30d; using {DEFAULT_WINDOW}")
        return DEFAULT_WINDOW
    return f"{m.group(1)}{m.group(2)}"


FIXED_FIELDS = ("target", "tools", "goal", "result", "window")
_TOPCOAT = re.compile(r"\btopcoat\b", re.IGNORECASE)


def _plain(text: str) -> str:
    return re.sub(r"[^0-9a-z]", "", text.casefold())


def named_machine(question: str, machines: Iterable[str]) -> str | None:
    """The one machine of the scope the question names ("Node-AMD" names ``node-amd``; case,
    hyphens and spaces do not count); ``None`` when it names none or several."""
    plain = _plain(question)
    hits = [
        m
        for m in machines
        if any(  # its name now, or a name it had before it was renamed
            len(_plain(n)) >= 3 and _plain(n) in plain for n in [m, *earlier_names(m)]
        )
    ]
    return hits[0] if len(hits) == 1 else None


def about_topcoat(question: str) -> bool:
    """The question is about Topcoat itself (its settings, its names), not about a machine."""
    return bool(_TOPCOAT.search(question))


def fill_target(
    fields: dict[str, Any], available: Iterable[dict[str, Any]], question: str
) -> dict[str, Any] | None:
    """The fields with a target when the model left it out, or ``None`` when no order can be
    made (no fixed field and no machine to name: the check runs the old way). The hub names the
    machine the question names, else the only machine of the scope; a question about Topcoat
    itself gets no machine. With other fixed fields set the order keeps them and runs on ``any``
    (live 2026-11-09: in a scope of nine machines the whole order was dropped, period included)."""
    if fields.get("target"):
        return fields
    machines = _nodes(available)
    target = None
    if not about_topcoat(question):
        target = named_machine(question, machines) or (machines[0] if len(machines) == 1 else None)
    if target is None and any(fields.get(k) for k in FIXED_FIELDS):
        target = "any"
    return None if target is None else {**fields, "target": target}


def resolve(
    order: dict[str, Any], available: list[dict[str, Any]], question: str = ""
) -> Order | OrderProblem:
    """The order out of the model's fields. ``available`` is what the chat's scope allows (the
    result of ``tool_groups.apply_scope``); nothing outside it can come back in."""
    notes: list[str] = []
    nodes = _nodes(available)
    target = str(order.get("target") or "").strip()
    if not target:
        target = "any"
        notes.append("no target given; the run may use any machine in the scope")
    if target != "any" and target not in nodes:
        return OrderProblem(
            f"node {target} is not in this chat's scope (servers: {', '.join(nodes) or 'none'})"
        )
    node = None if target == "any" else target

    # groups: the model's choice, cut to what the target has; an empty or wrong choice falls back
    have = _groups_of(available, node)
    asked = order.get("tools") or []
    if isinstance(asked, str):
        asked = [asked]
    chosen: list[str] = []
    for key in asked:
        if not isinstance(key, str):
            continue
        key = key.strip()
        if key not in have:
            notes.append(f"group {key!r} is not available on {target}; dropped")
            continue
        if key not in chosen:
            chosen.append(key)
    if not chosen:
        chosen = _default_groups(available, node, have)
        if asked:
            notes.append(
                f"no usable group in the order; using {', '.join(chosen) or 'none'} for {target}"
            )
    for key in ALWAYS:
        if key in {group_of(t["capability"]) for t in available} and key not in chosen:
            chosen.append(key)
    tools = tuple(k for k in KEYS if k in chosen)

    result = str(order.get("result") or DEFAULT_RESULT).strip().lower()
    if result not in RESULTS:
        notes.append(f"result {result!r} is unknown; using {DEFAULT_RESULT}")
        result = DEFAULT_RESULT
    if not order.get("result") and _AS_TABLE.search(question):
        # "als Tabelle": the person named the form (live 2026-11-09: a check the hub started had
        # no fields, so the table form never reached the run)
        result = "table"
        notes.append("the question asks for a table")
    goal = " ".join(str(order.get("goal") or question).split())[:GOAL_MAX]
    brief = str(order.get("brief") or "").strip()[:BRIEF_MAX]
    raw_window = str(order.get("window") or "").strip()
    window = _window(raw_window, notes)
    # "in letzter Zeit" or "immer wieder" asks about days, not about the last 24 hours: a model that
    # keeps the default gets 14 days (live 2026-11-09: 24 h missed the problem's history)
    if (
        window == DEFAULT_WINDOW
        and raw_window in ("", DEFAULT_WINDOW)
        and _LONG_PERIOD.search(question)
    ):
        window = LONG_WINDOW
        notes.append(f"the question asks about a longer time; period {LONG_WINDOW}")
    return Order(
        target=target,
        tools=tools,
        goal=goal,
        result=result,
        window=window,
        corrections=tuple(notes),
        brief=brief,
    )


def _default_groups(
    available: list[dict[str, Any]], node: str | None, have: list[str]
) -> list[str]:
    """What a machine of this kind is asked for by default: a Windows machine gets the host
    group, anything else every group it has. For ``any`` the defaults of every machine are
    joined."""
    nodes = [node] if node is not None else _nodes(available)
    out: list[str] = []
    for name in nodes:
        here = _groups_of(available, name)
        caps = [t["capability"] for t in available if t["node"] == name]
        picked = (
            ["host"] if any(c.startswith("windows.") for c in caps) and "host" in here else here
        )
        for key in picked:
            if key in have and key not in out:
                out.append(key)
    return out


def scope_for(order: Order, scope: dict[str, Any] | None) -> dict[str, Any] | None:
    """The scope a run of this order gets: the chat's scope cut down to the order's machine and
    groups. It only narrows; ``apply_scope`` still decides what the run may use."""
    base = dict(scope or {})
    if order.target != "any":
        allowed = base.get("nodes")
        base["nodes"] = [order.target] if allowed is None or order.target in allowed else []
    base["groups"] = list(order.tools)
    return clean_scope(base)


def card(
    order: Order, max_calls: int | None = None, typical_calls: int | None = None
) -> dict[str, Any]:
    """The order as the activity shows it (pruefauftrag T4.1): plain data, the browser words it.
    Every check of this kind only reads (mode ``read``); ``max_calls`` is the most calls the run
    may make, so the card can count its steps (T4.2); ``typical_calls`` what earlier checks of
    the same application usually needed (check_steps.py), or None."""
    return {
        "max_calls": max_calls,
        "typical_calls": typical_calls,
        "target": order.target,
        "tools": list(order.tools),
        "goal": order.goal,
        "result": order.result,
        "window": order.window,
        "corrections": list(order.corrections),
        "mode": "read",
    }


def result_lines(result: str) -> list[str]:
    """What the check's task says about the form of its result. Only ``table`` names an
    "Ergebnisform": the agent's rule turns a named form into a table with exactly its keys, so a
    plain diagnosis must not name one (it did: "Ergebnisform: diagnosis")."""
    if result == "table":
        return [
            f"Ergebnisform: {' | '.join(TABLE_COLUMNS)}",
            'Gib im Feld "table" eine Zeile je geprüftem Punkt mit genau den Schlüsseln '
            f"{' und '.join(TABLE_COLUMNS)}; in Beleg die Referenz (E1, E2 …).",
        ]
    if result == "yesno":
        return [
            "Ergebnis: Ja oder Nein. Beginne die Zusammenfassung mit „Ja.“ oder „Nein.“, oder "
            "mit „Unklar.“, wenn die Evidenz es nicht trägt. Keine Tabelle."
        ]
    return ["Ergebnis: Diagnose (Zusammenfassung und Befunde), keine Tabelle."]


def table_markdown(rows: Any, limit: int = TABLE_ROWS) -> str:
    """The table of a finish as Markdown (empty when there is none): the columns of the first
    row, at most ``limit`` rows, pipes in values escaped."""
    if not isinstance(rows, list):
        return ""
    rows = [r for r in rows if isinstance(r, dict) and r][:limit]
    if not rows:
        return ""
    columns = [str(k) for k in rows[0]]

    def cell(value: Any) -> str:
        return " ".join(str(value if value is not None else "").split()).replace("|", r"\|")

    out = ["| " + " | ".join(columns) + " |", "|" + "---|" * len(columns)]
    out += ["| " + " | ".join(cell(r.get(c)) for c in columns) + " |" for r in rows]
    return "\n".join(out)
