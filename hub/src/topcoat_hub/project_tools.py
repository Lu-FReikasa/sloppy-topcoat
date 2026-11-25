"""The project agent's tools (brief PPH-01-planning-app, 01b; rework 2026-10-27).

A virtual node of the hub, like the notes and Topcoat itself: no machine stands behind it. The
tools write only rows of ``project_*``, the application's own data. They never delete: leaving
the working set is a status (``dropped``). The tools are offered only to a caller whose
application is ``project``; any other caller gets neither the tools nor a way to call them.

The tools cannot set a project's scope: that is the person's grant (``project_scope``) and is
set from the person's own page. Every call is scoped to the caller (``user_id``) by
:class:`ProjectStore`. A project or node of another person answers the same as a missing one.
"""

import json
import uuid
from typing import Any

import jsonschema
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from topcoat_hub import project_refs
from topcoat_hub.message_service import MessageService
from topcoat_hub.models import User
from topcoat_hub.project_base import user_zone
from topcoat_hub.project_changes import READ_ONLY, ChangeStore
from topcoat_hub.project_changesets import (
    EXAMPLES,
    MAX_OPERATIONS,
    SCHEMAS,
    ChangeSetStore,
    SetError,
    operation_item_schema,
    plan_checked,
)
from topcoat_hub.project_events import Actor
from topcoat_hub.project_plan import (
    PLAN_DESCRIPTION,
    PLAN_SCHEMA,
    compile_plan,
    repair_plan,
    schema_problems,
)
from topcoat_hub.project_read_tools import READ_SPECS, run_read
from topcoat_hub.project_runs import (
    MAX_RUNS,
    RunError,
    batch_view,
    check_right,
    mode_of,
    target_of,
    unfinished_blockers,
)
from topcoat_hub.project_store import Conflict, Invalid, NotFound, ProjectError, ProjectStore
from topcoat_hub.project_views import node_view, project_view
from topcoat_hub.project_write_tools import WRITE_NAMES, WRITE_SPECS, run_write
from topcoat_hub.project_write_tools import preview as write_preview
from topcoat_hub.research_profile import DEPTH_GUIDE
from topcoat_hub.tools import Caller, ToolError

APPLICATION = "project"
# nodes per page of project.get: a page must fit the observation the model reads in one turn
PROJECT_PAGE = 12
PROJECT_NODE = "project"
PROJECT_NODE_ID = uuid.UUID("00000000-0000-4000-8000-00000000a0d5")  # not the GitHub id (a0d4)

_UUID = {
    "type": "string",
    "pattern": "^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$",
}
_TEXT = {"type": "string", "maxLength": 4000}
_TITLE = {"type": "string", "minLength": 1, "maxLength": 200}
_PROJECT_STATUS = {"enum": ["active", "paused", "done", "dropped"]}
_TASK_STATUS = {"enum": ["todo", "in_progress", "in_review", "blocked", "done", "dropped"]}
_TIME = {"type": ["string", "null"], "maxLength": 40}
# the task fields beyond title and body (project_fields.py); null clears a date, a colour, a rule
_TASK_FIELDS = {
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
FIELD_NAMES = (*_TASK_FIELDS, "as_of", "origin_app")
_DEPENDS = {"type": "array", "items": _UUID, "maxItems": 100}
_REVISION = {"type": "integer", "minimum": 1}


def _schema(properties: dict[str, Any], required: list[str]) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": properties,
        "required": required,
        "additionalProperties": False,
    }


