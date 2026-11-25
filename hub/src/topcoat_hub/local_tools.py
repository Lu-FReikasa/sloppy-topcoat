"""The chat's own small reads (pruefauftrag T3.6, T3.8): ``node_state``, ``monitor_state`` and
``read_check_record``. The hub runs them itself, read only, with no agent loop: one call, its answer
cut to a size, then one answer of the model that cites the result as evidence (``[E1]``).

This module holds the pure parts (the offered functions, reading arguments, cutting, the record
view). The chat runs the calls and writes the answer (``api/chat.py``)."""

from __future__ import annotations

import json
from typing import Any

from topcoat_hub import report

NODE_STATE = "node_state"
MONITOR_STATE = "monitor_state"
RECORD = "read_check_record"
ABOUT = "about_abilities"  # the long self-description of the application's contract (1.9)
LOCAL = (NODE_STATE, MONITOR_STATE, RECORD, ABOUT)
# the capabilities behind them; the scope decides whether they are offered at all
NODE_STATE_CAPABILITY = "topcoat.state"
MONITOR_CAPABILITY = "kuma.monitors"
NODE_STATE_CHARS = 1500
MONITOR_LINES = 20
RECORD_CHARS = 6000
SECTIONS = ("evidence", "failed", "steps")


def definitions(keys: set[str]) -> list[dict[str, Any]]:
    """The functions offered beside ``look_at_my_systems``. ``keys`` are the groups the scope
    allows: Topcoat's state needs ``topcoat``, the monitors need ``monitors``. The record reader is
    always offered, because a check's record is the person's own and only reads."""
    out: list[dict[str, Any]] = []
    if "topcoat" in keys:
        out.append(
            _function(
                NODE_STATE,
                "Read the state of a machine as the hub knows it (read only, no test is run): "
                "online or not, load, memory, the models it holds. Use it for questions like "
                "'how is node X doing' or 'what is the status'. Do not use it to find a fault; "
                "for that use look_at_my_systems.",
                {
                    "what": {
                        "type": "string",
                        "description": "'nodes' for every machine, or one machine's name",
                    }
                },
            )
        )
    if "monitors" in keys:
        out.append(
            _function(
                MONITOR_STATE,
                "Read the monitors (uptime checks and certificates), read only. Use it for "
                "'is anything down'.",
                {
                    "only_problems": {
                        "type": "boolean",
                        "description": "true (default) lists only monitors with a problem",
                    }
                },
            )
        )
    out.append(
        _function(
            RECORD,
            "Read the record of an earlier check of this person (its 'Akte:' line): one of its "
            "excerpts by reference (E3), a section (evidence, failed, steps), or nothing for its "
            "findings and the list of its evidence. Use it instead of a new check when the record "
            "answers the question.",
            {
                "task_id": {"type": "string", "description": "the id from the record's Akte line"},
                "ref": {"type": "string", "description": "one evidence reference such as E3"},
                "section": {"type": "string", "enum": list(SECTIONS)},
            },
            required=["task_id"],
        )
    )
    out.append(
        _function(
            ABOUT,
            "Read what you are for, what you may use, what you never do, what needs the person's "
            "click and whom you ask (your contract, in German). Use it when the person asks what "
            "you can do or which tools you have; answer in words from it, never with the names "
            "of functions or capabilities.",
            {},
        )
    )
    return out


def _function(
    name: str, description: str, properties: dict[str, Any], required: list[str] | None = None
) -> dict[str, Any]:
    params: dict[str, Any] = {"type": "object", "properties": properties}
    if required:
        params["required"] = required
    return {
        "type": "function",
        "function": {"name": name, "description": description, "parameters": params},
    }


def args_of(arguments: str) -> dict[str, Any]:
    """The arguments of a call as a dict (empty when they are not JSON)."""
    try:
        obj = json.loads(arguments or "{}")
    except ValueError:
        return {}
    return obj if isinstance(obj, dict) else {}


def node_state_input(args: dict[str, Any]) -> dict[str, Any]:
    what = str(args.get("what") or "nodes").strip()[:100] or "nodes"
    return {"what": what}


def monitor_input(args: dict[str, Any]) -> dict[str, Any]:
    flag = args.get("only_problems", True)
    return {"only_problems": flag if isinstance(flag, bool) else True}


def cut(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 1] + "…"


def monitor_text(data: Any) -> str:
    """The monitors as short lines, at most ``MONITOR_LINES`` of them."""
    rows = data if isinstance(data, list) else (data or {}).get("monitors") or []
    lines = [f"{r.get('name', '?')}: {r.get('status', '?')}" for r in rows if isinstance(r, dict)][
        :MONITOR_LINES
    ]
    return "\n".join(lines) or "no monitor has a problem"


def record_text(card: dict[str, Any] | None, ref: str = "", section: str = "") -> str:
    """The part of a check's record a question needs. Without ``ref`` or ``section``: the summary,
    the findings and the list of its evidence, never the excerpts (those are in the record only)."""
    if card is None:
        return "no such record of yours"
    if ref:
        for e in card.get("evidence") or []:
            if e.get("ref") == ref:
                head = f"{ref} ({e.get('capability') or '?'} on {e.get('node') or '?'})"
                return cut(f"{head}: {e.get('excerpt') or ''}", RECORD_CHARS)
        return f"no evidence {ref} in this record"
    if section == "failed":
        lines = [report.failure_text(f) for f in card.get("failures") or []]
        return cut("\n".join(lines) or "nothing failed", RECORD_CHARS)
    if section == "evidence":
        lines = [
            f"{e['ref']} {e.get('capability') or '?'} on {e.get('node') or '?'}"
            for e in card.get("evidence") or []
        ]
        return cut("\n".join(lines) or "no evidence", RECORD_CHARS)
    if section == "steps":
        return (
            f"the record has {card.get('calls', 0)} calls; their excerpts are the evidence sections"
        )
    summary = f"Summary: {card.get('summary') or ''}"
    finds = "\n".join(f"- [{f['tag']}] {f['claim']}" for f in (card.get("findings") or [])[:6])
    refs = ", ".join(e["ref"] for e in card.get("evidence") or [])
    return cut(f"{summary}\n{finds}\nEvidence: {refs or 'none'}", RECORD_CHARS)


def state_text(data: Any) -> str:
    """A state reply as one JSON line, cut to ``NODE_STATE_CHARS``."""
    return cut(json.dumps(data, ensure_ascii=False, default=str), NODE_STATE_CHARS)
