"""Iterations of a task (2026-11-17).

Live 2026-11-16, task 1.8.2 (a theme, v1 to v4): each new version took two to four chat turns
(rename the task to "(vN)", rewrite its text, confirm, start, confirm), the writer never saw the
version before ("based on 1.8.2.3" was only a sentence), and the feedback lived in the task text,
which the chat rewrote each time: v4 lost "dark" and came out light again.

Now a task is the fixed order and every run of it is an iteration:

- its result (a report below the task) carries its number, the result it continued
  (``based_on``) and the feedback it answers;
- the task keeps **requirements**: what every further iteration must hold. Feedback the person
  keeps becomes one; they are never rewritten by a model, only added or struck out;
- a new iteration is one click (the person's, D15: a GUI action or a card in the chat) with the
  feedback; the run gets the version before as its starting point, the requirements and the
  feedback (:func:`brief`), and changes only what they ask for.

The same shape carries code later (an iteration is a commit on the task's branch, accepting it a
pull request). Everything here but :class:`IterationStore` is a pure function."""

import json
import re
import secrets
import uuid
from collections.abc import Iterable, Sequence
from typing import Any

from sqlalchemy import select

from topcoat_hub.clock import hub_now
from topcoat_hub.models import ProjectNode
from topcoat_hub.project_base import (
    Invalid,
    StoreBase,
    bump,
    check_revision,
    get_node,
    get_project,
)

MAX_REQUIREMENTS = 30
MAX_REQUIREMENT_CHARS = 400
MAX_FEEDBACK_CHARS = 4000
MAX_BASE_CHARS = 16000  # the version before, as the run gets it


def _norm(text: str) -> str:
    return " ".join(text.split()).casefold()


def clean_requirements(items: Iterable[Any]) -> list[dict[str, Any]]:
    """The requirements in their stored shape: ``{"id", "text", "from"?}``, no empty or repeated
    text, at most :data:`MAX_REQUIREMENTS`. A bare string is a new requirement."""
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in items:
        raw = item if isinstance(item, dict) else {"text": item}
        text = " ".join(str(raw.get("text") or "").split())[:MAX_REQUIREMENT_CHARS]
        if not text or _norm(text) in seen:
            continue
        seen.add(_norm(text))
        entry: dict[str, Any] = {"id": str(raw.get("id") or secrets.token_hex(4)), "text": text}
        if isinstance(raw.get("from"), int):
            entry["from"] = raw["from"]
        out.append(entry)
    if len(out) > MAX_REQUIREMENTS:
        raise Invalid(f"at most {MAX_REQUIREMENTS} requirements")
    return out


def results_of(nodes: Iterable[ProjectNode], task_id: uuid.UUID) -> list[ProjectNode]:
    """The results below a task, oldest first (the iterations, also those filed before numbers)."""
    found = [
        n for n in nodes if n.parent_id == task_id and n.kind == "report" and n.trashed_at is None
    ]
    return sorted(found, key=lambda n: (n.iteration or 0, n.created_at or hub_now()))


def next_number(results: Sequence[ProjectNode]) -> int:
    return max((r.iteration or 0 for r in results), default=0) + 1


def theme_in(text: str) -> dict[str, Any] | None:
    """The theme document a result holds (its first JSON code block that is a theme)."""
    for m in re.finditer(r"```json\s*\n(.*?)\n```", text or "", re.S):
        try:
            doc = json.loads(m.group(1))
        except ValueError:
            continue
        if isinstance(doc, dict) and {"key", "layout", "bar", "chrome", "motion"} <= set(doc):
            return doc
    return None


def base_text(result: ProjectNode) -> str:
    """What of a result a run continues: the theme alone where there is one (the pictures and the
    hints of that run are not the thing to change), else the report without its status line."""
    theme = theme_in(result.body)
    if theme is not None:
        return "```json\n" + json.dumps(theme, ensure_ascii=False, indent=2) + "\n```"
    body = re.sub(r"^\*\*Stand:\*\*[^\n]*\n+", "", result.body or "")
    body = re.sub(r"\n+_Lauf: [0-9a-f-]+_\s*$", "", body)
    return body.strip()[:MAX_BASE_CHARS]


