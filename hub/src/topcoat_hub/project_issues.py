"""Issues from the running system (2026-11-17).

The person names a problem in a few words, in any chat ("Problem: the window jumps when I drag
it"). The hub, not a model, files it as a task below the topic "Issues" of the project the issues
belong to (the setting ``issue_project``, by default "Topcoat"), with the label ``issue`` and the
context it knows itself: the time, the build that runs, the window and conversation, the nodes,
runs that ended abnormally and the hub's own recent warnings (hub_log.py). The task then runs
through its iterations like any other (project_iterations.py):

1. ``issue_check`` (blocks/builtin.py): the cause in the code, with evidence, and a fix sketch;
2. ``issue_fix``: the change as edits; one click of the person applies them in the workshop, the
   tests run, and a passed fix can be rolled out to the hub (issue_fixes.py, D103, D104);
3. the person says whether it works: done, or a further iteration with what still fails.

Filing with a prefix is the person's own command, the same as a button: no model decides it, and
the answer offers to undo it. Without a prefix, a message that reads like a complaint gets a card
that files it on a click."""

import re
import uuid
from collections.abc import Sequence
from datetime import timedelta
from typing import Any

from sqlalchemy import select

from topcoat_hub import build_info, clock, hub_log
from topcoat_hub.clock import hub_now
from topcoat_hub.models import AgentTask, Node, ProjectNode
from topcoat_hub.project_base import NotFound, user_zone

ISSUE_LABEL = "issue"
ISSUES_TOPIC = "Issues"
DEFAULT_PROJECT = "Topcoat"
# a new issue starts with the investigation (2026-11-19: "Ursache suchen" went into it)
CHECK_PROGRAM = "issue_investigate"
FIX_PROGRAM = "issue_fix"
MAX_TITLE = 120  # a title the investigation writes
FIRST_TITLE = 80  # the hub's first title, from the person's text (1.15)
MAX_TEXT = 4000

# "Problem: …", "Bug: …", "Issue: …", "Fehler: …", "/issue …" at the start of the message
_PREFIX = re.compile(r"^\s*(?:/issue\b|(?:problem|bug|issue|fehler|störung)\s*:)\s*", re.I)
# a complaint without the prefix: offered as a card, never filed on its own
_COMPLAINT = re.compile(
    r"\b(geht nicht|funktioniert nicht|klappt nicht|hängt|hängen|kaputt|stürzt ab|"
    r"absturz|fehlermeldung|bug|doesn'?t work|does not work|broken|crash\w*|hangs?|freezes?)\b",
    re.I,
)


# "Bitte füge einen neuen Issue hinzu. <the problem>" (live 2026-11-20): the first sentence asks
# for an issue in words, the rest is the problem. The ask is the person's own command, as the
# prefix is; without it the project agent made a loose task at the project's root.
_ASK_FIRST = re.compile(r"^\s*([^.!?:\n]{0,120})[.!?:\n]+\s*(.*)$", re.S)
_ASK_NOUN = re.compile(r"\b(issue|bug|bugreport|ticket|fehlerbericht)\b", re.I)
_ASK_VERB = re.compile(
    r"\b(hinzu\w*|an(?:legen|lege|leg)\b|leg\w*\s+(?:\w+\s+){0,4}an\b|erstell\w*|aufnehm\w*|"
    r"nimm\w*|eintrag\w*|trag\w*\s+(?:\w+\s+){0,4}ein\b|mach\w*|meld\w*|add|create|file|open)\b",
    re.I,
)


def asked_text(text: str) -> str | None:
    """The problem when the message's first sentence asks for an issue in words ("Leg bitte ein
    Issue an: …", "Please file a bug. …") and something follows it; else None."""
    m = _ASK_FIRST.match(text or "")
    if not m:
        return None
    first, rest = m.group(1), m.group(2).strip()
    if not rest or not _ASK_NOUN.search(first) or not _ASK_VERB.search(first):
        return None
    return rest


def filed_text(text: str) -> str | None:
    """The problem itself when the message files an issue by its prefix, else None."""
    m = _PREFIX.match(text or "")
    if not m:
        return None
    rest = text[m.end() :].strip()
    return rest or None


def sounds_like_problem(text: str) -> bool:
    return bool(_COMPLAINT.search(text or "")) and len(text) <= 1200


# "z.B." ends no sentence (live 2026-11-20: the title of issue 2.10 ended in "(z.B.")
_ABBREV = re.compile(
    r"(?:\b(?:z\.\s?B|d\.\s?h|u\.\s?a|bzw|ca|etc|evtl|ggf|vgl|usw|e\.g|i\.e)|\(\w{0,3})$", re.I
)


