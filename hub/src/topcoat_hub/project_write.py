"""Writing tasks of a project without the agent loop (2026-11-14).

Live, the Projekte chat was asked to work out tasks 1.5 to 1.7. The agent loop read the tasks,
then had to write three long texts as JSON with ids, revisions and escaped line breaks; Gemma wrote
good texts three times and failed on the form each time (a missing ``op``), the set that got through
held only titles, and the answer claimed instructions. Writing is not a job for the loop:

1. the hub reads the tasks the request names itself (no model step);
2. the chat model, which saw the conversation, writes the new version in one call, in plain
   sections (``### 1.5 Title`` and the text), no ids, no JSON;
3. the hub builds the change set from the sections (ids, revisions, the lines a run needs) and
   the person sees it with before and after.

Everything here but the model call is a pure function; ``chat_engine/answer.py`` drives it. A
request that is not clearly "rewrite these tasks" stays with the agent loop."""

import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from typing import Any

# a request to write or rewrite tasks (not to read them, not to start them)
_WRITE = re.compile(
    # (a dot is allowed in between: "arbeite 1.5 und 1.6 aus")
    r"\b(ausformulier\w*|ausarbeit\w*|arbeite\w*\b[^?!\n]{0,60}\baus\b|überarbeit\w*|"
    r"umformulier\w*|formulier\w*|umbenenn\w*|benenn\w*\b[^?!\n]{0,40}\bum\b|aufteil\w*|"
    r"teil\w*\b[^?!\n]{0,40}\bauf\b|präzisier\w*|konkretisier\w*|verfeiner\w*|ergänz\w*|"
    r"anleitung\w*|unteraufgabe\w*|rewrite|reword|flesh out|work out|write out|refine)\b",
    re.I,
)
_DOTTED = re.compile(r"(?<![\d.])(\d+(?:\.\d+)+)(?![\d.])")
_DASHES = "-" + chr(0x2013)  # "1.5-1.7" and the same with the typographic dash
_RANGE = re.compile(
    rf"(?<![\d.])(\d+(?:\.\d+)*)\.(\d+)\s*(?:bis|[{_DASHES}]|to)\s*(?:\1\.)?(\d+)(?![\d.])"
)
_SECTION = re.compile(r"^#{2,4}\s*(neu|new|\d+(?:\.\d+)*)\b[:.)]?\s*(.*)$", re.I | re.M)
MAX_TASKS = 12
FORM_LINES = ("Zielanwendung:", "Ergebnisform:")


def wants_writing(text: str) -> bool:
    """Is the message a request to write or rewrite tasks?"""
    return bool(_WRITE.search(text))


def numbers_in(text: str) -> list[str]:
    """The task numbers a text names, in order, a range like ``1.5 bis 1.7`` spelled out."""
    out: list[str] = []
    for m in _RANGE.finditer(text):
        lo, hi = int(m.group(2)), int(m.group(3))
        if lo <= hi <= lo + MAX_TASKS:
            out += [f"{m.group(1)}.{i}" for i in range(lo, hi + 1)]
    out += _DOTTED.findall(text)
    return list(dict.fromkeys(out))[:MAX_TASKS]


def pick_project(projects: Sequence[Any], texts: Iterable[str]) -> Any | None:
    """The one project the texts name by title, the newest text first; a longer title wins over a
    shorter one it contains ("Topcoat-Doku" before "Topcoat")."""
    ordered = sorted(projects, key=lambda p: -len(p.title or ""))
    for text in texts:
        low = text.lower()
        hits = [
            p for p in ordered if p.title and re.search(rf"\b{re.escape(p.title.lower())}\b", low)
        ]
        if hits:
            longest = hits[0]
            return longest
    return None


@dataclass
class Section:
    number: str | None  # None: a new task
    title: str
    body: str


@dataclass
class Plan:
    """What a write produced: the operations of the set and what the person should know."""

    operations: list[dict[str, Any]] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    touched: list[str] = field(default_factory=list)  # "1.5", "new: Title"


def parse_sections(text: str) -> list[Section]:
    """The sections of the model's answer: ``### 1.5 Title`` or ``### neu Title``, then the text."""
    out: list[Section] = []
    matches = list(_SECTION.finditer(text))
    for i, m in enumerate(matches):
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        body = text[m.end() : end].strip("\n").strip()
        body = re.sub(r"\n?```\s*$", "", body).strip()
        head = m.group(1).lower()
        title = m.group(2).strip().strip("*").strip()
        out.append(Section(None if head in ("neu", "new") else head, title, body))
    return out