def brief(
    number: int,
    base: ProjectNode | None,
    feedback: str,
    requirements: Sequence[dict[str, Any]],
    evidence: bool = False,
) -> str:
    """What a run of iteration ``number`` gets besides its task: the version it continues, the
    feedback on that version and the requirements. Empty for a first run without requirements.

    ``evidence``: the result is a table backed by the run's own reads (an Ergebnisform with a
    Beleg column). Such a result is written anew from this run's reads; the earlier version only
    says what to look at again (live 2026-11-17: told to "keep everything else as it is", a survey
    copied its refused table word for word and was refused again)."""
    parts: list[str] = []
    if base is not None and evidence:
        stand = re.match(r"\*\*Stand:\*\*\s*([^\n]*)", base.body or "")
        verdict = f"Its verdict: {stand.group(1).strip()}\n" if stand else ""
        # the earlier text is not shown: a model given it copies it, told not to or not (live
        # 2026-11-17, twice in a row); its verdict and the person's feedback say what to do better
        parts.append(
            f"## The earlier version (iteration {base.iteration or '?'}, {base.number})\n"
            f"{verdict}"
            "It is not shown here on purpose: write the summary and every row of the table from "
            "what you read in THIS run, and cite for each row the read and the place in it (file "
            "and line). A row that no read of this run backs is left out."
        )
    elif base is not None:
        parts.append(
            f"## The version to continue (iteration {base.iteration or '?'}, {base.number})\n"
            "This is the starting point. Change only what the feedback and the requirements below "
            "ask for and keep everything else as it is (for a theme: the same key and name, the "
            "same base, every rule that is not concerned).\n\n" + base_text(base)
        )
    if feedback.strip():
        parts.append(f"## Feedback of the person on that version\n{feedback.strip()}")
    if requirements:
        parts.append(
            "## Requirements (every one must hold in this iteration)\n"
            + "\n".join(f"- {r['text']}" for r in requirements)
        )
    if not parts:
        return ""
    return f"\n\n# Iteration {number}\n\n" + "\n\n".join(parts)


class IterationStore(StoreBase):
    """The writes of iterations, each one transaction with its line of the Akte."""

    async def set_requirements(
        self, user_id: uuid.UUID, task_id: uuid.UUID, items: Iterable[Any], *, revision: int
    ) -> ProjectNode:
        cleaned = clean_requirements(items)
        async with self._sessionmaker() as db:
            node = await get_node(db, user_id, task_id, lock=True)
            if node.kind != "task":
                raise Invalid("only a task has requirements")
            project = await get_project(db, user_id, node.project_id, lock=True)
            check_revision(node.revision, revision)
            old = list(node.requirements or [])
            node.requirements = cleaned
            bump(node)
            await self._record(
                db,
                user_id,
                project,
                "node.requirements_changed",
                node=node,
                payload={"from": [r["text"] for r in old], "to": [r["text"] for r in cleaned]},
            )
            await db.commit()
            await db.refresh(node)
            return node

    async def begin(
        self,
        user_id: uuid.UUID,
        task_id: uuid.UUID,
        *,
        based_on: uuid.UUID | None,
        feedback: str,
        keep: bool,
    ) -> tuple[ProjectNode, dict[str, Any]]:
        """Prepare iteration N+1 of a task: check the result it continues (a result of this task;
        none given: the newest), keep the feedback as a requirement when asked, and note what the
        run is started with (``next_iteration``). Returns the task and that note."""
        feedback = feedback.strip()[:MAX_FEEDBACK_CHARS]
        async with self._sessionmaker() as db:
            node = await get_node(db, user_id, task_id, lock=True)
            if node.kind != "task":
                raise Invalid("only a task has iterations")
            if node.status in ("in_progress", "dropped"):
                raise Invalid(f"a task that is {node.status} gets no new iteration")
            project = await get_project(db, user_id, node.project_id, lock=True)
            results = results_of(
                await db.scalars(
                    select(ProjectNode).where(
                        ProjectNode.user_id == user_id, ProjectNode.parent_id == node.id
                    )
                ),
                node.id,
            )
            base = next((r for r in results if r.id == based_on), None) if based_on else None
            if based_on is not None and base is None:
                raise Invalid("the version to continue is no result of this task")
            if base is None and results:
                base = results[-1]
            number = next_number(results)
            if keep and feedback:
                node.requirements = clean_requirements(
                    [*(node.requirements or []), {"text": feedback, "from": number}]
                )
            note = {
                "number": number,
                "based_on": str(base.id) if base is not None else None,
                "feedback": feedback,
            }
            node.next_iteration = note
            bump(node)
            await self._record(
                db,
                user_id,
                project,
                "node.iteration_started",
                node=node,
                payload={
                    "iteration": number,
                    "based_on": base.number if base is not None else None,
                    "feedback": feedback[:300],
                    "kept": bool(keep and feedback),
                },
            )
            await db.commit()
            await db.refresh(node)
            return node, note

    async def cancel(self, user_id: uuid.UUID, task_id: uuid.UUID) -> None:
        """The iteration did not start: the task's note goes (a requirement it kept stays)."""
        async with self._sessionmaker() as db:
            node = await get_node(db, user_id, task_id, lock=True)
            if node.next_iteration is not None:
                node.next_iteration = None
                await db.commit()

    async def brief_for(self, user_id: uuid.UUID, task: ProjectNode, evidence: bool = False) -> str:
        """The iteration part of a run's order (:func:`brief`), read fresh for the task.
        ``evidence``: the result is written anew from the run's reads (see :func:`brief`)."""
        async with self._sessionmaker() as db:
            rows = list(
                await db.scalars(
                    select(ProjectNode).where(
                        ProjectNode.user_id == user_id, ProjectNode.parent_id == task.id
                    )
                )
            )
            node = await get_node(db, user_id, task.id)
        results = results_of(rows, node.id)
        note = node.next_iteration or {}
        number = int(note.get("number") or next_number(results))
        base_id = note.get("based_on")
        base = next((r for r in results if str(r.id) == base_id), None) if base_id else None
        return brief(
            number, base, str(note.get("feedback") or ""), node.requirements or [], evidence
        )

    async def file(
        self,
        user_id: uuid.UUID,
        task_id: uuid.UUID,
        report_id: uuid.UUID,
        note: dict[str, Any] | None = None,
    ) -> int | None:
        """A result was filed below the task: it becomes the next iteration (with what the run was
        started with, if it was started as one, or ``note`` for a result made elsewhere, the theme
        workshop) and the task's note is cleared. Returns its number."""
        async with self._sessionmaker() as db:
            node = await get_node(db, user_id, task_id, lock=True)
            report = await get_node(db, user_id, report_id, lock=True)
            if report.parent_id != node.id or report.kind != "report":
                return None
            project = await get_project(db, user_id, node.project_id, lock=True)
            others = results_of(
                await db.scalars(
                    select(ProjectNode).where(
                        ProjectNode.user_id == user_id,
                        ProjectNode.parent_id == node.id,
                        ProjectNode.id != report.id,
                    )
                ),
                node.id,
            )
            given = note is not None
            note = note if note is not None else (node.next_iteration or {})
            report.iteration = max(int(note.get("number") or 0), next_number(others))
            base_id = note.get("based_on")
            if base_id and not any(str(o.id) == str(base_id) for o in others):
                base_id = None  # only a result of the same task
            report.based_on_id = uuid.UUID(str(base_id)) if base_id else None
            report.feedback = str(note.get("feedback") or "") or None
            if not given:
                node.next_iteration = None
            await self._record(
                db,
                user_id,
                project,
                "node.iteration_filed",
                node=report,
                payload={"iteration": report.iteration, "task": node.number},
            )
            await db.commit()
            return report.iteration