def _first_part(line: str, ends: str) -> str | None:
    """The line up to its first end (one of ``ends``) that is no abbreviation and not inside a
    bracket, when that part is a title (15 to :data:`FIRST_TITLE` characters); else ``None``."""
    for end in re.finditer(rf"[{ends}](?=\s|$)", line):
        head = line[: end.start()]
        if len(head) < 15 or _ABBREV.search(head) or head.count("(") > head.count(")"):
            continue
        if len(head) + 1 <= FIRST_TITLE:
            return head + ("" if line[end.start()] in ".:;" else line[end.start()])
        return None
    return None


def title_of(text: str) -> str:
    """The hub's first title of an issue (plan step 1.15): the first line of the person's text when
    it is short (at most :data:`FIRST_TITLE` characters), else its first sentence, else the part
    before a colon, else the line cut at a word. A colon no longer ends a short line (the title was
    "the text before the first colon"). The investigation writes a better one (``apply_brief``)."""
    first = next((line.strip() for line in text.splitlines() if line.strip()), text.strip())
    first = re.sub(r"\s+", " ", first)
    if len(first) <= FIRST_TITLE:
        return first[:-1] if first.endswith(".") and not first.endswith("..") else first
    for ends in (".!?", ":;"):
        part = _first_part(first, ends)
        if part:
            return part
    cut = first[: FIRST_TITLE - 1].rsplit(" ", 1)[0]
    return cut + "…"


BRIEF_HEAD = "## Prüfpunkte (von der Untersuchung)"
_CONTEXT_HEAD = "## Kontext (vom Hub)"


def reported_text(body: str) -> str:
    """The person's own words in an issue's body: after the header lines, before the hub's context
    and the checks of an investigation."""
    _head, sep, rest = (body or "").partition("\n\n")
    text = rest if sep else ""
    for mark in (BRIEF_HEAD, _CONTEXT_HEAD):
        text = text.split("\n" + mark, 1)[0].split(mark, 1)[0]
    return text.strip()


def apply_brief(
    title: str, body: str, new_title: str, checks: Sequence[str]
) -> tuple[str | None, str | None]:
    """What an investigation changes of its issue (2026-11-20, the maintainer: the agent finds out
    and writes what is checked and tested, the person only describes the problem): a short title,
    while the title is still the hub's own cut of the person's words (a title the person gave
    stays), and the checks as a checklist before the hub's context, replacing earlier ones.
    ``(title, body)``, each None when it stays."""
    new_title = " ".join(str(new_title or "").split())[:MAX_TITLE].rstrip(" .")
    keep_title = not new_title or title != title_of(reported_text(body)) or new_title == title
    points = [" ".join(str(c).split()) for c in checks if str(c).strip()][:8]
    out_body: str | None = None
    if points:
        section = BRIEF_HEAD + "\n" + "\n".join(f"- [ ] {p}" for p in points)
        base = body or ""
        if BRIEF_HEAD in base:
            before, _sep, after = base.partition(BRIEF_HEAD)
            rest = after.split("\n## ", 1)
            base = before.rstrip() + ("\n\n## " + rest[1] if len(rest) > 1 else "")
        if _CONTEXT_HEAD in base:
            before, _sep, after = base.partition(_CONTEXT_HEAD)
            out_body = f"{before.rstrip()}\n\n{section}\n\n{_CONTEXT_HEAD}{after}"
        else:
            out_body = f"{base.rstrip()}\n\n{section}"
        if out_body == body:
            out_body = None
    return (None if keep_title else new_title), out_body


_WORD = re.compile(r"[a-zäöüß0-9]{4,}", re.I)


def similar(title: str, others: Sequence[ProjectNode], threshold: float = 0.5) -> list[ProjectNode]:
    """Open issues whose titles share most of their words with this one (a hint, never a merge)."""
    mine = {w.lower() for w in _WORD.findall(title)}
    if not mine:
        return []
    found = []
    for n in others:
        theirs = {w.lower() for w in _WORD.findall(n.title)}
        if theirs and len(mine & theirs) / len(mine | theirs) >= threshold:
            found.append(n)
    return found[:3]


def body_of(text: str, context: Sequence[str], when: str, where: str) -> str:
    """The task text: what a run needs (application, program, form), the person's words, then the
    hub's context."""
    lines = [
        "Zielanwendung: dev",
        f"Programm: {CHECK_PROGRAM}",
        "Ergebnisform: Diagnose",
        "Art: issue",
        f"Gemeldet: {when} ({where})",
        "",
        text.strip()[:MAX_TEXT],
    ]
    if context:
        lines += ["", "## Kontext (vom Hub)", *(f"- {c}" for c in context)]
    return "\n".join(lines)


