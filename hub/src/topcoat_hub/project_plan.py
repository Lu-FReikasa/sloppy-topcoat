"""The plan tool: a topic and its tasks, given as fixed fields (PPH-04, plan of 2026-11-08).

An agent that proposes a plan no longer writes the body of a task itself. It names the application
of each task, the columns of its result and its checklist; the hub writes the body lines, so the
form cannot be wrong, and it reports every problem of the plan at once, not one per try.

The plan becomes an ordinary change set (project_changesets.py): the person sees it, unticks parts
and applies it with one click, all or nothing.
"""

from typing import Any

import jsonschema

APPLICATIONS = ("research", "dev", "project", "admin")
MAX_TASKS = 30
TOPIC_LOCAL = "plan"

TASK_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["title", "application", "result", "checklist"],
    "properties": {
        "title": {"type": "string", "minLength": 1, "maxLength": 200},
        "application": {
            "enum": list(APPLICATIONS),
            "description": "the application that does the task: research (the internet, the "
            "notes and the wiki; not the machines or their models), dev (the repository and the "
            "dev node: code, formats, building; no internet), admin (the Verwaltung: the live "
            "hub, its settings, the machines and the models on them; reads, and proposes changes "
            "the person confirms), project (the planner's own data). Every checklist item must be "
            "answerable there; an item that needs another application is a task of its own",
        },
        "result": {
            "type": "array",
            "minItems": 1,
            "maxItems": 8,
            "items": {"type": "string", "minLength": 1, "maxLength": 60},
            "description": "the columns of the result table, one per item, e.g. Aussage and Beleg",
        },
        "checklist": {
            "type": "array",
            "minItems": 1,
            "maxItems": 30,
            "items": {"type": "string", "minLength": 1, "maxLength": 300},
            "description": "one item per thing to check, each a sentence the run can tick off",
        },
        "mode": {
            "enum": ["lesen", "schreiben"],
            "description": "lesen (the default): the task reads and reports. schreiben: it may "
            "propose changes, each one confirmed by the person; only for dev and admin",
        },
        "blocked_by": {
            "type": "array",
            "maxItems": 20,
            "items": {"type": "string", "minLength": 1, "maxLength": 200},
            "description": "exact titles of earlier tasks of this plan whose results this task "
            "uses (a merge, a comparison, anything that reads what another task produced). "
            "The task waits for each of them. Name every one: a task without blocked_by starts "
            "at once.",
        },
        "notes": {"type": "string", "maxLength": 2000},
    },
}

PLAN_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["project_id", "title", "topic", "tasks"],
    "properties": {
        "project_id": {"type": "string", "format": "uuid"},
        "title": {"type": "string", "minLength": 1, "maxLength": 200},
        "summary": {"type": "string", "maxLength": 2000},
        "topic": {
            "type": "object",
            "additionalProperties": False,
            "required": ["title"],
            "properties": {"title": {"type": "string", "minLength": 1, "maxLength": 200}},
        },
        "tasks": {
            "type": "array",
            "minItems": 1,
            "maxItems": MAX_TASKS,
            "items": TASK_SCHEMA,
            "description": "the tasks in the order they should run; a task that is blocked_by "
            "another names it by its exact title, and that task comes earlier in this list",
        },
    },
}

PLAN_DESCRIPTION = (
    "Propose a plan: one topic and its tasks, as one change set the person confirms with one "
    "click. Give each task its application, the columns of its result, and its checklist items; "
    "the hub writes the task text from these fields. A task that uses the result of an earlier "
    "task (a merge, a comparison) must name that task in blocked_by, or it starts at once. "
    'Example: {"project_id": "<id>", "title": '
    '"Wiki-Abgleich", "topic": {"title": "Abgleich Wiki und Repo"}, "tasks": [{"title": '
    '"Wiki-Seite Looks prüfen", "application": "research", "result": ["Aussage", "Beleg"], '
    '"checklist": ["Registrierung in look.ts prüfen"]}, {"title": "Repo-Stand prüfen", '
    '"application": "dev", "result": ["Befund", "Beleg"], "checklist": ["web/src/look.ts '
    'lesen"], "blocked_by": ["Wiki-Seite Looks prüfen"]}]}. Every problem of the plan is reported '
    "at once; fix them all in the next call."
)