SPECS: dict[str, dict[str, Any]] = {
    "project.list": {
        "description": "The projects of the user: id, title, status, revision.",
        "schema": _schema({}, []),
    },
    "project.get": {
        "description": "One project with its tree (topics, tasks and reports, in the order of "
        "their numbers), a page at a time: nodes_total says how many there are, and next_from "
        "is where the next page starts (pass it as from). Each node carries its ref (the "
        "project's title and its number) and its revision, not its body: read a node with "
        "project.node.get. Read it before you change anything.",
        "schema": _schema(
            {
                "project_id": _UUID,
                "from": {"type": "integer", "minimum": 0},
                "limit": {"type": "integer", "minimum": 1, "maximum": 30},
            },
            ["project_id"],
        ),
    },
    "project.update": {
        "description": "Change a project's title, description or status. Set the status to "
        "'dropped' to archive it. Nothing is deleted. Needs the revision you read.",
        "schema": _schema(
            {
                "project_id": _UUID,
                "revision": _REVISION,
                "title": _TITLE,
                "description": _TEXT,
                "status": _PROJECT_STATUS,
            },
            ["project_id", "revision"],
        ),
    },
    "project.node.create": {
        "description": "Add a topic (a folder) or a task to a project. parent_id puts it below "
        "another topic or task of the same project; depends_on lists tasks of the same project it "
        "waits for. The node gets the next number under its parent and keeps it. One node only: "
        "for a topic with tasks, or any request for several new nodes, use "
        "project.changeset.propose (a create_node operation each).",
        "schema": _schema(
            {
                "project_id": _UUID,
                "kind": {"enum": ["topic", "task"]},
                "title": _TITLE,
                "body": _TEXT,
                "parent_id": _UUID,
                "depends_on": _DEPENDS,
                **_TASK_FIELDS,
            },
            ["project_id", "kind", "title"],
        ),
    },
    "project.node.update": {
        "description": "Change a node's title, body, status (tasks only), dependencies, dates "
        "(ISO 8601 with an offset), priority 0-5, percent done, colour, estimate in minutes, "
        "repeat rule or milestone flag. Only the fields you give change. Set a task's status to "
        "'in_review' when the work is finished: the person closes it. Set it to 'dropped' to "
        "archive it. Needs the revision you read.",
        "schema": _schema(
            {
                "node_id": _UUID,
                "revision": _REVISION,
                "title": _TITLE,
                "body": _TEXT,
                "status": _TASK_STATUS,
                "depends_on": _DEPENDS,
                **_TASK_FIELDS,
            },
            ["node_id", "revision"],
        ),
    },
    "project.node.move": {
        "description": "Move a node below another topic or task of the same project, or to the top "
        "when parent_id is null. The node keeps its number. Needs the revision you read.",
        "schema": _schema(
            {
                "node_id": _UUID,
                "revision": _REVISION,
                "parent_id": {"anyOf": [_UUID, {"type": "null"}]},
            },
            ["node_id", "revision", "parent_id"],
        ),
    },
    "project.request.send": {
        "description": "Ask another application for something inside a project: research for a "
        "report, development for a read-only check of the code (is a claim true of the repository, "
        "which file and line; nothing changes, no branch) or for a branch or prototype's report, "
        "administration for released data. Give the claims as questions and say what each answer "
        "must show. You decide the whole request: the topic, the questions, how deep, how many "
        "sources and hosts at least, what must be covered and what you expect in the answer. "
        "Only what "
        "the person granted for this project is allowed; some requests wait for the person to "
        "confirm them. The answer arrives as a message of the same chain; you are told when it is "
        "in. Sending the same request again is a new request: look at the open ones first. "
        + DEPTH_GUIDE,
        "schema": _schema(
            {
                "project_id": _UUID,
                "target": {"enum": ["research", "dev", "admin"]},
                "topic": {"type": "string", "minLength": 1, "maxLength": 300},
                "questions": {"type": "array", "items": {"type": "string", "maxLength": 500}},
                "depth": {
                    "type": "string",
                    "maxLength": 200,
                    "description": "quick, default or thorough (Research), see above",
                },
                "min_sources": {"type": "integer", "minimum": 0, "maximum": 50},
                "min_hosts": {"type": "integer", "minimum": 0, "maximum": 50},
                "source_kinds": {"type": "array", "items": {"type": "string", "maxLength": 500}},
                "must_cover": {"type": "array", "items": {"type": "string", "maxLength": 500}},
                "expected": {"type": "array", "items": {"type": "string", "maxLength": 500}},
                "format": {"enum": ["brief", "guide", "comparison"]},
                "parent_id": _UUID,
            },
            ["project_id", "target", "topic"],
        ),
    },
    "project.order.draft": {
        "description": "Order Development to build a prototype or fix one on ONE branch below "
        "topcoat/ of a repository. You draft the order; you cannot release it. If the person gave "
        "a mandate for exactly this repository, branch and action, the order goes out by itself; "
        "otherwise it appears as a card and waits for the person's click. Describe the goal, the "
        "behaviour you expect as testable statements, the constraints, what is out of scope, and "
        "how you will judge the result (acceptance). A second branch is a second order and "
        "needs its own release.",
        "schema": _schema(
            {
                "project_id": _UUID,
                "action": {"enum": ["create_prototype", "fix"]},
                "repo": {"type": "string", "minLength": 1, "maxLength": 100},
                "branch": {"type": "string", "minLength": 1, "maxLength": 200},
                "goal": {"type": "string", "minLength": 1, "maxLength": 2000},
                "expected": {"type": "array", "items": {"type": "string", "maxLength": 500}},
                "constraints": {"type": "array", "items": {"type": "string", "maxLength": 500}},
                "out_of_scope": {"type": "array", "items": {"type": "string", "maxLength": 500}},
                "acceptance": {"type": "array", "items": {"type": "string", "maxLength": 500}},
                "parent_id": _UUID,
            },
            ["project_id", "action", "repo", "branch", "goal"],
        ),
    },
    "project.report.add": {
        "description": "Write a report into a project: a note, a question for the person, a "
        "decision, or a document. Put a question here instead of in the chat text; the person "
        "answers it on "
        "the project page. parent_id puts it below a topic or task.",
        "schema": _schema(
            {
                "project_id": _UUID,
                "report_kind": {"enum": ["note", "question", "decision", "document", "fact"]},
                "as_of": _TIME,
                "origin_app": {"type": "string", "maxLength": 40},
                "title": _TITLE,
                "body": _TEXT,
                "parent_id": _UUID,
            },
            ["project_id", "report_kind", "title"],
        ),
    },
}


