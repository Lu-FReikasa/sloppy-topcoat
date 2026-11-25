"""Change sets (PPH-04, 04g; decision 1 of PPH-04 section 1): the agent composes up to 100
operations freely, the person sees the whole set with a preview, may untick single items and applies
the rest with **one click**, all or nothing.

* **Propose** (the agent): the operations are checked against the schema, then *tried* in a
  transaction that is rolled back, so the person sees exactly what each operation would change
  (the lines it would write to the Akte, before -> after) and a set that could not be applied is
  refused at once, with the reason. The set is stored immutable (``content_hash`` over the
  operations); nothing can edit it afterwards.
* **Apply** (the person, a click on a signed-in session, never a key): the chosen items run in
  **one** transaction through the same store methods every other change uses, so all the rules
  (revisions, trees, loops, dates) and all the lines of the Akte come for free. One stale revision
  or one refused rule and nothing is applied. A ``changeset.applied`` line ties the resulting lines
  together; every line carries the set's id.
* There is no "approve all future changes": a set is one enumerated, hashed, atomic list.

Operations name nodes by id or, for something the set creates itself, by a local name (``n1``) that
must be created earlier in the same set. Text inside operations is data from the agent.
"""

import hashlib
import json
import re
import uuid
from collections.abc import Iterable
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any

import jsonschema
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from topcoat_hub import clock
from topcoat_hub.models import Node, ProjectChangeSet, ProjectEvent
from topcoat_hub.project_base import Invalid, ProjectError, get_project, owned
from topcoat_hub.project_events import Actor, EventLog
from topcoat_hub.project_runs import form_of, target_of
from topcoat_hub.project_store import ProjectStore

MAX_OPERATIONS = 100
PENDING, APPLIED, REFUSED, FAILED = "pending", "applied", "refused", "failed"
LOCAL = re.compile(r"^[a-z][a-z0-9_]{0,30}$")
UUID_RE = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)

_ID = {"type": "string", "minLength": 1, "maxLength": 40}  # a uuid or a local name
_TEXT = {"type": "string", "maxLength": 4000}
_TITLE = {"type": "string", "minLength": 1, "maxLength": 200}
_REV = {"type": "integer", "minimum": 1}
_TIME = {"type": ["string", "null"], "maxLength": 40}
_FIELDS = {
    "start_at": _TIME,
    "due_at": _TIME,
    "end_at": _TIME,
    "priority": {"type": "integer", "minimum": 0, "maximum": 5},
    "percent_done": {"type": "integer", "minimum": 0, "maximum": 100},
    "color": {"type": ["string", "null"], "maxLength": 20},
    "estimate_minutes": {"type": ["integer", "null"], "minimum": 0},
    "repeat_rule": {"type": ["object", "null"]},
    "is_milestone": {"type": "boolean"},
    "favorite": {"type": "boolean"},
}
FIELD_NAMES = tuple(_FIELDS)
_STATUS = {"enum": ["todo", "in_progress", "in_review", "blocked", "done", "dropped"]}
_LABELS = {
    "type": "array",
    "items": {"type": "string", "minLength": 1, "maxLength": 60},
    "maxItems": 20,
}


def _op(properties: dict[str, Any], required: list[str]) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {"op": {"type": "string"}, **properties},
        "required": ["op", *required],
        "additionalProperties": False,
    }


SCHEMAS: dict[str, dict[str, Any]] = {
    "create_node": _op(
        {
            "local": {"type": "string", "pattern": LOCAL.pattern},
            "kind": {"enum": ["topic", "task", "report"]},
            "report_kind": {"enum": ["note", "question", "decision", "document", "fact"]},
            "title": _TITLE,
            "body": _TEXT,
            "parent": _ID,
            "depends_on": {"type": "array", "items": _ID, "maxItems": 50},
            "labels": _LABELS,
            **_FIELDS,
            "as_of": _TIME,
            "origin_app": {"type": "string", "maxLength": 40},
        },
        ["kind", "title"],
    ),
    "update_node": _op(
        {
            "node": _ID,
            "revision": _REV,
            "title": _TITLE,
            "body": _TEXT,
            "body_append": _TEXT,
            "body_replace": {
                "type": "object",
                "properties": {
                    "old": {"type": "string", "minLength": 1, "maxLength": 4000},
                    "new": _TEXT,
                },
                "required": ["old", "new"],
                "additionalProperties": False,
            },
            "depends_on": {"type": "array", "items": _ID, "maxItems": 50},
            **_FIELDS,
        },
        ["node"],
    ),
    "move_node": _op(
        {"node": _ID, "revision": _REV, "parent": {"type": ["string", "null"]}}, ["node", "parent"]
    ),
    "set_status": _op({"node": _ID, "revision": _REV, "status": _STATUS}, ["node", "status"]),
    "schedule": _op(
        {"node": _ID, "revision": _REV, "start_at": _TIME, "due_at": _TIME, "end_at": _TIME},
        ["node"],
    ),
    "archive": _op({"node": _ID, "revision": _REV}, ["node"]),
    "relate": _op(
        {
            "node": _ID,
            "other": _ID,
            "kind": {
                "enum": [
                    "blocked_by",
                    "related",
                    "duplicate_of",
                    "precedes",
                    "copied_from",
                    "supersedes",
                ]
            },
        },
        ["node", "other", "kind"],
    ),
    "unrelate": _op({"relation_id": _ID}, ["relation_id"]),
    "label": _op({"node": _ID, "labels": _LABELS}, ["node", "labels"]),
    "unlabel": _op({"node": _ID, "labels": _LABELS}, ["node", "labels"]),
    "reminder": _op(
        {
            "node": _ID,
            "at": _TIME,
            "relative_to": {"enum": ["due", "start", "end"]},
            "offset_seconds": {"type": "integer"},
        },
        ["node"],
    ),
    "supersede_report": _op({"old": _ID, "by": _ID}, ["old", "by"]),
}


# One valid call per operation. They teach a small model the shape, they are shown in the error of
# a wrong call, and a test validates every one against its schema, so an example cannot rot.
EXAMPLES: dict[str, dict[str, Any]] = {
    "create_node": {"op": "create_node", "kind": "task", "title": "Order tape", "parent": "n1"},
    "update_node": {"op": "update_node", "node": "<node id>", "revision": 3, "title": "New title"},
    "move_node": {"op": "move_node", "node": "<node id>", "parent": "<new parent id>"},
    "set_status": {"op": "set_status", "node": "<node id>", "status": "dropped"},
    "schedule": {"op": "schedule", "node": "<node id>", "due_at": "2026-11-05T12:00:00Z"},
    "archive": {"op": "archive", "node": "<node id>"},
    "relate": {"op": "relate", "node": "<node id>", "other": "<other id>", "kind": "duplicate_of"},
    "unrelate": {"op": "unrelate", "relation_id": "<relation id>"},
    "label": {"op": "label", "node": "<node id>", "labels": ["urgent"]},
    "unlabel": {"op": "unlabel", "node": "<node id>", "labels": ["urgent"]},
    "reminder": {
        "op": "reminder",
        "node": "<node id>",
        "relative_to": "due",
        "offset_seconds": -3600,
    },
    "supersede_report": {
        "op": "supersede_report",
        "old": "<old report id>",
        "by": "<new report id>",
    },
}


