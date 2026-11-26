"""A short, current brief of what an application is about, for the chat of that application.

The assistant of a program should know what the person means by "the project" or "my repos" before
it has looked anything up: the brief is the live state (read from the application's own tables
at the moment of the question, never remembered), put in front of the person's message like the
recalled notes. It is small on purpose: names and counts, not content. The details are for the
tools.
"""

import re
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import select

from topcoat_hub.models import ProjectEvent
from topcoat_hub.project_base import user_zone
from topcoat_hub.project_events import KINDS
from topcoat_hub.project_store import ProjectStore
from topcoat_hub.project_view_defs import BUILTIN

MAX_CHARS = 1500
MAX_PROJECTS = 12
MAX_REPOS = 12

HEADER = "Current state of this application (read just now, not a memory):"


async def project(state: Any, user_id: uuid.UUID) -> str:
    store = ProjectStore(state.sessionmaker)
    projects = await store.list_projects(user_id)
    if not projects:
        return "The person has no project in Projekte yet."
    lines = []
    for p in projects[:MAX_PROJECTS]:
        nodes = await store.tree(user_id, p.id)
        tasks = [n for n in nodes if n.kind == "task"]
        counts: dict[str, int] = {}
        for t in tasks:
            counts[t.status] = counts.get(t.status, 0) + 1
        by_status = ", ".join(f"{n} {s}" for s, n in sorted(counts.items())) or "no tasks"
        lines.append(f"- {p.title} ({p.status}): {len(nodes)} entries; tasks: {by_status}")
    more = len(projects) - MAX_PROJECTS
    if more > 0:
        lines.append(f"- … and {more} more")
    text = "Projects of the person:\n" + "\n".join(lines)
    across = await _across(state, store, user_id)
    return f"{text}\n{across}" if across else text


# what needs the person's or the agent's attention, counted with the filters of the built-in views
_ATTENTION = (("overdue", "overdue"), ("in_review", "in review"), ("unanswered", "unanswered"))


async def _across(state: Any, store: ProjectStore, user_id: uuid.UUID) -> str:
    """One line over all projects: how many tasks are overdue, in review, or wait for an answer.
    Counts only; the details are behind project.node.search and project.summary."""
    zone = await user_zone(state.sessionmaker, user_id, state.settings.hub_timezone)
    parts = []
    for key, label in _ATTENTION:
        found = await store.search_nodes(user_id, filter_text=BUILTIN[key][2], limit=1, zone=zone)
        if found.total:
            parts.append(f"{found.total} {label}")
    return "Needs attention: " + ", ".join(parts) + "." if parts else ""


# What the person changed in Projekte themselves (actor "person" in the Akte), newest first: the
# assistant must know what was done by hand since it last looked (the maintainer, 2026-11-17: "the
# agent should always see all my changes"; tasks made by hand were unknown to the next run).
CHANGES_HOURS = 48
CHANGES_LINES = 15
CHANGES_CHARS = 1400
_EVENT_WORDS = {
    "node.created": "created",
    "node.updated": "renamed or changed",
    "body.changed": "changed the text of",
    "node.status_changed": "set the status of",
    "node.completed": "completed",
    "node.moved": "moved",
    "node.trashed": "deleted",
    "node.restored": "restored",
    "project.created": "created the project",
    "project.updated": "changed the project",
    "project.trashed": "deleted the project",
    "project.restored": "restored the project",
}


async def person_changes(state: Any, user_id: uuid.UUID, hours: int = CHANGES_HOURS) -> str:
    """The person's own recent changes in Projekte as short lines, newest first, or "". Only this
    person's events (D20), only those the person made, not the agent's or the system's."""
    since = datetime.now(UTC) - timedelta(hours=hours)
    zone = await user_zone(state.sessionmaker, user_id, state.settings.hub_timezone)
    async with state.sessionmaker() as db:
        rows = (
            await db.scalars(
                select(ProjectEvent)
                .where(
                    ProjectEvent.user_id == user_id,
                    ProjectEvent.actor_kind == "person",
                    ProjectEvent.at >= since,
                )
                .order_by(ProjectEvent.seq.desc())
                .limit(CHANGES_LINES)
            )
        ).all()
    lines: list[str] = []
    used = 0
    for e in rows:
        what = _EVENT_WORDS.get(e.kind) or KINDS.get(e.kind, {}).get("en", e.kind).lower()
        where = " ".join(x for x in (e.node_ref, f"«{e.node_title}»" if e.node_title else "") if x)
        if not where:
            where = f"«{e.project_title}»" if e.project_title else ""
        line = (
            f"- {e.at.astimezone(ZoneInfo(zone)).strftime('%d.%m. %H:%M')} {what} {where}".rstrip()
        )
        if e.kind == "node.status_changed" and isinstance(e.payload, dict) and e.payload.get("to"):
            line += f" to {e.payload['to']}"
        if used + len(line) > CHANGES_CHARS:
            break
        lines.append(line)
        used += len(line) + 1
    if not lines:
        return ""
    return (
        f"What the person changed in Projekte by hand in the last {hours} h, newest first (read "
        "the entries with the project tools before you build on them):\n" + "\n".join(lines)
    )


async def development(state: Any, user_id: uuid.UUID) -> str:
    rows = await state.devrepos.list(user_id)
    if not rows:
        return "No repository is registered for the workshop yet."
    lines = [f"- {r.name} ({r.kind})" for r in rows[:MAX_REPOS]]
    return "Registered repositories:\n" + "\n".join(lines)


BRIEFS: dict[str, Any] = {"project": project, "dev": development}