SPECS.update(READ_SPECS)  # the reading tools of 04d (project_read_tools.py)
SPECS.update(WRITE_SPECS)  # the single-step writing tools of 04g (project_write_tools.py)
# the one writing tool that is never "prepared" by the chat: it *is* the preparation (04g)
PLAN_TOOL = "project.plan.propose"  # a topic and its tasks as fixed fields (project_plan.py)
SPECS[PLAN_TOOL] = {"description": PLAN_DESCRIPTION, "schema": PLAN_SCHEMA}
PROPOSE = frozenset({"project.changeset.propose", PLAN_TOOL})
SPECS["project.changeset.propose"] = {
    "description": (
        f"Propose a change set: up to {MAX_OPERATIONS} operations that change a project together, "
        "which the person looks at (with a preview of what each would change), may untick in "
        "parts and applies with one click, all or nothing. Nothing changes until then. Use it for "
        "more than a couple of changes, for example a whole tree of tasks. ONE change (a status, "
        "a title, a date) is simpler with project.node.update: that is prepared for the person's "
        "click as well. Operations (field op): " + ", ".join(SCHEMAS) + ". The node is called "
        "`node` in an operation, not `node_id`. Examples: "
        + " ".join(json.dumps(EXAMPLES[n]) for n in ("set_status", "update_node", "create_node"))
        + ' Name a node by its id, or by a local name (`"local": "n1"` on a create_node, then '
        '`"parent": "n1"`) for something the set creates earlier. Read a node before you change '
        "it and give the revision you read; update_node also takes body_append and body_replace "
        "{old, new} (old must occur exactly once) instead of rewriting the text. Search before "
        "you create."
    ),
    "schema": _schema(
        {
            "project_id": _UUID,
            "title": {"type": "string", "maxLength": 200},
            "summary": {"type": "string", "maxLength": 2000},
            "operations": {
                "type": "array",
                "minItems": 1,
                "maxItems": MAX_OPERATIONS,
                "items": operation_item_schema(),
            },
        },
        ["project_id", "operations"],
    ),
}