# "mach eine v5", "neue Version von 1.8.2", "nächste Iteration", "new version": a further iteration
_ITERATE = re.compile(
    r"\b(v\d{1,3}|version\s*\d{1,3}|(neue|nächste|weitere)[nrs]?\s+(version|fassung|iteration|"
    r"variante|runde)|iterier\w*|iteration|(new|next|another)\s+(version|iteration))\b",
    re.I,
)


def wants_iteration(text: str) -> bool:
    """Does the message ask for a further version of a task's result?"""
    return bool(_ITERATE.search(text))


def iteration_target(
    nodes: Sequence[ProjectNode], numbers: Sequence[str]
) -> tuple[ProjectNode, ProjectNode | None] | None:
    """The task a request for a new iteration is about and the version to continue: a number of a
    result names that result (and its task), a number of a task its newest result. Only a task that
    has a result can iterate; the first number that fits decides."""
    by_number = {str(n.number): n for n in nodes if n.trashed_at is None}
    by_id = {n.id: n for n in nodes}
    for number in numbers:
        node = by_number.get(number)
        if node is None:
            continue
        if node.kind == "report" and node.parent_id in by_id:
            task = by_id[node.parent_id]
            if task.kind == "task":
                return task, node
        if node.kind == "task":
            results = results_of(nodes, node.id)
            if results:
                return node, results[-1]
    return None


def offer_text(
    language: str, task: ProjectNode, base: ProjectNode | None, number: int, kept: Sequence[Any]
) -> str:
    """The hub's own answer to "make the next version": what waits for the click, nothing more."""
    de = language == "de"
    source = f" ({'aus' if de else 'from'} {base.number})" if base is not None else ""
    if de:
        text = (
            f"Ich habe Iteration {number} von {task.number} „{task.title}“{source} vorbereitet. "
            "Auf der Karte steht deine Rückmeldung; kürze sie, wenn nötig, und starte die "
            "Iteration mit einem Klick. Der Aufgabentext bleibt, wie er ist."
        )
    else:
        text = (
            f"I prepared iteration {number} of {task.number} “{task.title}”{source}. The card "
            "holds your feedback; trim it if needed and start the iteration with one click. The "
            "task text stays as it is."
        )
    if kept:
        head = "Vorgaben, die weiter gelten" if de else "Requirements that still hold"
        text += f"\n\n{head}: " + "; ".join(str(r.get("text")) for r in kept) + "."
    return text


def iteration_view(report: ProjectNode, by_id: dict[uuid.UUID, ProjectNode]) -> dict[str, Any]:
    """A result as one iteration in a list (the GUI's history of a task)."""
    base = by_id.get(report.based_on_id) if report.based_on_id else None
    return {
        "id": str(report.id),
        "number": report.number,
        "iteration": report.iteration,
        "based_on": base.number if base is not None else None,
        "feedback": report.feedback,
        "theme": theme_in(report.body) is not None,
        "created_at": report.created_at.isoformat() if report.created_at else None,
    }
