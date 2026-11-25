"""The messages the applications send each other, from one file (``proto/messages/registry.json``).

A message is typed ``<application>.<purpose>``; the prefix is the *sending* application. The
registry says which application may send which type to which, what its payload looks like, how far
to trust it and what happens when it arrives (brief PPH-05-messages-chains-mandates,
section 3). An unknown type, an edge that is not listed or a payload that does not validate is
refused: nothing runs on free text, and the applications know each other's *responsibilities*, not
each other's contents.

This module only reads and checks. Storing a message is :mod:`topcoat_hub.message_store`, acting on
one is :mod:`topcoat_hub.message_service`.
"""

import functools
import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import jsonschema

from topcoat_hub.config import REPO_ROOT

TRUSTS = ("own", "system", "foreign")
EFFECTS = ("none", "queue_task", "stage", "click", "mandate")


class MessageError(ValueError):
    """A message the registry refuses; ``code`` says why (unknown_type, bad_edge, bad_payload)."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True)
class MessageType:
    name: str
    senders: tuple[str, ...]
    receivers: tuple[str, ...]
    trust: str
    needs_review: bool
    effect: str
    schema_version: int
    labels: dict[str, str]
    payload: dict[str, Any]

    def label(self, language: str) -> str:
        return self.labels.get(language) or self.labels.get("en") or self.name


@dataclass(frozen=True)
class Registry:
    types: dict[str, MessageType]
    responsibilities: dict[str, dict[str, str]]
    states: tuple[str, ...]


def _path() -> Path:
    root = os.environ.get("TOPCOAT_PROTO_ROOT")
    return (Path(root) if root else REPO_ROOT / "proto") / "messages" / "registry.json"


@functools.cache
def load() -> Registry:
    raw = json.loads(_path().read_text(encoding="utf-8"))
    types: dict[str, MessageType] = {}
    for entry in raw["types"]:
        name = entry["name"] if "name" in entry else entry["type"]
        if name in types:
            raise ValueError(f"{_path()}: the type {name} is listed twice")
        if entry["trust"] not in TRUSTS or entry["effect"] not in EFFECTS:
            raise ValueError(f"{_path()}: {name} has an unknown trust or effect")
        if not name.startswith(f"{entry['from'][0]}."):
            raise ValueError(f"{_path()}: {name} must start with its sending application")
        jsonschema.Draft202012Validator.check_schema(entry["payload"])
        types[name] = MessageType(
            name=name,
            senders=tuple(entry["from"]),
            receivers=tuple(entry["to"]),
            trust=entry["trust"],
            needs_review=bool(entry["needs_review"]),
            effect=entry["effect"],
            schema_version=int(entry["schema_version"]),
            labels=dict(entry["label"]),
            payload=entry["payload"],
        )
    return Registry(
        types=types,
        # the responsibility of an application is its contract's purpose (step 1.9)
        responsibilities={app: _purpose(app) for app in raw["applications"]},
        states=tuple(raw["states"]),
    )


def _purpose(app: str) -> dict[str, str]:
    from topcoat_hub import agent_contract

    contract = agent_contract.get(app)
    if contract is None:
        raise ValueError(f"{_path()}: the application {app} has no contract in proto/agents")
    return {"en": contract.purpose("en"), "de": contract.purpose("de")}


def get(name: str) -> MessageType:
    found = load().types.get(name)
    if found is None:
        raise MessageError("unknown_type", f"there is no message type {name}")
    return found


def edges() -> set[tuple[str, str, str]]:
    """Every (type, from, to) the registry allows."""
    return {(t.name, s, r) for t in load().types.values() for s in t.senders for r in t.receivers}


def check_edge(name: str, sender: str, receiver: str) -> MessageType:
    """The type, when ``sender`` may send it to ``receiver``; a :class:`MessageError` when not."""
    spec = get(name)
    if sender not in spec.senders or receiver not in spec.receivers:
        raise MessageError("bad_edge", f"{sender} may not send {name} to {receiver}")
    return spec


def check_payload(name: str, payload: Any) -> None:
    """The payload against the schema of its type; a :class:`MessageError` naming the problem."""
    try:
        jsonschema.validate(payload, get(name).payload, cls=jsonschema.Draft202012Validator)
    except jsonschema.ValidationError as exc:
        where = "/".join(str(p) for p in exc.absolute_path)
        raise MessageError(
            "bad_payload", f"{name}: {exc.message}" + (f" ({where})" if where else "")
        ) from exc


def validate(name: str, sender: str, receiver: str, payload: Any) -> MessageType:
    """Everything the registry checks about one message: the type, the edge, the payload."""
    spec = check_edge(name, sender, receiver)
    check_payload(name, payload)
    return spec


def report_kind_of(name: str) -> str:
    """The ``report_kind`` of the node a message of this type is stored as: the internal kinds keep
    theirs (``project.note`` is a ``note``), a request is a ``request``, the rest a ``message``."""
    if name.endswith(".request"):
        return "request"
    app, _, kind = name.partition(".")
    if app == "project" and kind in ("note", "question", "decision", "document", "fact"):
        return kind
    return "message"


def responsibility(app: str, language: str = "en") -> str:
    texts = load().responsibilities.get(app, {})
    return texts.get(language) or texts.get("en") or ""


# --- how a message reads -------------------------------------------------------------------------

BODY_CHARS = 80000  # a thorough research report is a document


def _lines(title: str, items: Any) -> list[str]:
    if not items:
        return []
    return [f"**{title}**", *[f"- {item}" for item in items]]


def title_of(name: str, payload: dict[str, Any], language: str = "en") -> str:
    """A short title for the node of a message: what it is about, else what kind it is."""
    for key in ("topic", "title", "summary", "what", "statement"):
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip().splitlines()[0][:120]
    if name == "project.order":
        return f"{payload.get('action', 'order')} {payload.get('branch', '')}".strip()
    if name == "project.review":
        return f"Review: {payload.get('verdict', '')}"
    if name.endswith(".failure"):
        return f"Failed: {payload.get('reason', '')}"
    if name.endswith(".clarification"):
        questions = payload.get("questions") or []
        return str(questions[0])[:120] if questions else get(name).label(language)
    return get(name).label(language)


def body_of(name: str, payload: dict[str, Any]) -> str:
    """The text of a message as the page and the knowledge base read it, written by code from the
    payload (Markdown). Data only: whatever the payload says is shown, never acted on."""
    out: list[str] = []
    if name == "project.request" or name.endswith(".request"):
        out += [str(payload.get("topic", ""))]
        out += _lines("Questions", payload.get("questions"))
        out += _lines("Must cover", payload.get("must_cover"))
        out += _lines("Expected", payload.get("expected"))
        extra = [
            f"{k}: {payload[k]}"
            for k in ("depth", "format", "min_sources", "min_hosts")
            if k in payload
        ]
        out += _lines("Wanted", extra)
    elif name == "project.order":
        spec = payload.get("spec") or {}
        out += [f"{payload.get('action')} on {payload.get('repo')}, branch {payload.get('branch')}"]
        out += [str(spec.get("goal", ""))]
        out += _lines("Expected behaviour", spec.get("expected"))
        out += _lines("Constraints", spec.get("constraints"))
        out += _lines("Out of scope", spec.get("out_of_scope"))
        out += _lines("Acceptance", payload.get("acceptance"))
        out += _lines("Findings to fix", payload.get("findings"))
    elif name == "project.review":
        out += [f"Verdict: {payload.get('verdict')}", str(payload.get("reasons", ""))]
        out += _lines(
            "Criteria",
            [
                f"{'met' if c.get('met') else 'not met'}: {c.get('item')}"
                for c in payload.get("criteria", [])
            ],
        )
        out += _lines("Follow-ups", payload.get("followups"))
    elif name.endswith(".result"):
        out += [str(payload.get("summary", ""))]
        for f in payload.get("findings") or []:
            quote = f.get("quote")
            source = f.get("source_url") or f.get("path") or f.get("source") or ""
            out.append(
                f"- {f.get('claim')}"
                + (f' — "{quote}"' if quote else "")
                + (f" ({source})" if source else "")
            )
        if name == "dev.result":
            if payload.get("branch"):
                out.append(f"Branch {payload['branch']} at {payload.get('head', '')}".strip())
            stat = payload.get("diff_stat")
            if stat:
                files, ins, dele = (stat.get(k, 0) for k in ("files", "insertions", "deletions"))
                out.append(f"Diff: {files} files, +{ins} -{dele}")
            tests = payload.get("tests")
            if tests:
                out.append(f"Tests: {tests.get('state')} {tests.get('summary', '')}".strip())
            out += _lines(
                "Acceptance",
                [
                    f"{'met' if a.get('met') else 'NOT met'}: {a.get('item')}"
                    for a in payload.get("acceptance", [])
                ],
            )
        out += _lines("Open points", payload.get("open_points"))
    elif name.endswith(".failure"):
        out += [f"Could not deliver: {payload.get('reason')}", str(payload.get("detail", ""))]
    elif name.endswith(".clarification"):
        out += _lines("The application needs to know", payload.get("questions"))
        out += _lines("Missing", payload.get("missing"))
    elif name.endswith(".changelog"):
        out += [str(payload.get("what", ""))]
        for key in ("by", "before", "after", "result"):
            if payload.get(key):
                out.append(f"{key}: {payload[key]}")
    elif name.endswith(".proposal"):
        out += [str(payload.get("title", "")), str(payload.get("why", ""))]
        out += _lines("Steps", payload.get("steps"))
    else:
        out += [str(payload.get("statement", ""))]
    return "\n\n".join(part for part in out if part).strip()[:BODY_CHARS]