def operation_item_schema() -> dict[str, Any]:
    """What one entry of ``operations`` looks like in the schema the model is offered: the keys of
    all operations by name, so a small model sees ``node`` (not ``node_id``) and ``status`` next to
    ``set_status``. It does not repeat the types or the per-operation rules: those are checked in
    :func:`check_operations`, whose error teaches the right call. Only ``op`` is required here, so
    that a wrong entry reaches that check instead of failing with a general message first."""
    keys: dict[str, Any] = {}
    for schema in SCHEMAS.values():
        for key in schema["properties"]:
            if key != "op":
                keys[key] = {}
    # ``op`` is not required here: an entry without it is completed (guess_op) or refused by
    # check_operations with its number and an example, which a bare schema error does not give
    return {
        "type": "object",
        "properties": {"op": {"type": "string"}, **keys},  # the names are in the description
    }


def _unknown_keys(op: dict[str, Any], name: str) -> list[str]:
    allowed = SCHEMAS[name]["properties"]
    return [k for k in op if k not in allowed]


def _hint(unknown: list[str], name: str) -> str:
    """What to do instead of an unknown key: the operation that takes it, or its likely spelling."""
    hints = []
    for key in unknown:
        if key == "node_id" and "node" in SCHEMAS[name]["properties"]:
            hints.append("`node_id` is called `node`")
            continue
        others = [n for n, s in SCHEMAS.items() if n != name and key in s["properties"]]
        if others:
            hints.append(
                f"`{key}` belongs to the operation {', '.join(f'`{o}`' for o in others[:3])}"
            )
        else:
            hints.append(f"`{key}` is not a field of any operation")
    return "; ".join(hints)


def teaching_error(op: dict[str, Any], name: str, message: str) -> str:
    """The error of an operation that does not fit its schema, for a model that must learn from it:
    the unknown keys with the operation they belong to, the keys this operation takes and one
    valid example."""
    props = SCHEMAS[name]["properties"]
    required = [k for k in SCHEMAS[name]["required"] if k != "op"]
    parts = [message]
    if unknown := _unknown_keys(op, name):
        parts.append("unknown: " + _hint(unknown, name))
    takes = ", ".join(k for k in props if k != "op")
    parts.append(f"{name} takes: {takes} (required: {', '.join(required)})")
    if name in EXAMPLES:
        parts.append(f"example: {json.dumps(EXAMPLES[name])}")
    return ". ".join(parts)


def content_hash(project_id: str, operations: list[dict[str, Any]]) -> str:
    body = json.dumps({"project": project_id, "operations": operations}, sort_keys=True)
    return hashlib.sha256(body.encode()).hexdigest()


class SetError(ProjectError):
    """An operation of a set cannot be carried out; ``index`` says which."""

    def __init__(self, index: int, message: str) -> None:
        super().__init__(f"operation {index + 1}: {message}")
        self.index, self.reason = index, message


# Names a model writes next to an operation's own fields, where they say nothing new: the project
# is the call's (project_id), and a parent is named `parent` (what the schema has).
RENAMES = {"parent_id": "parent", "node_id": "node", "depends_on_ids": "depends_on"}


_NUMBERED = re.compile(r"^\s*(\d+(?:\.\d+)+)\.?\s+(\S.*)$")


def _numbered_tree(out: list[Any]) -> None:
    """Titles that carry the numbers a person asked for ("1.8 Wireframe", "1.8.1 Recherche",
    "1.8.2 Dev") say the tree: a node whose number continues the number of another node of the set
    becomes its child, and the numbers leave the titles (the hub numbers the nodes itself). Taken
    only when the set nests its own numbers, so a title like "3.5 mm jack" is left alone. Live
    2026-11-15: the three tasks were laid side by side under the topic and became 1.8, 1.9, 1.10."""
    numbered: dict[str, int] = {}
    for i, op in enumerate(out):
        if isinstance(op, dict) and op.get("op") == "create_node":
            m = _NUMBERED.match(str(op.get("title") or ""))
            if m:
                numbered.setdefault(m.group(1), i)
    nests = {n: n.rsplit(".", 1)[0] for n in numbered if n.rsplit(".", 1)[0] in numbered}
    if not nests:
        return
    for number, i in numbered.items():
        op = out[i]
        op["title"] = _NUMBERED.match(op["title"]).group(2)  # type: ignore[union-attr]
        if number in nests:
            target = numbered[nests[number]]
            if not out[target].get("local"):
                out[target]["local"] = f"auto{target}"
            op["parent"] = out[target]["local"]


def resolve_parents(operations: list[Any]) -> list[Any]:
    """A parent the set created earlier, named in a way the model made up (``local:1``, a
    placeholder, the topic's title), becomes that operation's local name. A created operation gets
    a local name only when a later one refers to it; an unclear reference is left alone, and the
    check then refuses it."""
    out: list[Any] = [dict(op) if isinstance(op, dict) else op for op in operations]
    _numbered_tree(out)
    created: list[tuple[int, str, str]] = []  # (index in out, title, kind) of the earlier creates
    resolved: dict[str, int] = {}  # a made-up reference -> the operation it was taken for
    for i, op in enumerate(out):
        if not isinstance(op, dict) or op.get("op") != "create_node":
            continue
        parent = op.get("parent")
        if isinstance(parent, str) and not UUID_RE.match(parent):
            ref = re.sub(r"^(local:|\[|\]|<|\s)+|(\]|>|\s)+$", "", parent).strip().lower()
            known = {str(out[j].get("local")) for j, _, _ in created if out[j].get("local")}
            if ref in resolved:
                # the same placeholder again (live 2026-11-15: "<new_node_id_1.8>" for two
                # children; the second was refused as unknown)
                op["parent"] = out[resolved[ref]]["local"]
            elif ref in known:
                op["parent"] = ref  # the name the set gave, written without the prefix
            else:
                matches = [
                    j
                    for j, title, _ in created
                    if ref == title.strip().lower() or (title and ref in title.lower())
                ]
                topics = [j for j, _, kind in created if kind == "topic"]
                if len(matches) == 1:
                    target = matches[0]
                elif not matches and len(topics) == 1:
                    target = topics[0]  # a made-up name for the one topic the set created
                elif not matches and len(created) == 1:
                    target = created[0][0]  # the only node created so far
                else:
                    target = None
                if target is not None:
                    if not out[target].get("local"):
                        out[target]["local"] = f"auto{target}"
                    op["parent"] = out[target]["local"]
                    resolved[ref] = target
        title = op.get("title") if isinstance(op.get("title"), str) else ""
        created.append((i, title, str(op.get("kind") or "")))
    return out