# A batch: several task nodes run one after another (project_runs.py). The planner only proposes
# it; the card in the chat waits for the person's click, and that click is the one start.
BATCH_TOOL = "project.batch.propose"
BATCH_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["project_id", "node_ids", "max_runs"],
    "properties": {
        "project_id": _UUID,
        "node_ids": {
            "type": "array",
            "minItems": 1,
            "maxItems": 50,
            "uniqueItems": True,
            "items": _UUID,
            "description": "the task nodes in run order, by id; each open (todo or blocked)",
        },
        "max_runs": {
            "type": "integer",
            "minimum": 1,
            "maximum": MAX_RUNS,
            "description": "the most runs the batch may start before it stops",
        },
    },
}
SPECS[BATCH_TOOL] = {
    "description": "Propose a batch: task nodes of one project, run one after another in the "
    "order given, as long as each result fits its form. This is also how a single task is started "
    'or started again ("stoß die Aufgabe an"): a batch of that one task; a blocked task goes '
    "back into the queue when the person approves. Nothing starts until the person clicks the "
    "card; say that the batch waits for the click. Give the tasks in order (ids or their numbers, "
    "e.g. 2.1) and a limit of runs.",
    "schema": BATCH_SCHEMA,
}


# --- tiers (04d) -------------------------------------------------------------------------------
# All tools are named, schema-validated actions. Offering all of them costs about 6000 tokens in
# every turn, which a small local model feels. So the agent is offered the ten it uses all the time
# with their schemas, and reaches the rest in two steps: ``project.more.help`` lists them (or shows
# the schema of one), ``project.more.run`` calls one by name. The call is unpacked before anything
# else happens, so the schema check, the preparation in a chat and the lines of the Akte are those
# of the tool that is run, and the security baseline is the one of the single tools.

CORE = (
    "project.list",
    "project.get",
    "project.node.search",
    "project.node.get",
    "project.agenda",
    "project.node.create",
    "project.node.update",
    "project.node.complete",
    "project.changeset.propose",
    "project.plan.propose",
    "project.batch.propose",
    "project.report.add",
)
EXTENDED = tuple(name for name in SPECS if name not in CORE)
MORE_HELP = "project.more.help"
MORE_RUN = "project.more.run"
META_SPECS: dict[str, dict[str, Any]] = {
    MORE_HELP: {
        "description": "The less common project tools are not listed with their schema. Without "
        "a name this lists them (name and what it does); with a name it shows that tool's input "
        "schema. Use project.more.run to call one.",
        "schema": _schema({"name": {"enum": list(EXTENDED)}}, []),
    },
    MORE_RUN: {
        "description": "Call one of the less common project tools by name with its input (see "
        "project.more.help). It behaves exactly like calling that tool directly: in a chat the "
        "change is prepared, not made.",
        "schema": _schema(
            {"name": {"enum": list(EXTENDED)}, "args": {"type": "object"}}, ["name", "args"]
        ),
    },
}
SPECS_ALL = {**SPECS, **META_SPECS}


def _entry(name: str, spec: dict[str, Any]) -> dict[str, Any]:
    return {
        "node_id": str(PROJECT_NODE_ID),
        "node": PROJECT_NODE,
        "capability": name,
        "description": spec["description"],
        "input_schema": spec["schema"],
    }


def tool_entries() -> list[dict[str, Any]]:
    """The tools as the agent is offered them (the same shape as the other virtual tools): the core
    tiers and the two that reach the rest."""
    return [_entry(name, SPECS[name]) for name in CORE] + [
        _entry(name, spec) for name, spec in META_SPECS.items()
    ]


def all_tool_entries() -> list[dict[str, Any]]:
    """Every tool, whatever the tier (what the tests and the help tool look at)."""
    return [_entry(name, spec) for name, spec in SPECS.items()]


def _first_line(text: str) -> str:
    first = text.split(". ")[0].strip()
    return first if first.endswith(".") else first + "."