def _text(value: str) -> str:
    """A field written as one line of the body: no line breaks, no column separator inside."""
    return " ".join(value.replace("|", "/").split())


def repair_plan(args: Any) -> Any:
    """The usual shape slips of a small model, put right before the plan is checked (live
    2026-11-10: "title is a required property" twice, and the tasks inside the topic): a topic
    written as text or as "topics", tasks inside the topic, a task named by "name" or
    "description" instead of "title", no plan title. Anything else is left for the check."""
    if not isinstance(args, dict):
        return args
    out = dict(args)
    if "topic" not in out and isinstance(out.get("topics"), list) and out["topics"]:
        out["topic"] = out.pop("topics")[0]
    topic = out.get("topic")
    if isinstance(topic, str):
        topic = {"title": topic}
    if isinstance(topic, dict):
        topic = dict(topic)
        if not topic.get("title"):
            topic["title"] = topic.get("name") or topic.get("topic") or out.get("title") or ""
        inner = topic.pop("tasks", None)
        if isinstance(inner, list) and not out.get("tasks"):
            out["tasks"] = inner
        topic = {"title": topic["title"]} if topic.get("title") else topic
        out["topic"] = topic
    if isinstance(out.get("tasks"), list):
        tasks = []
        for task in out["tasks"]:
            if isinstance(task, dict) and not task.get("title"):
                task = dict(task)
                named = task.pop("name", None) or task.get("description")
                if isinstance(named, str) and named.strip():
                    task["title"] = " ".join(named.split())[:200]
                if task.get("description") and not task.get("checklist"):
                    task["checklist"] = [" ".join(str(task["description"]).split())[:300]]
                task.pop("description", None)
            tasks.append(task)
        out["tasks"] = tasks
    if not out.get("title") and isinstance(out.get("topic"), dict) and out["topic"].get("title"):
        out["title"] = out["topic"]["title"]
    return out


def schema_problems(args: Any) -> list[str]:
    """Every problem of the arguments against the schema, not only the first one."""
    validator = jsonschema.Draft202012Validator(PLAN_SCHEMA)
    out = []
    for err in sorted(validator.iter_errors(args), key=lambda e: list(e.absolute_path)):
        where = "/".join(str(p) for p in err.absolute_path)
        out.append(f"{where}: {err.message}" if where else err.message)
    return out


def compile_plan(args: dict[str, Any]) -> tuple[list[dict[str, Any]], list[str]]:
    """The change set's operations for a plan, and every problem that is not a schema problem
    (a repeated title, a blocker that is no earlier task)."""
    problems: list[str] = []
    ops: list[dict[str, Any]] = [
        {
            "op": "create_node",
            "kind": "topic",
            "local": TOPIC_LOCAL,
            "title": _text(args["topic"]["title"]),
        }
    ]
    seen: dict[str, str] = {}  # casefolded title -> local name
    for i, task in enumerate(args["tasks"], start=1):
        title = _text(task["title"])
        key = title.casefold()
        if key in seen:
            problems.append(f"tasks/{i - 1}: the title {title!r} is named twice in this plan")
            continue
        deps: list[str] = []
        for blocker in task.get("blocked_by", []):
            target = seen.get(_text(blocker).casefold())
            if target is None:
                problems.append(
                    f"tasks/{i - 1}: {title!r} is blocked by {blocker!r}, which is no earlier task "
                    "of this plan (name its exact title and list it before this task)"
                )
            else:
                deps.append(target)
        mode = task.get("mode", "lesen")
        if mode == "schreiben" and task["application"] not in ("dev", "admin"):
            problems.append(
                f"tasks/{i - 1}: {title!r} may write only in dev or admin, not in "
                f"{task['application']}"
            )
        lines = [
            f"Zielanwendung: {task['application']}",
            f"Modus: {mode}",
            "Ergebnisform: " + " | ".join(_text(c) for c in task["result"]),
            *[f"- [ ] {_text(item)}" for item in task["checklist"]],
        ]
        if task.get("notes"):
            lines.append(_text(task["notes"]))
        local = f"t{i}"
        op: dict[str, Any] = {
            "op": "create_node",
            "kind": "task",
            "local": local,
            "title": title,
            "parent": TOPIC_LOCAL,
            "body": "\n".join(lines),
        }
        if deps:
            op["depends_on"] = deps
        ops.append(op)
        seen[key] = local
    return ops, problems