_UPDATE_KEYS = ("title", "body", "body_append", "body_replace", "depends_on", *FIELD_NAMES)


def guess_op(op: dict[str, Any]) -> str | None:
    """The operation an entry without ``op`` can only mean (live 2026-11-14: Gemma wrote the new
    texts of three tasks three times and left ``op`` out of each, and was refused each time): a
    node and new fields is ``update_node``, a node and a status ``set_status``, a kind and a title
    without a node ``create_node``. Anything else stays without one and is refused with an
    example."""
    if "node" in op:
        if any(k in op for k in _UPDATE_KEYS):
            return "update_node"
        if "status" in op:
            return "set_status"
        return None
    if "kind" in op and "title" in op:
        return "create_node"
    return None


def resolve_numbers(operations: Any, nodes: Iterable[Any], project_title: str = "") -> Any:
    """A node named by its number (``1.5``) or its reference (``Topcoat 1.5``) instead of its id
    becomes the id (live: two of fifteen calls of a run were lost to a mistyped UUID). A name the
    set creates (``n1``) and anything unknown stay as they are; the check then refuses those."""
    if not isinstance(operations, list):
        return operations
    by_number: dict[str, str] = {}
    prefix = project_title.strip().lower()
    for n in nodes:
        number = str(n.number)
        by_number[number] = str(n.id)
        if prefix:
            by_number[f"{prefix} {number}"] = str(n.id)

    def resolved(ref: Any) -> Any:
        if not isinstance(ref, str) or UUID_RE.match(ref):
            return ref
        key = ref.strip().lower().removeprefix("#")
        return by_number.get(key, ref)

    out: list[Any] = []
    for op in operations:
        if not isinstance(op, dict):
            out.append(op)
            continue
        fixed = dict(op)
        for key in ("node", "parent", "other", "old", "by"):
            if key in fixed:
                fixed[key] = resolved(fixed[key])
        if isinstance(fixed.get("depends_on"), list):
            fixed["depends_on"] = [resolved(d) for d in fixed["depends_on"]]
        out.append(fixed)
    return out


def normalize_operations(operations: Any, project_id: uuid.UUID | str | None = None) -> Any:
    """The operations with those harmless variants mapped to the schema's names, so a model that
    writes ``parent_id`` or repeats the project does not fail on the form (the meaning is the same).
    A reference to the project itself as a parent is no node: it is the top level."""
    if not isinstance(operations, list):
        return operations
    project = str(project_id) if project_id is not None else None
    out: list[Any] = []
    for op in operations:
        if not isinstance(op, dict):
            out.append(op)
            continue
        fixed: dict[str, Any] = {}
        for key, value in op.items():
            if key == "project_id":
                continue  # the call names the project
            target = RENAMES.get(key, key)
            if target != key and target in op:
                continue  # both spellings given: the schema's wins
            fixed[target] = value
        if "parent" in fixed and (fixed["parent"] is None or fixed["parent"] == project):
            del fixed["parent"]  # no parent is the top level
        if "op" not in fixed and (guessed := guess_op(fixed)):
            fixed = {"op": guessed, **fixed}
        fixed = known_target(_target_into_body(_real_newlines(fixed)))
        status = fixed.get("status") if fixed.get("op") == "update_node" else None
        if isinstance(status, str) and isinstance(fixed.get("node"), str):
            # a status belongs to set_status (live 2026-11-18: refused, and the run gave up)
            fixed = {k: v for k, v in fixed.items() if k != "status"}
            out.append({"op": "set_status", "node": fixed["node"], "status": status})
            if set(fixed) <= {"op", "node", "revision"}:
                continue  # the update carried nothing but the status
        labels = fixed.get("labels") if fixed.get("op") == "update_node" else None
        if isinstance(labels, list) and isinstance(fixed.get("node"), str):
            # labels belong to their own operation: the change and the labels both stand
            fixed = {k: v for k, v in fixed.items() if k != "labels"}
            out += [fixed, {"op": "label", "node": fixed["node"], "labels": labels}]
            continue
        out.append(fixed)
    # the names are the schema's now: a parent the set gave (parent_id: n1 included) is resolved
    return resolve_parents(out)


def _real_newlines(op: dict[str, Any]) -> dict[str, Any]:
    """A text a model escaped twice ("Zielanwendung: research\\nErgebnisform: ...": the two
    characters backslash and n, no line break at all, live 2026-11-18) gets its line breaks back.
    Only a text without a single real line break is touched."""
    out = dict(op)
    for key in ("body", "body_append"):
        text = out.get(key)
        if isinstance(text, str) and "\n" not in text and "\\n" in text:
            out[key] = text.replace("\\n", "\n")
    return out


_TARGET_KEYS = ("application", "zielanwendung", "target", "target_application")
_TARGET_LINE = re.compile(r"^\s*Zielanwendung:.*$", re.MULTILINE | re.IGNORECASE)


def _target_into_body(op: dict[str, Any]) -> dict[str, Any]:
    """An application given as a field of a task (live 2026-11-18: ``"application": "research"``
    on update_node; the proposal was refused and the run gave up) is the task's line
    ``Zielanwendung:`` of the body that the operation writes. Without a body to write it stays as
    it is and is refused with the field named."""
    if op.get("op") not in ("create_node", "update_node"):
        return op
    key = next((k for k in op if k.casefold() in _TARGET_KEYS), None)
    if key is None or not isinstance(op[key], str) or not op[key].strip():
        return op
    if op.get("op") == "update_node" and not isinstance(op.get("body"), str):
        return op
    line = f"Zielanwendung: {op[key].strip()}"
    body = str(op.get("body") or "")
    body = _TARGET_LINE.sub(line, body, count=1) if _TARGET_LINE.search(body) else (
        f"{line}\n{body}" if body.strip() else line
    )  # fmt: skip
    return {**{k: v for k, v in op.items() if k != key}, "body": body}


# the names a model writes for an application instead of its key (live 2026-11-18: "Zielanwendung:
# development" was refused and cost a turn; "Verwaltung" is what the person says)
TARGET_SYNONYMS = {
    "development": "dev",
    "entwicklung": "dev",
    "develop": "dev",
    "recherche": "research",
    "verwaltung": "admin",
    "administration": "admin",
    "administrator": "admin",
    "projekte": "project",
    "projekt": "project",
    "projects": "project",
}
_TARGET_NAME = re.compile(r"^(\s*Zielanwendung:\s*)([A-Za-zÄÖÜäöü]+)(\s*)$", re.M | re.I)


