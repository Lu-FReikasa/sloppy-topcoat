"""A chat check in steps, each with a fresh context (Phase 1, 2026-11-11).

Live, a check ran as one conversation with the model: every tool result stayed in the prompt, which
grew from ~5k to 15-32k tokens after 10-20 calls, and a small model lost its way (27-49 % of the
runs ended on ``no_progress``, ``repeated_calls`` or a limit). Here a check is planned first: one
model call splits the task into one to three working steps, each with a goal, the tools it needs
and what it must deliver. The hub adds a last step that writes the answer. Every step starts with
an empty transcript and sees only the task, its own goal and the results of the earlier steps
(summary and findings with their evidence marks). The run stays one task: the marks E1, E2 … go
on across the steps, so the answer cites what any step saw, and the steps show in the GUI as they
always did.

The plan is checked against the tools the run really has; a plan that cannot be read, names no
known tool or has a single step leaves the run as it was (one loop) — a planning failure never
ends a run. Pure functions: the loop (agent_loop.py) calls them.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from topcoat_hub.json_repair import repair_json_object

MAX_STEPS = 3
GOAL_CHARS = 300
RESULT_CHARS = 2400  # what the next steps see of one step's result
FINAL_CALLS = 2  # the answer step may look once or twice more, no more

PLAN_SYSTEM = (
    "You plan how an assistant answers a task with read-only tools, in steps. Each step runs on "
    "its own: it sees the task, its goal and the results of the earlier steps, not their tool "
    "outputs. Reply with one JSON object only, no prose."
)


@dataclass
class Step:
    goal: str
    tools: list[str]
    result: str


@dataclass
class Stages:
    """The plan of a run and where it stands."""

    steps: list[Step]
    index: int = 0
    start_calls: int = 0
    max_calls: int = 6
    results: list[str] = field(default_factory=list)

    @property
    def total(self) -> int:
        return len(self.steps) + 1  # the hub's answer step

    @property
    def final(self) -> bool:
        return self.index >= len(self.steps)

    def budget(self) -> int:
        return FINAL_CALLS if self.final else self.max_calls


def plan_messages(task: str, tools: list[dict[str, Any]]) -> list[dict[str, str]]:
    """The planning request: the task and the tools by name and purpose (no schemas)."""
    seen: dict[str, str] = {}
    for t in tools:
        cap = str(t.get("capability") or "")
        if cap and cap not in seen:
            seen[cap] = " ".join(str(t.get("description") or "").split())[:140]
    listing = "\n".join(f"- {c}: {d}" for c, d in seen.items())
    user = (
        f"TASK:\n{task.strip()}\n\nTOOLS:\n{listing}\n\n"
        f"Split the work into 1 to {MAX_STEPS} steps. A question that one or two tool calls "
        "answer is ONE step. Typical steps: find (search or list to learn names, paths, ids), "
        "read (read the found items), check (look at a machine or a state), draft (write "
        "something from what was read). Do not plan the final answer: it is added after your "
        "steps.\n"
        'Reply: {"steps": [{"goal": "one sentence", "tools": ["capability", ...], '
        '"result": "what this step must deliver, e.g. the paths of the theme files"}]}'
    )
    return [{"role": "system", "content": PLAN_SYSTEM}, {"role": "user", "content": user}]


def parse_plan(text: str, tools: list[dict[str, Any]]) -> list[Step]:
    """The steps of a plan, checked against the tools there are; [] when it is unusable. A tool
    name the run does not have is dropped; a step left with none may use every tool (the step's
    goal still narrows what the model does)."""
    doc: Any = None
    start, end = text.find("{"), text.rfind("}")
    if start >= 0 and end > start:
        try:
            doc = json.loads(text[start : end + 1])
        except ValueError:
            doc = repair_json_object(text[start : end + 1])
    raw = doc.get("steps") if isinstance(doc, dict) else doc if isinstance(doc, list) else None
    if not isinstance(raw, list):
        return []
    have = {str(t.get("capability") or "") for t in tools}
    out: list[Step] = []
    for item in raw[:MAX_STEPS]:
        if not isinstance(item, dict):
            continue
        goal = " ".join(str(item.get("goal") or "").split())[:GOAL_CHARS]
        if not goal:
            continue
        named = item.get("tools") if isinstance(item.get("tools"), list) else []
        caps = [str(c) for c in named if str(c) in have]
        result = " ".join(str(item.get("result") or "").split())[:GOAL_CHARS]
        out.append(Step(goal, caps, result))
    return out


def step_tools(stages: Stages, tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The tools of the current step: the ones it named (all of them for the answer step, or when
    none of its names was known), so a step is not tempted by the rest."""
    if stages.final:
        return tools
    caps = set(stages.steps[stages.index].tools)
    picked = [t for t in tools if t.get("capability") in caps]
    return picked or tools


