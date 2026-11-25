"""What each application a task can run in may use, read from the ceilings that bind its runs.

Live 2026-11-18: Projekte wrote a task for Research that asked which models node-amd and node-nvidia can
run; Research has no tool that sees Topcoat's nodes (only Verwaltung has ``topcoat.state``), so the
run could only answer "not found". The planner's own words even said Research reads the wiki, which
it did not. This list is made from the same ``tool_scope`` of each program (ui_templates) that
``app_scope.ceiling`` applies to the runs, so it says what a run there really has.
"""

from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from topcoat_hub import tool_groups
from topcoat_hub.ui.store import TemplateStore

# the applications a task names in "Zielanwendung:" (project_runs.APPLICATIONS), with their name
TARGETS = (
    ("research", "Research"),
    ("dev", "Entwicklung"),
    ("admin", "Verwaltung"),
    ("project", "Projekte"),
)
# what a task most often needs; a target without one of these says so plainly
KEY_GROUPS = ("topcoat", "workshop", "wiki", "notes", "project")
LABELS = {key: label for key, label, _ in tool_groups.GROUPS}

RULES = (
    "Every point of a task must be answerable with what its application can use. A point that "
    "needs another application gets a task of its own there, and the task that uses its result "
    "depends on it (depends_on). Chat is no application a task runs in. A question about the "
    "machines, their models or Topcoat's settings needs Verwaltung (admin); a question about "
    "the internet needs Research or Verwaltung; a question about the code needs Entwicklung."
)


def describe(key: str, name: str, scope: dict[str, Any] | None) -> str:
    """One line: what a run of ``key`` may use and what it may not."""
    if scope is None:
        return f"- {key} ({name}): every tool group the person may use, and the internet"
    groups = scope.get("groups")
    has = list(tool_groups.KEYS) if groups is None else [g for g in groups if g in LABELS]
    internet = scope.get("internet") is not False
    parts = [f"{g} ({LABELS[g]})" for g in has if g != "web"]
    if internet:
        parts.append("the internet (web search, public pages)")
    missing = [g for g in KEY_GROUPS if g not in has]
    if not internet:
        missing.append("internet")
    line = f"- {key} ({name}) can use: " + ("; ".join(parts) or "nothing")
    if missing:
        line += f". It can NOT use: {', '.join(missing)}"
    return line + "."


async def catalogue(db: AsyncSession) -> str:
    """The applications a task can run in, each with what its runs may use."""
    store = TemplateStore(db)
    lines = []
    for key, name in TARGETS:
        template = await store.get(key)
        if template is None:
            continue
        agent = template.agent if isinstance(template.agent, dict) else {}
        scope = agent.get("tool_scope") if "tool_scope" in agent else None
        lines.append(describe(key, name, scope if isinstance(scope, dict) else None))
    return "\n".join(lines)


def section(text: str) -> str:
    """The catalogue as a part of a system prompt."""
    if not text:
        return ""
    return (
        "\n\nThe applications a task can run in (Zielanwendung) and what their runs can use\n"
        f"{text}\n- {RULES}"
    )