def known_target(op: dict[str, Any]) -> dict[str, Any]:
    """A task's ``Zielanwendung:`` that names an application by another word (development,
    Verwaltung) names it by its key; an unknown name stays and is refused by the check."""
    body = op.get("body")
    if not isinstance(body, str):
        return op

    def key(m: re.Match[str]) -> str:
        name = TARGET_SYNONYMS.get(m.group(2).casefold(), m.group(2))
        return f"{m.group(1)}{name}{m.group(3)}"

    fixed = _TARGET_NAME.sub(key, body, count=1)
    return op if fixed == body else {**op, "body": fixed}


# Verwaltung has the machines, their models and Topcoat's settings (app_catalogue.RULES). Live
# 2026-11-18: "Punkt 2 an Verwaltung delegieren" became "Zielanwendung: research".
_ADMIN_HINT = re.compile(
    r"\[verwaltung\]|\bverwaltung\b|\badmin\w*|\bnodes?\b|\bvram\b|\bmaschinen?\b", re.I
)
_DEV_HINT = re.compile(r"\[entwicklung\]|\bcode\b|\brepo\w*|\bimplement\w*|\bdatei\w*", re.I)
_PROJECT_HINT = re.compile(r"\[projekt\]", re.I)


def complete_task_form(operations: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """A task of an agent plan that lacks the lines a run needs gets them, instead of a refusal
    (live 2026-11-08..10: 14 refusals, each a lost model turn): the application from the title
    and body ("[Projekt]" → project; Verwaltung, admin, nodes, VRAM → admin; "[Entwicklung]",
    code, repository → dev; else research), the result
    form "Ergebnis | Beleg" and one checklist line with the title. A line the model wrote is never
    replaced, and an application that is named but unknown is still refused by the check."""
    out = []
    for op in operations:
        if not (
            isinstance(op, dict) and op.get("op") == "create_node" and op.get("kind") == "task"
        ):
            out.append(op)
            continue
        body = str(op.get("body") or "")
        title = str(op.get("title") or "")
        add: list[str] = []
        if not re.search(r"^\s*Zielanwendung:", body, re.MULTILINE):
            text = f"{title}\n{body}"
            app = (
                "project"
                if _PROJECT_HINT.search(text)
                else "admin"
                if _ADMIN_HINT.search(text)
                else "dev"
                if _DEV_HINT.search(text)
                else "research"
            )
            add.append(f"Zielanwendung: {app}")
        if not form_of(body):
            add.append("Ergebnisform: Ergebnis | Beleg")
        if "- [ ]" not in body:
            add.append(f"- [ ] {title or 'erledigt'}")
        if add:
            op = {**op, "body": "\n".join([*add, body] if body.strip() else add)}
        out.append(op)
    return out


MACHINE_APPS = ("admin", "project")  # where a point about one machine can be answered or kept
_ITEM = re.compile(r"^\s*- \[[ xX]\]\s*(.+?)\s*$", re.MULTILINE)


def check_machine_points(operations: list[dict[str, Any]], machines: Iterable[str]) -> None:
    """A checklist point that names one of the person's machines is a question for Verwaltung:
    only it sees the machines and their models. Live 2026-11-18: three plans in a row put "Welche
    Modelle auf node-amd und node-nvidia …" into a task for dev or research, which can never answer it,
    although the catalogue's rule says so (issue 2.4). Refused with the way out named."""
    names = [m for m in machines if len(m) >= 3]
    if not names:
        return
    canonical = {m.lower(): m for m in names}
    pattern = re.compile(
        r"(?<![\w-])(" + "|".join(re.escape(m) for m in names) + r")(?![\w-])", re.IGNORECASE
    )
    for i, op in enumerate(operations):
        if op.get("op") not in ("create_node", "update_node"):
            continue
        body = op.get("body")
        if not isinstance(body, str) or target_of(body) in MACHINE_APPS or target_of(body) is None:
            continue
        for item in _ITEM.findall(body):
            if found := pattern.search(item):
                raise SetError(
                    i,
                    f"point '{item[:120]}' asks about the machine "
                    f"'{canonical.get(found.group(1).lower(), found.group(1))}': only "
                    "Verwaltung sees the machines and their models. Put this point into its own "
                    "task with 'Zielanwendung: admin' and let the task that needs its answer "
                    "depend on it (depends_on)",
                )


async def plan_checked(
    sm: async_sessionmaker[AsyncSession], operations: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """The checks of an agent's plan, for a change set and for a single staged change alike:
    the missing lines completed, the form checked, a point about a machine sent to Verwaltung.
    Live 2026-11-18: refused as a set, the planner staged the same tasks one by one with
    project.node.create, past every check."""
    ops = complete_task_form(operations)
    check_task_form(ops)
    async with sm() as db:
        machines = list(await db.scalars(select(Node.name)))
    check_machine_points(ops, machines)
    return ops


def check_task_form(operations: list[dict[str, Any]]) -> None:
    """A task a plan proposes says where it is done and what its result looks like; without both
    the hub could not start it (project_runs.py), so the plan is refused here, at once. Only the
    project agent's plans are checked this way; a task the person writes by hand is free."""
    for i, op in enumerate(operations):
        if op.get("op") != "create_node" or op.get("kind") != "task":
            continue
        body = op.get("body") or ""
        if target_of(body) is not None and form_of(body) and "- [ ]" in body:
            continue
        named = re.search(r"^\s*Zielanwendung:\s*(.+?)\s*$", body, re.MULTILINE)
        if named is not None and target_of(body) is None:
            raise SetError(
                i,
                f"'Zielanwendung: {named.group(1)}' is not an application: write exactly one of "
                "research, dev, admin or project (the application that does the task, not a node "
                "name), on a line of its own",
            )
        # the refusal carries a ready body to copy: a model that copies it passes at once
        ready = json.dumps(
            "Zielanwendung: research\nErgebnisform: Ergebnis | Beleg\n- [ ] erster Prüfpunkt",
            ensure_ascii=False,
        )
        raise SetError(
            i,
            f"the task {op.get('title')!r} needs three lines in its body: "
            "'Zielanwendung: research' (or dev, admin or project: where it is done), "
            "'Ergebnisform: <column> | <column>' (the columns of its result table) and one "
            "checklist line '- [ ] <item>' per thing to check. "
            f"Put this body into it, with its items: {ready}",
        )


def check_operations(operations: Any) -> list[dict[str, Any]]:
    """The operations, checked against their schemas (nothing is tried yet)."""
    if not isinstance(operations, list) or not operations:
        raise Invalid("a change set has at least one operation")
    if len(operations) > MAX_OPERATIONS:
        raise Invalid(f"a change set has at most {MAX_OPERATIONS} operations")
    defined: set[str] = set()
    for i, op in enumerate(operations):
        name = op.get("op") if isinstance(op, dict) else None
        if name not in SCHEMAS:
            raise SetError(
                i,
                f"op must be one of {', '.join(SCHEMAS)}; every operation names it, for example "
                f"{json.dumps(EXAMPLES['update_node'])} or {json.dumps(EXAMPLES['create_node'])}",
            )
        try:
            jsonschema.validate(op, SCHEMAS[name])
        except jsonschema.ValidationError as exc:
            where = ".".join(str(part) for part in exc.absolute_path)  # names the field
            message = f"{where}: {exc.message}" if where else exc.message
            raise SetError(i, teaching_error(op, name, message)) from exc
        for key in ("node", "other", "parent", "old", "by"):
            ref = op.get(key)
            if isinstance(ref, str) and not UUID_RE.match(ref) and ref not in defined:
                raise SetError(i, f"{key} {ref!r} is neither an id nor a name created earlier")
        for ref in op.get("depends_on", []):
            if not UUID_RE.match(ref) and ref not in defined:
                raise SetError(i, f"depends_on {ref!r} is neither an id nor a name created earlier")
        if name == "create_node" and op.get("local"):
            if op["local"] in defined:
                raise SetError(i, f"the name {op['local']!r} is used twice")
            defined.add(op["local"])
        if name == "update_node":
            used = [k for k in ("body", "body_append", "body_replace") if k in op]
            if len(used) > 1:
                raise SetError(i, "use only one of body, body_append and body_replace")
        if name == "reminder" and ("at" in op) == ("relative_to" in op):
            raise SetError(i, "a reminder is either at a time or relative to a date")
    return operations


def dependants(operations: list[dict[str, Any]]) -> dict[int, set[int]]:
    """For each operation the operations that use a name it creates: unticking it disables them."""
    creator: dict[str, int] = {
        op["local"]: i
        for i, op in enumerate(operations)
        if op.get("op") == "create_node" and op.get("local")
    }
    out: dict[int, set[int]] = {i: set() for i in range(len(operations))}
    for i, op in enumerate(operations):
        refs = [op.get(k) for k in ("node", "other", "parent", "old", "by")] + list(
            op.get("depends_on", [])
        )
        for ref in refs:
            if isinstance(ref, str) and ref in creator and creator[ref] != i:
                out[creator[ref]].add(i)
    return out


def closure(
    selected: Iterable[int], operations: list[dict[str, Any]]
) -> tuple[set[int], dict[int, int]]:
    """The operations that can run when ``selected`` are ticked: those that do not need a name from
    an unticked one. Returns them and, for each one that was dropped, the unticked item it needs."""
    chosen = set(selected)
    need = dependants(operations)
    dropped: dict[int, int] = {}
    changed = True
    while changed:
        changed = False
        for creator, users in need.items():
            if creator in chosen:
                continue
            for user in users:
                if user in chosen:
                    chosen.discard(user)
                    dropped[user] = creator
                    changed = True
    return chosen, dropped


# --- trying and applying inside one transaction -------------------------------------------------


class _Shared:
    """A session for the store methods of one set: they ``commit`` as they always do, which here
    only flushes, so the whole set is one transaction that the caller commits or rolls back. The
    events the methods add are noted per operation."""

    def __init__(self, real: AsyncSession) -> None:
        self._real = real
        self.added: list[ProjectEvent] = []

    def __getattr__(self, name: str) -> Any:
        return getattr(self._real, name)

    def add(self, obj: Any) -> None:
        if isinstance(obj, ProjectEvent):
            self.added.append(obj)
        self._real.add(obj)

    async def commit(self) -> None:
        await self._real.flush()


class _SharedMaker:
    def __init__(self, shared: _Shared) -> None:
        self.shared = shared

    def __call__(self) -> Any:
        @asynccontextmanager
        async def cm() -> Any:
            yield self.shared

        return cm()


@dataclass
class OpResult:
    index: int
    op: str
    target: str | None = None
    lines: list[dict[str, Any]] = field(default_factory=list)
    # what the operation does to the node it touches: field -> [before, after] (title, body,
    # status), so the person reads the new text instead of a line kind (live 2026-11-14)
    change: dict[str, list[Any]] = field(default_factory=dict)
    ref: str | None = None  # the node's reference ("Topcoat 1.5")
    node_id: uuid.UUID | None = None

    @property
    def changes_nothing(self) -> bool:
        """A node operation whose trial left the node as it was (the title it already had, a status
        it already had): it would only add an empty line to the Akte."""
        return (
            self.op in NODE_OPS
            and not self.change
            and bool(self.lines)
            and all(line["kind"] == "node.updated" and not line["payload"] for line in self.lines)
        )


NODE_OPS = ("update_node", "set_status", "schedule", "archive")
SNAPSHOT = ("title", "body", "status")


async def _snapshot(store: ProjectStore, user_id: uuid.UUID, node_id: uuid.UUID) -> dict[str, Any]:
    node = await store.get_node(user_id, node_id)
    return {k: getattr(node, k, None) for k in SNAPSHOT}


async def run_operations(
    db: AsyncSession,
    user_id: uuid.UUID,
    project_id: uuid.UUID,
    operations: list[dict[str, Any]],
    chosen: set[int],
    actor: Actor,
    default_zone: str,
) -> tuple[list[OpResult], dict[str, uuid.UUID]]:
    """Carry out the chosen operations in order inside ``db``'s transaction. Raises
    :class:`SetError` for the first one that cannot be done; the caller rolls back."""
    shared = _Shared(db)
    store = ProjectStore(_SharedMaker(shared), actor, default_zone)  # type: ignore[arg-type]
    names: dict[str, uuid.UUID] = {}
    results: list[OpResult] = []

    def ident(ref: str, index: int) -> uuid.UUID:
        if UUID_RE.match(ref):
            return uuid.UUID(ref)
        if ref in names:
            return names[ref]
        raise SetError(index, f"{ref!r} is created by an item that is not part of the apply")

    async def fresh(node_id: uuid.UUID) -> int:
        return (await store.get_node(user_id, node_id)).revision

    # a node an earlier operation of this set changed has a newer revision than the one the model
    # read: the later operation uses the new one (live: "operation 2: revision" for a second
    # operation on the same task, which the model could not make sense of)
    touched: set[uuid.UUID] = set()

    for i, op in enumerate(operations):
        if i not in chosen:
            continue
        if op.get("node") and op["op"] != "create_node":
            node_ref = ident(op["node"], i)
            if node_ref in touched and op.get("revision"):
                op = {k: v for k, v in op.items() if k != "revision"}
            touched.add(node_ref)
        before = len(shared.added)
        result = OpResult(i, op["op"])
        try:
            old: dict[str, Any] = {}
            if op["op"] in NODE_OPS:
                result.node_id = ident(op["node"], i)
                old = await _snapshot(store, user_id, result.node_id)
            await _one(store, user_id, project_id, op, i, ident, fresh, names, result)
            if result.node_id is not None:
                new = await _snapshot(store, user_id, result.node_id)
                result.change = {k: [old.get(k), v] for k, v in new.items() if old.get(k) != v}
        except SetError:
            raise
        except ProjectError as exc:
            raise SetError(i, str(exc)) from exc
        result.lines = [
            {"kind": e.kind, "node": e.node_ref, "payload": e.payload}
            for e in shared.added[before:]
        ]
        result.ref = next((line["node"] for line in result.lines if line["node"]), None)
        results.append(result)
    return results, names


async def _one(store, user_id, project_id, op, i, ident, fresh, names, result):  # type: ignore[no-untyped-def]
    name = op["op"]
    if name == "create_node":
        parent = ident(op["parent"], i) if op.get("parent") else None
        fields = {k: op[k] for k in (*FIELD_NAMES, "as_of", "origin_app") if k in op}
        node = await store.create_node(
            user_id, project_id, kind=op["kind"], title=op["title"], body=op.get("body", ""),
            parent_id=parent, depends_on=[ident(d, i) for d in op.get("depends_on", [])],
            report_kind=op.get("report_kind"), fields=fields,
        )  # fmt: skip
        if op.get("local"):
            names[op["local"]] = node.id
        if op.get("labels"):
            await _labels(store, user_id, node.id, op["labels"], add=True)
        result.target = f"{node.number} {node.title}"
        return
    if name == "relate":
        node_id, other_id = ident(op["node"], i), ident(op["other"], i)
        await store.add_relation(user_id, node_id, other_id, op["kind"])
        result.target = f"{op['kind']}"
        return
    if name == "unrelate":
        await store.end_relation(user_id, uuid.UUID(op["relation_id"]))
        return
    if name == "supersede_report":
        await store.supersede_report(user_id, ident(op["old"], i), by_id=ident(op["by"], i))
        return
    node_id = ident(op["node"], i)
    if name in ("label", "unlabel"):
        await _labels(store, user_id, node_id, op["labels"], add=name == "label")
        result.target = ", ".join(op["labels"])
        return
    if name == "reminder":
        await store.add_reminder(
            user_id, node_id, at=clock.parse(op["at"]) if op.get("at") else None,
            relative_to=op.get("relative_to"), offset_seconds=op.get("offset_seconds"),
        )  # fmt: skip
        return
    revision = op.get("revision") or await fresh(node_id)
    if name == "move_node":
        parent = op["parent"]
        await store.move_node(
            user_id,
            node_id,
            revision=revision,
            parent_id=None if parent is None else ident(parent, i),
        )
        return
    node = await store.get_node(user_id, node_id)
    kwargs: dict[str, Any] = {}
    fields = {k: op[k] for k in FIELD_NAMES if k in op}
    if name == "set_status":
        kwargs["status"] = op["status"]
    elif name == "archive":
        if node.kind != "task":
            raise SetError(i, "only a task is archived")
        kwargs["status"] = "dropped"
    elif name == "schedule":
        fields = {k: op[k] for k in ("start_at", "due_at", "end_at") if k in op}
    else:  # update_node
        for key in ("title", "body"):
            if key in op:
                kwargs[key] = op[key]
        if "body_append" in op:
            kwargs["body"] = node.body + op["body_append"]
        if "body_replace" in op:
            old, new = op["body_replace"]["old"], op["body_replace"]["new"]
            if node.body.count(old) != 1:
                raise SetError(i, "the text to replace must occur exactly once in the body")
            kwargs["body"] = node.body.replace(old, new, 1)
        if "depends_on" in op:
            kwargs["depends_on"] = [ident(d, i) for d in op["depends_on"]]
    await store.update_node(user_id, node_id, revision=revision, fields=fields, **kwargs)
    result.target = node.title


async def _labels(
    store: ProjectStore, user_id: uuid.UUID, node_id: uuid.UUID, titles: list[str], *, add: bool
) -> None:
    have = {label.title: label for label in await store.list_labels(user_id)}
    ids = []
    for title in titles:
        label = have.get(title)
        if label is None and add:
            label = await store.create_label(
                user_id, title=title
            )  # a label is metadata of the person
            have[title] = label
        if label is not None:
            ids.append(label.id)
    if add:
        await store.set_node_labels(user_id, node_id, add=ids)
    else:
        await store.set_node_labels(user_id, node_id, remove=ids)


# --- the stored sets ----------------------------------------------------------------------------


def summary_of(row: Any) -> str:
    """What a set changes, per node, with the values, in a line the answer and the person can
    check: ``1.5: title «Neu», body; 1.6: status todo → dropped; new 1.7 «Title» (Zielanwendung
    admin)``. Read from the trial (``preview``) and the operations. Live 2026-11-18: with only the
    field names the closing answer guessed the values ("all development", "in_review", a title
    the set did not have)."""
    parts: list[str] = []
    for p in row.preview or []:
        op = (row.operations or [])[p["index"]] if p["index"] < len(row.operations or []) else {}
        ref = str(p.get("ref") or p.get("target") or "?").split(" ")[-1]
        if p.get("op") == "create_node":
            title = op.get("title") or p.get("target") or "?"
            number = f" {ref}" if p.get("ref") else ""
            target = target_of(str(op.get("body") or ""))
            parts.append(
                f"new{number} «{title}»" + (f" (Zielanwendung {target})" if target else "")
            )
            continue
        change = p.get("change") or {}
        fields = [_field_value(k, change[k]) for k in sorted(change)] or [p.get("op") or "?"]
        parts.append(f"{ref}: {', '.join(fields)}")
    return "; ".join(parts)


LISTING_LINES = 12
LISTING_LINE = 200


def listing(rows: list[Any], language: str) -> str:
    """The prepared sets as the hub writes them under the answer: every new task with its title
    and the lines of its text, every other change as in ``summary_of``. Live 2026-11-20 the answer
    described "detailed descriptions" and quoted texts the set did not have; the person reads here
    what the click would write."""
    de = language == "de"
    out: list[str] = []
    for row in rows:
        for p in row.preview or []:
            ops = row.operations or []
            op = ops[p["index"]] if p.get("index", len(ops)) < len(ops) else {}
            if p.get("op") == "create_node":
                title = op.get("title") or p.get("target") or "?"
                kind = op.get("kind") or "task"
                label = (
                    {"task": "neue Aufgabe", "topic": "neues Thema", "note": "neue Notiz"}.get(
                        kind, f"neu ({kind})"
                    )
                    if de
                    else f"new {kind}"
                )
                out.append(f"- {label} «{title}»")
                lines = [ln.rstrip() for ln in str(op.get("body") or "").splitlines() if ln.strip()]
                for ln in lines[:LISTING_LINES]:
                    out.append(
                        "  " + (ln if len(ln) <= LISTING_LINE else ln[: LISTING_LINE - 1] + "…")
                    )
                if len(lines) > LISTING_LINES:
                    more = len(lines) - LISTING_LINES
                    out.append(f"  … {more} " + ("weitere Zeilen" if de else "more lines"))
                continue
            ref = str(p.get("ref") or p.get("target") or "?").split(" ")[-1]
            change = p.get("change") or {}
            fields = [_field_value(k, change[k]) for k in sorted(change)] or [p.get("op") or "?"]
            out.append(f"- {ref}: {', '.join(fields)}")
    if not out:
        return ""
    head = (
        "**Vorbereitet, wartet auf deinen Klick (vom Hub, so wie es angelegt würde):**"
        if de
        else "**Prepared, waiting for your click (from the hub, as it would be written):**"
    )
    return "\n\n" + head + "\n" + "\n".join(out)


def _field_value(key: str, value: Any) -> str:
    """One changed field with its new value; a text (body) by its name only."""
    if isinstance(value, list) and len(value) == 2 and key not in ("body",):
        old, new = value
        return f"{key} «{new}»" if key == "title" else f"{key} {old} → {new}"
    return key


def changes_text(row: Any) -> bool:
    """Does the set write a text (a body), not only titles or states?"""
    for p in row.preview or []:
        if "body" in (p.get("change") or {}):
            return True
        op = (row.operations or [])[p["index"]] if p["index"] < len(row.operations or []) else {}
        if p.get("op") == "create_node" and op.get("body"):
            return True
    return False


def _touched(operations: list[dict[str, Any]]) -> set[str]:
    """The existing nodes a set names and the titles it creates: two sets overlap when they share
    one of them."""
    out: set[str] = set()
    for op in operations:
        for key in ("node", "parent", "other", "old", "by"):
            ref = op.get(key)
            if isinstance(ref, str) and UUID_RE.match(ref):
                out.add(ref.lower())
        if op.get("op") == "create_node" and isinstance(op.get("title"), str):
            out.add("title:" + op["title"].strip().lower())
    return out


def view(row: ProjectChangeSet) -> dict[str, Any]:
    return {
        "id": str(row.id),
        "project_id": str(row.project_id),
        "task_id": str(row.task_id) if row.task_id else None,
        "title": row.title,
        "summary": row.summary,
        "operations": row.operations,
        "preview": row.preview,
        "dependants": {str(k): sorted(v) for k, v in dependants(row.operations).items() if v},
        "status": row.status,
        "applied": row.applied,
        "result": row.result,
        "created_at": row.created_at.isoformat() if row.created_at else None,
        "decided_at": row.decided_at.isoformat() if row.decided_at else None,
    }


class ChangeSetError(Exception):
    def __init__(self, status: int, code: str, message: str) -> None:
        super().__init__(message)
        self.status, self.code, self.message = status, code, message


class ChangeSetStore:
    def __init__(
        self, sessionmaker: async_sessionmaker[AsyncSession], default_zone: str = "UTC"
    ) -> None:
        self.sm = sessionmaker
        self.default_zone = default_zone

    async def propose(
        self,
        user_id: uuid.UUID,
        project_id: uuid.UUID,
        operations: Any,
        *,
        title: str = "",
        summary: str = "",
        task_id: uuid.UUID | None = None,
        agent_plan: bool = False,
    ) -> ProjectChangeSet:
        """Check the set, try it in a transaction that is rolled back, and keep it with its preview.
        A set that cannot be applied is refused here, with the operation and the reason.

        An operation that changes nothing (a title the node already has, a status it already has)
        is left out, and a set of nothing but those is refused with what to write instead (live
        2026-11-14: "Aufgaben verfeinern" set three titles to what they were, was applied, and the
        tasks stayed as short as before). ``row.unchanged`` names what was left out. An earlier
        set of the same task that is still waiting and touches the same nodes is replaced by this
        one."""
        async with self.sm() as db:
            project = await get_project(db, user_id, project_id)  # the person's own
            title = project.title
        nodes = await ProjectStore(self.sm).tree(user_id, project_id)
        ops = check_operations(
            resolve_numbers(normalize_operations(operations, project_id), nodes, title)
        )
        if agent_plan:
            ops = await plan_checked(self.sm, ops)
        actor = Actor("agent", agent_task_id=task_id)
        results = await self._try(user_id, project_id, ops, actor)
        idle = [r for r in results if r.changes_nothing]
        if idle and len(idle) == len(results):
            raise SetError(
                idle[0].index,
                "this set changes nothing: every node already has the title or status it names. "
                "To rewrite a task, give update_node its complete new text in `body` (or "
                "`body_append` for lines to add), not only the title it already has",
            )
        unchanged = [f"{r.index + 1}. {r.op} {r.target or ''}".strip() for r in idle]
        if idle:
            skip = {r.index for r in idle}
            ops = [op for i, op in enumerate(ops) if i not in skip]
            results = await self._try(user_id, project_id, ops, actor)
        preview = [
            {
                "index": r.index,
                "op": r.op,
                "target": r.target,
                "ref": r.ref,
                "change": r.change,
                "lines": r.lines,
            }
            for r in results
        ]
        async with self.sm() as db:
            row = ProjectChangeSet(
                id=uuid.uuid4(), user_id=user_id, application="project", project_id=project_id,
                task_id=task_id, title=title[:200], summary=summary[:2000], operations=ops,
                preview=preview, content_hash=content_hash(str(project_id), ops), status=PENDING,
                applied=[],
            )  # fmt: skip
            db.add(row)
            await db.flush()
            project = await get_project(db, user_id, project_id, lock=True)
            await EventLog.record(
                db, user_id, project, "changeset.proposed", actor=actor,
                payload={"change_set": str(row.id), "operations": len(ops), "title": row.title},
            )  # fmt: skip
            if task_id is not None:
                await self._supersede(db, user_id, project, row)
            await db.commit()
            row.unchanged = unchanged  # type: ignore[attr-defined]  # for the tool's answer only
            return row

    async def _try(
        self, user_id: uuid.UUID, project_id: uuid.UUID, ops: list[dict[str, Any]], actor: Actor
    ) -> list[OpResult]:
        async with self.sm() as db:
            await get_project(db, user_id, project_id)  # the project is the person's own
            try:
                results, _ = await run_operations(
                    db, user_id, project_id, ops, set(range(len(ops))), actor, self.default_zone
                )
            finally:
                await db.rollback()  # a try never changes anything
        return results

    async def _supersede(
        self, db: AsyncSession, user_id: uuid.UUID, project: Any, row: ProjectChangeSet
    ) -> None:
        """A run that proposes again ("V2") means its new set, not both: its earlier waiting sets
        that touch one of the same nodes are refused as replaced (live 2026-11-14: two equal sets
        waited, the person applied one, the other failed on its stale revision)."""
        mine = _touched(row.operations)
        older = await db.scalars(
            select(ProjectChangeSet).where(
                ProjectChangeSet.task_id == row.task_id,
                ProjectChangeSet.id != row.id,
                ProjectChangeSet.status == PENDING,
                owned(ProjectChangeSet, user_id),
            )
        )
        for other in older:
            if not mine & _touched(other.operations):
                continue
            other.status, other.decided_at = REFUSED, clock.hub_now()
            other.result = {"superseded_by": str(row.id)}
            await EventLog.record(
                db, user_id, project, "changeset.refused",
                actor=Actor("agent", agent_task_id=row.task_id),
                payload={"change_set": str(other.id), "superseded_by": str(row.id)},
            )  # fmt: skip

    async def get(self, user_id: uuid.UUID, set_id: uuid.UUID) -> ProjectChangeSet:
        async with self.sm() as db:
            row = await db.scalar(
                select(ProjectChangeSet).where(
                    ProjectChangeSet.id == set_id, owned(ProjectChangeSet, user_id)
                )
            )
        if row is None:
            raise ChangeSetError(404, "not_found", "there is no such change set")
        return row

    async def for_task(self, user_id: uuid.UUID, task_id: uuid.UUID) -> list[ProjectChangeSet]:
        async with self.sm() as db:
            rows = await db.scalars(
                select(ProjectChangeSet)
                .where(ProjectChangeSet.task_id == task_id, owned(ProjectChangeSet, user_id))
                .order_by(ProjectChangeSet.created_at, ProjectChangeSet.id)
            )
            return list(rows)

    async def for_project(
        self, user_id: uuid.UUID, project_id: uuid.UUID
    ) -> list[ProjectChangeSet]:
        async with self.sm() as db:
            rows = await db.scalars(
                select(ProjectChangeSet)
                .where(ProjectChangeSet.project_id == project_id, owned(ProjectChangeSet, user_id))
                .order_by(ProjectChangeSet.created_at.desc())
            )
            return list(rows)

    async def _claim(self, user_id: uuid.UUID, row: ProjectChangeSet) -> None:
        if row.content_hash != content_hash(str(row.project_id), row.operations):
            raise ChangeSetError(409, "tampered", "the change set no longer matches its hash")
        async with self.sm() as db:
            taken = await db.execute(
                update(ProjectChangeSet)
                .where(
                    ProjectChangeSet.id == row.id,
                    ProjectChangeSet.user_id == user_id,
                    ProjectChangeSet.status == PENDING,
                )
                .values(status="running")
            )
            await db.commit()
        if taken.rowcount != 1:
            raise ChangeSetError(409, "decided", "this change set was already decided")

    async def apply(
        self, user_id: uuid.UUID, set_id: uuid.UUID, selection: list[int] | None = None
    ) -> ProjectChangeSet:
        """Apply the ticked operations (all when ``selection`` is None) in one transaction, all or
        nothing. An item that needs a name created by an unticked item is dropped and said so."""
        row = await self.get(user_id, set_id)
        ops = row.operations
        wanted = set(range(len(ops))) if selection is None else set(selection)
        if not wanted or any(i < 0 or i >= len(ops) for i in wanted):
            raise ChangeSetError(422, "invalid_selection", "tick at least one item of the set")
        chosen, dropped = closure(wanted, ops)
        if not chosen:
            raise ChangeSetError(
                422, "invalid_selection", "nothing is left after the items it needs"
            )
        await self._claim(user_id, row)
        actor = Actor("person", agent_task_id=row.task_id, change_set_id=row.id)
        group = uuid.uuid4()
        try:
            async with self.sm() as db:
                results, _ = await run_operations(
                    db, user_id, row.project_id, ops, chosen, actor, self.default_zone
                )
                project = await get_project(db, user_id, row.project_id, lock=True)
                await EventLog.record(
                    db, user_id, project, "changeset.applied", actor=actor, group_id=group,
                    payload={
                        "change_set": str(row.id),
                        "applied": sorted(chosen),
                        "dropped": {str(k): v for k, v in dropped.items()},
                    },
                )  # fmt: skip
                await db.execute(
                    update(ProjectChangeSet)
                    .where(ProjectChangeSet.id == row.id)
                    .values(
                        status=APPLIED,
                        applied=sorted(chosen),
                        decided_at=clock.hub_now(),
                        result={
                            "operations": len(results),
                            "dropped": {str(k): v for k, v in dropped.items()},
                        },
                    )
                )
                await db.commit()
        except (SetError, ProjectError) as exc:
            reason = getattr(exc, "reason", str(exc))
            index = getattr(exc, "index", None)
            await self._finish(
                user_id, row, FAILED, {"error": reason, "operation": index}, "changeset.refused"
            )
            raise ChangeSetError(
                409 if "revision" in str(exc) else 422, "not_applied",
                f"nothing was applied: {exc}",
            ) from exc  # fmt: skip
        return await self.get(user_id, set_id)

    async def refuse(self, user_id: uuid.UUID, set_id: uuid.UUID) -> ProjectChangeSet:
        row = await self.get(user_id, set_id)
        await self._claim(user_id, row)
        await self._finish(user_id, row, REFUSED, {}, "changeset.refused")
        return await self.get(user_id, set_id)

    async def _finish(
        self,
        user_id: uuid.UUID,
        row: ProjectChangeSet,
        status: str,
        result: dict[str, Any],
        kind: str,
    ) -> None:
        async with self.sm() as db:
            project = await get_project(db, user_id, row.project_id, lock=True)
            await EventLog.record(
                db, user_id, project, kind, actor=Actor("person", change_set_id=row.id),
                payload={"change_set": str(row.id), **result},
            )  # fmt: skip
            await db.execute(
                update(ProjectChangeSet)
                .where(ProjectChangeSet.id == row.id)
                .values(status=status, result=result, decided_at=clock.hub_now())
            )
            await db.commit()