def _keep_form(old: str, new: str) -> str:
    """A rewritten text keeps the lines a run needs when the model left them out."""
    keep = [
        line
        for line in old.splitlines()
        if line.strip().startswith(FORM_LINES)
        and not any(n.strip().startswith(line.strip().split(":")[0]) for n in new.splitlines())
    ]
    return "\n".join([*keep, new]) if keep else new


def build(sections: Sequence[Section], nodes: Sequence[Any], wanted: Sequence[str]) -> Plan:
    """The change set of the sections against the tasks of the project. A section for a number
    that does not exist, or that is no task, is left out and named; a task the request named but
    the model did not write is named too. A new task goes below the parent of the first named
    one."""
    by_number = {str(n.number): n for n in nodes}
    plan = Plan()
    parent = None
    for number in wanted:
        if number in by_number:
            parent = by_number[number].parent_id
            break
    written: set[str] = set()
    for s in sections:
        if s.number is None:
            if not s.title:
                continue
            op: dict[str, Any] = {"op": "create_node", "kind": "task", "title": s.title[:200]}
            if s.body:
                op["body"] = s.body[:4000]
            if parent is not None:
                op["parent"] = str(parent)
            plan.operations.append(op)
            plan.touched.append(f"new: {s.title}")
            continue
        node = by_number.get(s.number)
        if node is None or node.kind != "task":
            plan.notes.append(f"{s.number}: no such task, left out")
            continue
        written.add(s.number)
        update: dict[str, Any] = {"op": "update_node", "node": str(node.id)}
        if s.title and s.title != node.title:
            update["title"] = s.title[:200]
        body = _keep_form(node.body or "", s.body)[:4000] if s.body else ""
        if body and body.strip() != (node.body or "").strip():
            update["body"] = body
        if len(update) > 2:
            plan.operations.append(update)
            plan.touched.append(s.number)
        else:
            plan.notes.append(f"{s.number}: unchanged")
    for number in wanted:
        if number in by_number and number not in written:
            plan.notes.append(f"{number}: not written")
    return plan


WRITER_SYSTEM = (
    "You write tasks of the person's project so that an agent can carry each one out on its own. "
    "You get the conversation, the current tasks and the request. Answer with the new version of "
    "every task the request is about, in exactly this form and nothing else:\n\n"
    "### <number> <title>\n"
    "Zielanwendung: <research | dev | admin | project>\n"
    "Ergebnisform: <column> | <column>\n"
    "- [ ] <one checkpoint the agent works through>\n"
    "- [ ] <...>\n\n"
    "<what to do, step by step, and what the result must contain>\n\n"
    "The text after the checkpoints speaks to the agent that does the task ('Vergleiche …', "
    "'Richte … ein'); it never repeats the request or names the project. "
    "Keep each task's number. A task to add gets the heading '### neu <title>'. Keep the "
    "application and the result form a task has unless the request changes them. Write what the "
    "person asked for in the conversation, completely; do not invent facts about their code or "
    "systems. Write in the language of the conversation. No other text before or after the tasks."
)


def writer_messages(
    conversation: str, tasks: Sequence[Any], request: str, others: Sequence[Any] = ()
) -> list[dict[str, str]]:
    """The one call that writes: the conversation, the tasks as they are, the request."""
    current = "\n\n".join(
        f"### {n.number} {n.title}\n(status: {n.status})\n{(n.body or '').strip() or '(no text)'}"
        for n in tasks
    )
    context = ", ".join(f"{n.number} {n.title}" for n in others)
    parts = [
        f"Conversation so far:\n{conversation}" if conversation else "",
        f"Current tasks:\n{current}",
        f"Other tasks of the project (for reference only): {context}" if context else "",
        f"Request:\n{request}",
    ]
    return [
        {"role": "system", "content": WRITER_SYSTEM},
        {"role": "user", "content": "\n\n".join(p for p in parts if p)},
    ]


def answer_text(language: str, plan: Plan, project: str) -> str:
    """The hub's own answer: what was written and that it waits; no claim the set does not hold."""
    de = language == "de"
    names = ", ".join(plan.touched)
    if not plan.operations:
        lead = (
            f"Ich habe keine Änderung an {project} vorbereitet: der Entwurf änderte nichts."
            if de
            else f"I prepared no change to {project}: the draft changed nothing."
        )
    else:
        lead = (
            f"Ich habe die Aufgaben neu geschrieben ({names}). Der Änderungssatz wartet auf "
            "deinen Klick; darunter siehst du jeden Text vorher und nachher."
            if de
            else f"I rewrote the tasks ({names}). The change set waits for your click; below it "
            "you see each text before and after."
        )
    if plan.notes:
        lead += ("\n\nHinweise: " if de else "\n\nNotes: ") + "; ".join(plan.notes) + "."
    return lead