class ProjectTools:
    def __init__(
        self, sessionmaker: async_sessionmaker[AsyncSession], hub_timezone: str = "UTC"
    ) -> None:
        self.sm = sessionmaker
        self.hub_timezone = hub_timezone
        # the hub's agent service, set once it exists: sent requests become its tasks
        self.agents: Any = None
        # the run service (project_runs.py), set once it exists: the person's click starts a batch
        self.runs: Any = None

    async def offer(self, caller: Caller) -> list[dict[str, Any]]:
        """The tools, for the project agent only."""
        return tool_entries() if caller.application == APPLICATION else []

    async def _resolve_refs(
        self, caller: Caller, capability: str, tool_input: dict[str, Any]
    ) -> dict[str, Any]:
        """Task numbers, titles and id beginnings resolved to ids (project_refs.py). Only when the
        input holds a reference that is no id: most calls cost no lookup."""
        nested = tool_input.get("args") if isinstance(tool_input.get("args"), dict) else None
        if not project_refs.needs_lookup(tool_input) and not (
            nested and project_refs.needs_lookup(nested)
        ):
            return tool_input
        store = ProjectStore(self.sm)
        projects = await store.list_projects(caller.user_id)

        async def nodes_of(project_id: str) -> list[Any]:
            return await store.tree(caller.user_id, uuid.UUID(project_id))

        out = await project_refs.resolve(tool_input, projects, nodes_of)
        if nested is not None:  # project.more.run carries the call in "args"
            out["args"] = await project_refs.resolve(nested, projects, nodes_of)
        schema = SPECS_ALL[capability]["schema"]
        if "project_id" in out and "project_id" not in (schema.get("properties") or {}):
            # given to say where the node is, not an input of this tool: it served its purpose
            out.pop("project_id")
        return out

    async def run_tool(
        self, caller: Caller, capability: str, tool_input: dict[str, Any]
    ) -> dict[str, Any]:
        if caller.application != APPLICATION or capability not in SPECS_ALL:
            raise ToolError(404, "unknown_capability", f"no such capability: {capability}")
        tool_input = await self._resolve_refs(caller, capability, tool_input)
        if capability == PLAN_TOOL:  # every problem of a plan at once, not the first one
            tool_input = repair_plan(tool_input)
            problems = schema_problems(tool_input)
            if problems:
                raise ToolError(422, "invalid_input", "; ".join(problems))
        else:
            try:
                jsonschema.validate(tool_input, SPECS_ALL[capability]["schema"])
            except jsonschema.ValidationError as exc:
                raise ToolError(422, "invalid_input", exc.message) from exc
        if capability == MORE_HELP:
            return {
                "ok": True,
                "output": _more_help(tool_input.get("name")),
                "at": None,
                "bytes": 0,
            }
        if capability == MORE_RUN:
            capability, tool_input = tool_input["name"], tool_input["args"]
            try:
                jsonschema.validate(tool_input, SPECS[capability]["schema"])
            except jsonschema.ValidationError as exc:
                raise ToolError(422, "invalid_input", f"{capability}: {exc.message}") from exc
        # who made the change: the agent in a run, the person when they confirm a prepared one
        actor = (
            Actor("agent", agent_task_id=caller.task_id)
            if caller.via == "agent"
            else Actor("person", agent_task_id=caller.task_id)
        )
        store = ProjectStore(self.sm, actor, self.hub_timezone)
        try:
            if capability == PLAN_TOOL:
                output = await self._propose_plan(store, caller, tool_input)
            elif capability == BATCH_TOOL:
                output = await self._batch(store, caller, tool_input)
            elif capability in PROPOSE:
                output = await self._propose(store, caller, tool_input)
            elif capability in READ_SPECS:
                zone = await user_zone(self.sm, caller.user_id, self.hub_timezone)
                output = await run_read(store, caller.user_id, capability, tool_input, zone=zone)
            elif caller.stage_writes and capability not in READ_ONLY:
                output = await self._stage(store, caller, capability, tool_input)
            elif capability in WRITE_NAMES:
                zone = await user_zone(self.sm, caller.user_id, self.hub_timezone)
                output = await run_write(store, caller.user_id, capability, tool_input, zone=zone)
            else:
                output = await _dispatch(store, caller, capability, tool_input, self.agents)
        except NotFound as exc:
            raise ToolError(404, "not_found", "there is no such item") from exc
        except Conflict as exc:
            raise ToolError(
                409, "conflict", "the item changed since it was read; read it again"
            ) from exc
        except Invalid as exc:
            raise ToolError(422, "invalid_input", str(exc)) from exc
        except ProjectError as exc:  # pragma: no cover - a new subclass without a mapping
            raise ToolError(422, "invalid_input", str(exc)) from exc
        return {"ok": True, "output": output, "at": None, "bytes": len(str(output))}

    async def _propose_plan(
        self, store: ProjectStore, caller: Caller, args: dict[str, Any]
    ) -> dict[str, Any]:
        """A plan: its topic and tasks become the operations, then the same check as any set."""
        async with self.sm() as db:
            role = await db.scalar(select(User.role).where(User.id == caller.user_id))
        try:
            for task in args["tasks"]:
                check_right(task["application"], role == "admin")
        except RunError as exc:
            raise ToolError(422, "invalid_input", str(exc)) from exc
        operations, problems = compile_plan(args)
        if problems:
            raise ToolError(422, "invalid_input", "; ".join(problems))
        return await self._propose(
            store,
            caller,
            {
                "project_id": args["project_id"],
                "operations": operations,
                "title": args["title"],
                "summary": args.get("summary", ""),
            },
        )

    async def _propose(
        self, store: ProjectStore, caller: Caller, args: dict[str, Any]
    ) -> dict[str, Any]:
        """Check a set, try it, keep it for the person's click. Never staged, never applied here."""
        try:
            row = await ChangeSetStore(self.sm, self.hub_timezone).propose(
                caller.user_id,
                uuid.UUID(args["project_id"]),
                args["operations"],
                title=args.get("title", ""),
                summary=args.get("summary", ""),
                task_id=caller.task_id,
                agent_plan=True,
            )
        except SetError as exc:
            raise ToolError(422, "invalid_input", str(exc)) from exc
        out: dict[str, Any] = {
            "proposed": True,
            "applied": False,
            "change_set_id": str(row.id),
            "operations": len(row.operations),
            "note": "Prepared, not applied. The person sees the whole set with a preview, may "
            "untick items and applies the rest with one click. It replaces your earlier waiting "
            "set for the same nodes. Say what you prepared and that it waits.",
            "preview": [
                {
                    "i": p["index"] + 1,
                    "op": p["op"],
                    "target": p["target"],
                    "changes": sorted(p.get("change") or {}),
                }
                for p in row.preview[:40]
            ],
        }
        unchanged = getattr(row, "unchanged", [])
        if unchanged:
            out["left_out"] = {
                "operations": unchanged,
                "why": "they change nothing (same title or status as now); to rewrite a task "
                "put its complete new text into `body`",
            }
        return out

    async def _plan_checked_single(self, capability: str, args: dict[str, Any]) -> dict[str, Any]:
        """``args`` of a single task create or rewrite after the checks of a plan, with the lines
        a run needs completed in its body; refused like a set that fails them."""
        if capability == "project.node.create" and args.get("kind", "task") == "task":
            op = {"op": "create_node", "kind": "task", "title": args.get("title", "")}
            op["body"] = args.get("body") or ""
        elif capability == "project.node.update" and isinstance(args.get("body"), str):
            op = {"op": "update_node", "body": args["body"]}
        else:
            return args
        try:
            [checked] = await plan_checked(self.sm, [op])
        except SetError as exc:
            raise ToolError(422, "invalid_input", str(exc).replace("operation 1: ", "")) from exc
        return {**args, "body": checked["body"]}

    async def _stage(
        self, store: ProjectStore, caller: Caller, capability: str, args: dict[str, Any]
    ) -> dict[str, Any]:
        """A writing call of a chat run: checked as far as it can be without writing (the thing
        exists, the revision is the current one), then kept for the person's click. A task it
        creates or rewrites passes the checks of a plan first (project_changesets.plan_checked)."""
        args = await self._plan_checked_single(capability, args)
        preview = await _preview(store, caller.user_id, capability, args)
        row = await ChangeStore(self.sm).stage(
            caller.user_id, caller.task_id, capability, args, preview
        )
        return {
            "staged": True,
            "change_id": str(row.id),
            "applied": False,
            "note": "Prepared, not applied. The person confirms or refuses this change with a "
            "button in the chat. Say exactly what you prepared and that it waits for the click.",
            "preview": preview,
        }

    async def _batch(
        self, store: ProjectStore, caller: Caller, args: dict[str, Any]
    ) -> dict[str, Any]:
        """A batch: checked here (open tasks of the project, in their order), then either kept
        for the person's click or, when the click itself is the call (the apply of a prepared
        card), started. A chat, a run or an API key never starts one: they only prepare the card."""
        project = await store.get_project(caller.user_id, uuid.UUID(args["project_id"]))
        node_ids = [uuid.UUID(n) for n in args["node_ids"]]
        titles: list[str] = []
        apps: list[dict[str, Any]] = []
        problems: list[str] = []
        async with self.sm() as db:
            waits = await unfinished_blockers(db, caller.user_id, node_ids)
        for nid in node_ids:
            node = await store.get_node(caller.user_id, nid)
            if node.project_id != project.id:
                raise NotFound("node")
            if node.kind != "task":
                problems.append(f"{node.number} {node.title!r} is not a task")
            elif node.status not in ("todo", "blocked"):
                problems.append(f"{node.number} {node.title!r} is {node.status}, not open")
            line = f"{node_view(node, project.title)['ref']} {node.title}"
            if waits[str(nid)]:  # the card names what the task waits for, before the click
                numbers = ", ".join(w["number"] for w in waits[str(nid)])
                line += f" (wartet auf {numbers})"
            titles.append(line)
            apps.append(
                {"application": target_of(node.body or ""), "mode": mode_of(node.body or "")}
            )
        if problems:
            raise ToolError(422, "invalid_input", "; ".join(problems))
        if caller.via == "gui" and not caller.stage_writes:
            if self.runs is None:  # pragma: no cover - the hub always sets the run service
                raise ToolError(503, "unavailable", "the run service is not ready")
            try:
                batch = await self.runs.create_batch(
                    caller.user_id, project.id, node_ids, args["max_runs"]
                )
            except RunError as exc:
                raise ToolError(422, "invalid_input", str(exc)) from exc
            return {"started": True, "batch": batch_view(batch)}
        preview = {
            "target": project.title,
            "fields": {
                "tasks": titles,
                "task_apps": apps,  # the application and mode of each task, in the same order
                "max_runs": args["max_runs"],
                # the nodes the applications' rows name (the gateway picks among them per run)
                "nodes": ", ".join(
                    self.runs.nodes_for({a["application"] for a in apps if a["application"]})
                    if self.runs is not None
                    else []
                )
                or "-",
            },
        }
        row = await ChangeStore(self.sm).stage(
            caller.user_id, caller.task_id, BATCH_TOOL, args, preview
        )
        return {
            "staged": True,
            "change_id": str(row.id),
            "applied": False,
            "note": "Prepared, not started. The person starts the batch with one click on its "
            "card in the chat. Say which tasks it runs and that it waits for the click.",
            "preview": preview,
        }