async def gather_context(
    sessionmaker: Any,
    user_id: uuid.UUID,
    *,
    application: str | None,
    conversation: str | None,
    title: str | None,
    zone_name: str = "UTC",
) -> list[str]:
    """What the hub knows about the moment, without a model: build, window, nodes, runs that ended
    badly in the last 30 minutes, its own warnings of the last 30 minutes."""
    out = [f"Build: {build_info.BUILD}"]
    if application or conversation:
        where = application or "?"
        if title:
            where += f": „{title[:80]}“"
        if conversation:
            where += f" (Gespräch {conversation})"
        out.append(f"Fenster: {where}")
    since = hub_now() - timedelta(minutes=30)
    async with sessionmaker() as db:
        nodes = list(await db.scalars(select(Node).order_by(Node.name)))
        bad = list(
            await db.scalars(
                select(AgentTask)
                .where(
                    AgentTask.user_id == user_id,
                    AgentTask.started_at >= since,
                    AgentTask.status.in_(("failed", "canceled", "error")),
                )
                .order_by(AgentTask.started_at.desc())
                .limit(5)
            )
        )
    if nodes:
        out.append("Knoten: " + ", ".join(f"{n.name} {n.state}" for n in nodes))
    for t in bad:
        reason = (t.diagnosis or {}).get("error") or t.stop_reason or t.status
        out.append(f"Lauf {str(t.id)[:8]} „{(t.title or '')[:60]}“ endete: {str(reason)[:160]}")
    for line in hub_log.recent(30, 12):
        if line.logger == "topcoat_hub.main":  # what the hub says at every start is no clue
            continue
        out.append(f"Hub-Log {line.render(clock.zone(zone_name))}")
    return out


async def file_issue(
    store: Any,
    user_id: uuid.UUID,
    text: str,
    *,
    project_title: str = DEFAULT_PROJECT,
    application: str | None = None,
    conversation: str | None = None,
    conversation_title: str | None = None,
    where: str = "Chat",
) -> tuple[ProjectNode, list[ProjectNode]]:
    """File the issue: the project (by title), its topic "Issues" (made when missing), the task
    with the context, the label. Returns the task and similar open issues."""
    projects = [
        p for p in await store.list_projects(user_id) if p.title.lower() == project_title.lower()
    ]
    if not projects:
        raise NotFound(f"there is no project {project_title!r} to file issues in")
    project = projects[0]
    nodes = await store.tree(user_id, project.id)
    topic = next(
        (
            n
            for n in nodes
            if n.kind == "topic" and n.title == ISSUES_TOPIC and n.trashed_at is None
        ),
        None,
    )
    if topic is None:
        topic = await store.create_node(user_id, project.id, kind="topic", title=ISSUES_TOPIC)
    open_issues = [
        n
        for n in nodes
        if n.parent_id == topic.id
        and n.kind == "task"
        and n.status not in ("done", "dropped")
        and n.trashed_at is None
    ]
    title = title_of(text)
    # the person's own time (live 2026-11-17: the first issue said 00:56 for 01:56 in Berlin)
    where_zone = await user_zone(store._sessionmaker, user_id, store.default_zone)
    context = await gather_context(
        store._sessionmaker,
        user_id,
        application=application,
        conversation=conversation,
        title=conversation_title,
        zone_name=where_zone,
    )
    when = hub_now().astimezone(clock.zone(where_zone)).strftime("%Y-%m-%d %H:%M")
    task = await store.create_node(
        user_id,
        project.id,
        kind="task",
        title=title,
        body=body_of(text, context, when, where),
        parent_id=topic.id,
    )
    label = next((x for x in await store.list_labels(user_id) if x.title == ISSUE_LABEL), None)
    if label is None:
        label = await store.create_label(
            user_id,
            title=ISSUE_LABEL,
            color="#d9534f",
            description="a problem from the running system",
        )
    await store.set_node_labels(user_id, task.id, add=[label.id])
    return task, similar(title, open_issues)


def is_issue(node: ProjectNode) -> bool:
    return node.kind == "task" and "\nArt: issue" in f"\n{node.body or ''}"


def filed_answer(
    language: str, task: ProjectNode, project: str, alike: Sequence[ProjectNode]
) -> str:
    de = language == "de"
    if de:
        text = (
            f"Als Issue {task.number} in {project} angelegt: „{task.title}“. Den Kontext (Build, "
            "Fenster, Knoten, Hub-Log) habe ich angehängt. Geprüft wird es mit „Issue prüfen“ "
            "oder mit allen offenen Issues zusammen."
        )
        if alike:
            text += " Ähnlich: " + ", ".join(f"{n.number} „{n.title}“" for n in alike) + "."
    else:
        text = (
            f"Filed as issue {task.number} in {project}: “{task.title}”. The context (build, "
            "window, nodes, hub log) is attached. It is checked with “Check issue” or with all "
            "open issues together."
        )
        if alike:
            text += " Similar: " + ", ".join(f"{n.number} “{n.title}”" for n in alike) + "."
    return text


def select_open(nodes: Sequence[ProjectNode]) -> list[ProjectNode]:
    """The open issues that have no result yet (what "check the open issues" runs)."""
    has_result = {n.parent_id for n in nodes if n.kind == "report" and n.trashed_at is None}
    return [
        n
        for n in nodes
        if is_issue(n)
        and n.status in ("todo", "blocked")
        and n.trashed_at is None
        and n.id not in has_result
    ]