def step_prompt(task: str, stages: Stages) -> str:
    """The task as the current step sees it."""
    earlier = "\n".join(stages.results) if stages.results else "(none yet)"
    if stages.final:
        return (
            f"{task.strip()}\n\n"
            f"WORKING IN STEPS — last step ({stages.total} of {stages.total}): write the answer to "
            "the task from the results of the steps below. Their marks (E1, E2 …) are valid: cite "
            "them in your findings. Look again only if something essential is missing (at most "
            f"{FINAL_CALLS} calls); otherwise finish now.\n\nRESULTS OF THE STEPS:\n{earlier}"
        )
    step = stages.steps[stages.index]
    return (
        f"{task.strip()}\n\n"
        f"WORKING IN STEPS — step {stages.index + 1} of {stages.total}: {step.goal}\n"
        + (f"This step must deliver: {step.result}\n" if step.result else "")
        + f"Do only this step (at most {stages.max_calls} tool calls), then reply with finish: "
        "the summary is the result of this step, the findings are its facts with their marks. "
        "The answer to the whole task is written in the last step.\n\n"
        f"RESULTS OF THE EARLIER STEPS:\n{earlier}"
    )


def _found_lines(found: list[tuple[str, str, str]]) -> list[str]:
    return [f"  [{ref}] {call}: {gist}" for ref, call, gist in found[-8:]]


def result_text(
    stages: Stages,
    summary: str,
    findings: list[dict[str, Any]],
    found: list[tuple[str, str, str]] | None = None,
) -> str:
    """One step's result for the steps after it: its summary, its findings with their marks, and
    what it looked at (mark, call, first characters): the next step reads on from there instead of
    asking the same again (live R4: steps that saw only a summary repeated the earlier calls)."""
    step = stages.steps[stages.index]
    lines = [f"Step {stages.index + 1} ({step.goal}): {' '.join(summary.split())}"]
    for f in findings[:8]:
        if not isinstance(f, dict):
            continue
        marks = f.get("evidence") or []
        mark = f" [{', '.join(str(m) for m in marks)}]" if marks else ""
        lines.append(f"- {' '.join(str(f.get('claim') or '').split())}{mark}")
    if found:
        lines.append("  It looked at:")
        lines += _found_lines(found)
    return "\n".join(lines)[:RESULT_CHARS]


def incomplete_text(stages: Stages, found: list[tuple[str, str, str]]) -> str:
    """The result of a step that used its calls without finishing: what it collected."""
    step = stages.steps[stages.index]
    lines = [f"Step {stages.index + 1} ({step.goal}): not finished within its calls. It looked at:"]
    lines += _found_lines(found) or ["  nothing"]
    return "\n".join(lines)[:RESULT_CHARS]


def plan_note(steps: list[Step]) -> str:
    """The plan as a note of the run (shown with its steps in the GUI)."""
    body = "; ".join(f"{i}. {s.goal}" for i, s in enumerate(steps, 1))
    return f"Plan in {len(steps) + 1} steps: {body}; {len(steps) + 1}. answer"


def dumps(steps: list[Step]) -> str:
    return json.dumps([s.__dict__ for s in steps], ensure_ascii=False)