def _more_help(name: str | None) -> dict[str, Any]:
    if name is not None:
        spec = SPECS[name]
        return {"name": name, "description": spec["description"], "input_schema": spec["schema"]}
    return {
        "tools": [{"name": n, "does": _first_line(SPECS[n]["description"])} for n in EXTENDED],
        "how": "Call project.more.run with name and args; project.more.help with a name shows "
        "the schema of that tool.",
    }


async def _preview(
    store: ProjectStore, user_id: uuid.UUID, capability: str, args: dict[str, Any]
) -> dict[str, Any]:
    """What a prepared call would do, as data for the card: its target and the fields it sets."""
    if capability in WRITE_NAMES:
        return await write_preview(store, user_id, capability, args)
    fields = {k: v for k, v in args.items() if k not in ("project_id", "node_id", "revision")}
    if capability == "project.update":
        project = await store.get_project(user_id, uuid.UUID(args["project_id"]))
        _same_revision(project.revision, args["revision"])
        return {"target": project.title, "fields": fields}
    if capability in ("project.node.update", "project.node.move"):
        node = await store.get_node(user_id, uuid.UUID(args["node_id"]))
        _same_revision(node.revision, args["revision"])
        project = await store.get_project(user_id, node.project_id)
        fields = dict(fields)
        if capability == "project.node.move":
            parent = args.get("parent_id")
            fields = {
                "parent": "(top level)"
                if parent is None
                else node_view(await store.get_node(user_id, uuid.UUID(parent)), project.title)[
                    "ref"
                ]
            }
        return {"target": node_view(node, project.title)["ref"], "fields": fields}
    project = await store.get_project(user_id, uuid.UUID(args["project_id"]))
    return {"target": project.title, "fields": fields}