# What the assistant of an application must know to do its work without reading files first. Not cut
# with the live part. (A frame rule is cut at 240 characters, so a format cannot live there.)
THEME_REFERENCE = (
    "Themes of the desktop (data plus CSS, never code). Show a finished theme as ONE json code "
    "block with the whole document, no separate css block, and one short line on the look after "
    "it. Do not doubt the format, do not offer to verify anything.\n"
    '{"key": "dusk" (a-z 0-9 -, at most 40; a new key for a new theme), "name": "Dusk", '
    '"base": "terminal" (always, unless asked for a control room or a paper desk), '
    '"layout": {"kind": "tile"|"columns"|"rows"|"focus"|"cascade"|"nest"}, '
    '"bar": {"position": "bottom"|"top"|"left"|"right"|"hidden", '
    '"items": a list of "start","programs","clock","status","me"}, '
    '"chrome": {"buttons": a list of "minimise","maximise","close", "titlebar": "top"|"hidden"}, '
    '"motion": {"style": "none"|"fade"|"slide"|"flicker"|"zoom" (a window opens), "close": the '
    'same (it closes or is minimised), "overlay": the same (the overlay comes and goes), "ms": 0 '
    "to 2000}, "
    '"css": "..."}\n'
    "Choose the fields from what the person asked and keep what they did not mention neutral "
    "(tile, bottom, all items, fade 150). base: terminal = dark flat panels, the only base "
    "whose windows follow the colour variables, so use it for every dark or coloured theme; "
    "leitstand = control room with signal lamps; schreibtisch = ALWAYS light paper slips with "
    "tape, it ignores the colour variables inside the windows (never use it for a dark theme). "
    "layout: tile = a grid of all open programs, columns = side "
    "by side, rows = stacked, focus = one big and the rest small at the side, cascade = "
    "overlapping windows, nest = splits inside splits with a view that zooms to one group "
    "(Alt+Down/Alt+Up/Alt+0, Esc steps out; a program too small is drawn scaled down: "
    ".dt-scaled, one outside the zoomed group: .dt-unframed). motion is only how a window "
    "appears; for nest, ms is also the length of a zoom.\n"
    "css is a string. Every declaration ends with ; and every rule is on its own line (write \\n "
    "inside the json string); a line without ; swallows the next one. Colours are variables, "
    "written as bare declarations at the top, not inside :root: --bg (desktop and page "
    "background), --surface (window and bar), --text, --muted, --border, --accent, --accent-text "
    "(text on an accent), --track, --ok, --warn, --bad, --radius; the font is the plain "
    "declaration font-family: ... (not a variable). The programs inside the windows use these "
    "variables for everything, so set colours through them. Dark means dark everywhere: "
    "--bg, --surface and --track dark, --text and --muted light, and no white or light "
    "background on .dt-program or .dt-program-body. Keep every text colour readable on its "
    'background. A correction ("too bright", "more blue") must change what is visible: '
    "change the variables, and the base if it is the cause, then say in one line what you "
    "changed; never send the same document again. At most 40000 characters, url() only as "
    "data:image, no @import. Animations: @keyframes plus animation: on a class below.\n"
    "These are ALL the classes of the desktop; a rule for any other .dt- class does nothing: "
    ".dt-stage (the area of the windows), .dt-program (a window; .dt-focused the active one, "
    ".dt-maxi a maximised one) holding .dt-titlebar (.dt-lamp, .dt-code, .dt-title with .dt-icon, "
    ".dt-titlebar-buttons with plain button elements) and .dt-program-body (.dt-nav, .dt-view); "
    ".dt-taskbar holding .dt-start, .dt-tasks with one button.dt-task per program (.dt-open, "
    ".dt-min, .dt-front, .dt-icon, .dt-label, .dt-signal), .dt-clock, .dt-me; the command palette "
    ".dt-palette on .dt-palette-backdrop with li.dt-pal-current. "
    "Under the block the chat shows a preview and the click that stores the theme."
)
REFERENCES = {"dev": THEME_REFERENCE}
# a reference is given only when the conversation is about its topic: the theme format with its
# example in every Development message was copied as the answer to a question about agent_loop.py
# (live 2026-11-11, Qwen3-30B: "dusk" twice), and it costs ~900 tokens a message
TOPICS = {
    "dev": re.compile(
        r"(theme\w*|\blook\w*|aussehen|design|farb\w*|colou?r\w*|\bcss\b|dunkl\w*|dunkel|\bhell\w*|"
        r"\bdark\w*|\blight\w*|neon|taskleiste|taskbar|titelleiste|titlebar|\bdesktop\w*|"
        r"oberfl[äa]che)",
        re.I,
    )
}


def needs_reference(application: str | None, talk: str) -> bool:
    """Whether the conversation (the last messages of the person) is about the reference's topic."""
    topic = TOPICS.get(application or "")
    return topic is not None and bool(topic.search(talk or ""))


async def brief(
    state: Any, user_id: uuid.UUID, application: str | None, talk: str | None = None
) -> str:
    """The brief of ``application`` with its header, or "" when it has none (or cannot be read: a
    brief must never cost the person their answer). ``talk`` is what the person said lately: the
    reference of the application comes only when it is about its topic (``None``: always)."""
    make = BRIEFS.get(application or "")
    if make is None:
        return ""
    try:
        text = await make(state, user_id)
    except Exception:
        return ""
    reference = REFERENCES.get(application or "")
    if reference and talk is not None and not needs_reference(application, talk):
        reference = None
    tail = f"\n{reference}" if reference else ""
    if application == "project":
        try:
            changes = await person_changes(state, user_id)
        except Exception:
            changes = ""
        if changes:
            tail += f"\n{changes}"
    return f"{HEADER}\n{text[:MAX_CHARS]}{tail}\n\n"