def _same_revision(current: int, given: int) -> None:
    if current != given:
        raise Conflict("revision")


async def _dispatch(
    store: ProjectStore, caller: Caller, capability: str, args: dict[str, Any], agents: Any
) -> Any:
    user_id = caller.user_id
    if capability == "project.list":
        return {"projects": [project_view(p) for p in await store.list_projects(user_id)]}
    if capability == "project.get":
        project = await store.get_project(user_id, uuid.UUID(args["project_id"]))
        nodes = await store.tree(user_id, project.id)
        start = int(args.get("from", 0))
        size = int(args.get("limit", PROJECT_PAGE))
        page = nodes[start : start + size]
        keep = ("id", "ref", "number", "parent_id", "kind", "title", "status", "revision")
        return {
            "project": project_view(project),
            "nodes_total": len(nodes),
            "nodes": [
                {k: v for k, v in node_view(n, project.title).items() if k in keep} for n in page
            ],
            "next_from": start + size if start + size < len(nodes) else None,
        }
    if capability == "project.update":
        changes = {k: args[k] for k in ("title", "description", "status") if k in args}
        project = await store.update_project(
            user_id, uuid.UUID(args["project_id"]), revision=args["revision"], **changes
        )
        return project_view(project)
    if capability == "project.node.create":
        node = await store.create_node(
            user_id,
            uuid.UUID(args["project_id"]),
            kind=args["kind"],
            title=args["title"],
            body=args.get("body", ""),
            parent_id=_uuid_or_none(args.get("parent_id")),
            depends_on=[uuid.UUID(d) for d in args.get("depends_on", [])],
            fields={k: args[k] for k in FIELD_NAMES if k in args},
        )
        return await _node_out(store, user_id, node)
    if capability == "project.node.update":
        kwargs: dict[str, Any] = {k: args[k] for k in ("title", "body", "status") if k in args}
        if "depends_on" in args:
            kwargs["depends_on"] = [uuid.UUID(d) for d in args["depends_on"]]
        node = await store.update_node(
            user_id,
            uuid.UUID(args["node_id"]),
            revision=args["revision"],
            fields={k: args[k] for k in FIELD_NAMES if k in args},
            **kwargs,
        )
        return await _node_out(store, user_id, node)
    if capability == "project.node.move":
        node = await store.move_node(
            user_id,
            uuid.UUID(args["node_id"]),
            revision=args["revision"],
            parent_id=_uuid_or_none(args["parent_id"]),
        )
        return await _node_out(store, user_id, node)
    if capability == "project.request.send":
        payload = {
            k: args[k]
            for k in (
                "topic", "questions", "depth", "min_sources", "min_hosts", "source_kinds",
                "must_cover", "expected", "format",
            )
            if k in args
        }  # fmt: skip
        node, why = await MessageService(store, agents).send_request(
            user_id,
            caller.admin,
            uuid.UUID(args["project_id"]),
            target=args["target"],
            payload=payload,
            parent_id=_uuid_or_none(args.get("parent_id")),
        )
        out = await _node_out(store, user_id, node)
        out["queue_note"] = why  # "" when the request is in the agent queue
        return out
    if capability == "project.order.draft":
        spec = {k: args[k] for k in ("expected", "constraints", "out_of_scope") if k in args}
        order = {
            "action": args["action"],
            "repo": args["repo"],
            "branch": args["branch"],
            "spec": {"goal": args["goal"], **spec},
            **({"acceptance": args["acceptance"]} if "acceptance" in args else {}),
        }
        node, why = await MessageService(store, agents).draft_order(
            user_id,
            caller.admin,
            uuid.UUID(args["project_id"]),
            payload=order,
            parent_id=_uuid_or_none(args.get("parent_id")),
        )
        out = await _node_out(store, user_id, node)
        out["queue_note"] = why
        return out
    # project.report.add
    node = await store.create_node(
        user_id,
        uuid.UUID(args["project_id"]),
        kind="report",
        title=args["title"],
        body=args.get("body", ""),
        parent_id=_uuid_or_none(args.get("parent_id")),
        report_kind=args["report_kind"],
        fields={k: args[k] for k in ("as_of", "origin_app") if k in args},
    )
    return await _node_out(store, user_id, node)


async def _node_out(store: ProjectStore, user_id: uuid.UUID, node: Any) -> dict[str, Any]:
    project = await store.get_project(user_id, node.project_id)
    return node_view(node, project.title)


def _uuid_or_none(value: str | None) -> uuid.UUID | None:
    return None if value is None else uuid.UUID(value)
