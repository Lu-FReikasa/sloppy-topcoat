"""The theme workshop (2026-11-17): a theme talked into shape, shown live while it changes.

The page holds the draft and the conversation; the desktop draws the draft at once (the preview
of desktop/preview.ts), so the person sees each change on the real windows. A turn here is one
model call that answers with a **patch** (ui/theme_patch.py): only what changes, so a turn is
short and nothing settled earlier is lost. The hub applies it, checks the result like any theme
(theme_problems, theme_lint) and, when the patch broke something, gives the findings back to the
model once. Nothing is stored by a turn; saving is the person's click (D15) and files the result
as an iteration of the task the workshop was opened for (project_iterations.py)."""

import json
import logging
import time
import uuid
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field

from topcoat_hub.auth import CurrentUser, api_user
from topcoat_hub.gateway import GatewayError
from topcoat_hub.memory import _answer_text as answer_text
from topcoat_hub.project_base import ProjectError
from topcoat_hub.project_events import PERSON
from topcoat_hub.project_iterations import IterationStore, theme_in
from topcoat_hub.project_store import ProjectStore
from topcoat_hub.ui import theme_patch, themes

log = logging.getLogger(__name__)
router = APIRouter(prefix="/api/ui/workshop")

LABEL = "theme_workshop"  # a chore label: the call does not think (gateway.CHORE_LABELS)
MAX_TOKENS = 2500
MAX_HISTORY = 12

SYSTEM = (
    "You are the theme workshop of Topcoat's desktop. The person and you shape ONE theme together; "
    "they see every change on their desktop at once. You get the current theme (JSON), the "
    "requirements that must always hold, the hub's findings on the current theme, the last turns "
    "and the person's request.\n\n"
    "Answer with ONE JSON object and nothing else, a patch of only what changes:\n"
    '{"reply": "<one or two sentences to the person, in their language: what you changed>",\n'
    ' "set": {"name"?, "base"?, "layout"?, "bar"?, "chrome"?, "motion"?},\n'
    ' "vars": {"--bg": "#05070a", "--unused": null},\n'
    ' "rules": [{"selector": ".dt-titlebar", "css": "height: 22px; background: #111;"}],\n'
    ' "keyframes": {"wire-in": "from { opacity: 0; transform: scale(.96); } to { opacity: 1; '
    'transform: none; }"}}\n\n'
    "Rules: leave out every part you do not change. 'css' of a rule replaces that rule's whole "
    "block (write all its declarations, no braces); null removes a rule, variable or keyframes. "
    "Change only what the request asks for and keep the requirements. A rule that names a colour "
    "itself (#d0d0d0, #fff) does not follow the variables: when the colours change, rewrite such "
    "rules too, best with var(--surface), var(--text) and so on. Use only the classes and "
    "variables of the guide below. Animations: a window opens, closes, gets focus ONCE; never "
    "'infinite' on windows or the taskbar, no fill mode on .dt-program, nothing on :hover of a "
    "whole window. When the request is a question, answer in 'reply' and change nothing. Use "
    "'css' (the whole sheet) only when the person asks to start over.\n\n"
    "The guide to themes:\n"
)


class Turn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    role: str = Field(pattern="^(person|workshop)$")
    text: str = Field(max_length=4000)


class TurnBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    theme: dict[str, Any]
    message: str = Field(min_length=1, max_length=4000)
    history: list[Turn] = Field(default_factory=list, max_length=40)
    requirements: list[str] = Field(default_factory=list, max_length=30)
    model: str | None = Field(default=None, max_length=200)


def _messages(body: TurnBody, theme: dict[str, Any], findings: list[str]) -> list[dict[str, str]]:
    parts = [
        "Current theme:\n```json\n" + json.dumps(theme, ensure_ascii=False, indent=2) + "\n```"
    ]
    if body.requirements:
        parts.append("Requirements (always):\n" + "\n".join(f"- {r}" for r in body.requirements))
    if findings:
        parts.append(
            "The hub's findings on the current theme:\n" + "\n".join(f"- {f}" for f in findings)
        )
    if body.history:
        turns = body.history[-MAX_HISTORY:]
        parts.append(
            "Last turns:\n"
            + "\n".join(
                f"{'Person' if t.role == 'person' else 'Workshop'}: {t.text}" for t in turns
            )
        )
    parts.append(f"Request:\n{body.message}")
    return [
        {"role": "system", "content": SYSTEM + themes.schema_text()},
        {"role": "user", "content": "\n\n".join(parts)},
    ]


def check(theme: dict[str, Any]) -> tuple[dict[str, Any], list[str], list[str]]:
    """The theme as the hub would store it, what keeps it from being stored, and the lint."""
    doc, _ = themes.normalise(theme)
    problems = themes.theme_problems(doc)
    return doc, problems, ([] if problems else themes.theme_lint(doc))


async def _model(request: Request, picked: str | None) -> str:
    resolver = request.app.state.gateway.resolver
    choice = await resolver.resolve(
        "answer",
        application="dev",
        picked=picked,
        fallback=await resolver.default_model(),
        if_none="default",
    )
    return str(choice.requested)


async def _ask(request: Request, model: str, messages: list[dict[str, str]]) -> str:
    payload = {
        "model": model,
        "messages": messages,
        "stream": False,
        "temperature": 0.4,
        "max_tokens": MAX_TOKENS,
    }
    data = await request.app.state.gateway.call(model, "/v1/chat/completions", payload, label=LABEL)
    return answer_text(data)[0]


@router.post("/turn")
async def workshop_turn(
    body: TurnBody, request: Request, _user: CurrentUser = Depends(api_user)
) -> dict[str, Any]:
    """One turn: the model's patch applied to the draft and checked. A patch that breaks the theme
    goes back to the model once (a corrected patch to the draft as it was); a patch that keeps the
    theme whole but brings new findings is kept, and the model gets one further patch on top of it
    (live 2026-11-17: a corrected patch to the old draft dropped what the first one had done). If
    the theme still breaks, the draft stays."""
    started = time.monotonic()
    current, problems_before, lint_before = check(body.theme)
    if problems_before and not {"key", "name"} <= set(current):
        raise HTTPException(
            400, detail={"code": "bad_theme", "message": "; ".join(problems_before)}
        )
    model = await _model(request, body.model)
    messages = _messages(body, current, problems_before + lint_before)
    replies: list[str] = []
    changed: list[str] = []
    notes: list[str] = []
    base = current  # what the next patch applies to
    result, problems, lint = current, problems_before, lint_before
    calls = 0
    try:
        for attempt in range(2):
            calls += 1
            text = await _ask(request, model, messages)
            patch = theme_patch.parse_patch(text)
            if patch is None:
                notes.append("the answer held no JSON patch")
                messages = [
                    *messages,
                    {"role": "assistant", "content": text[:4000]},
                    {
                        "role": "user",
                        "content": "Answer with ONE JSON object (the patch), nothing else.",
                    },
                ]
                continue
            draft, done, left = theme_patch.apply_patch(base, patch)
            draft["key"] = current["key"]
            doc, broken, found = check(draft)
            notes += left
            new_lint = [f for f in found if f not in lint_before]
            if not broken:  # kept; a further patch builds on it
                base, result, problems, lint = doc, doc, [], found
                changed += [c for c in done if c not in changed]
                if reply := str(patch.get("reply") or "").strip():
                    replies.append(reply)
            if not broken and not new_lint:
                break
            if attempt == 0:
                ask = (
                    "Your patch would break the theme, it was NOT applied:\n"
                    + "\n".join(f"- {f}" for f in broken)
                    + "\nAnswer with a corrected patch to the theme as given above."
                    if broken
                    else "Your patch is applied. The hub finds on the theme now:\n"
                    + "\n".join(f"- {f}" for f in new_lint)
                    + "\nAnswer with a FURTHER patch that fixes each finding: one rule entry "
                    "for every selector named there. Keep your reply short."
                )
                messages = [
                    *messages,
                    {"role": "assistant", "content": text[:6000]},
                    {"role": "user", "content": ask},
                ]
            elif broken and result is current:
                problems = broken  # the last word: the draft stays, the reason is shown
    except GatewayError as exc:
        raise HTTPException(
            exc.status if 400 <= exc.status < 600 else 502,
            detail={"code": "model_failed", "message": exc.message},
        ) from exc
    applied = result is not current and bool(changed)
    return {
        "theme": result,
        "applied": applied,
        "reply": " ".join(replies),
        "changed": changed if applied else [],
        "notes": notes,
        "problems": problems,
        "lint": lint,
        "model": model,
        "calls": calls,
        "ms": int((time.monotonic() - started) * 1000),
    }


class FixBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    theme: dict[str, Any]


@router.post("/fix-contrast")
async def workshop_fix_contrast(
    body: FixBody, _user: CurrentUser = Depends(api_user)
) -> dict[str, Any]:
    """Without a model: in a dark theme, the rules' own light backgrounds become the variables
    (var(--surface), the stage var(--bg)) and their own dark text var(--text)."""
    current, problems, _ = check(body.theme)
    if problems:
        raise HTTPException(400, detail={"code": "bad_theme", "message": "; ".join(problems)})
    fixed, changed = theme_patch.fix_contrast(current)
    doc, problems, lint = check(fixed)
    if problems:  # cannot happen from a valid theme; refuse rather than hand back a broken one
        raise HTTPException(400, detail={"code": "bad_theme", "message": "; ".join(problems)})
    return {"theme": doc, "changed": changed, "lint": lint}


class Step(BaseModel):
    model_config = ConfigDict(extra="forbid")

    message: str = Field(max_length=4000)
    reply: str = Field(default="", max_length=4000)


class SaveBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    theme: dict[str, Any]
    task_id: uuid.UUID | None = None
    based_on: uuid.UUID | None = None
    steps: list[Step] = Field(default_factory=list, max_length=200)
    store: bool = True  # also store the theme for everybody to choose (admin)


def _result_body(theme: dict[str, Any], steps: list[Step], lint: list[str]) -> str:
    text = (
        f"**Stand:** in der Theme-Werkstatt gebaut ({len(steps)} Schritte)\n\n```json\n"
        + json.dumps(theme, ensure_ascii=False, indent=2)
        + "\n```"
    )
    if lint:
        text += "\n\n## Hinweise\n\n" + "\n".join(f"- {h}" for h in lint)
    if steps:
        text += "\n\n## Verlauf\n\n" + "\n".join(
            f"- **Du:** {s.message.strip()}"
            + (f"\n  **Werkstatt:** {s.reply.strip()}" if s.reply.strip() else "")
            for s in steps
        )
    return text


@router.post("/save")
async def workshop_save(
    body: SaveBody, request: Request, user: CurrentUser = Depends(api_user)
) -> dict[str, Any]:
    """The person's click on "save": the theme is stored (admins) and, when the workshop belongs
    to a task, filed below it as its next iteration, with the conversation that made it."""
    doc, problems, lint = check(body.theme)
    if problems:
        raise HTTPException(400, detail={"code": "bad_theme", "message": "; ".join(problems)})
    out: dict[str, Any] = {"key": doc["key"], "stored": False, "result": None, "iteration": None}
    if body.store:
        if not user.is_admin:
            raise HTTPException(
                403, detail={"code": "forbidden", "message": "only an administrator stores a theme"}
            )
        async with request.app.state.sessionmaker() as db:
            try:
                row, _created = await themes.ThemeStore(db).put(doc, user.uuid)
            except themes.ThemeError as exc:
                raise HTTPException(
                    exc.status, detail={"code": "bad_theme", "message": str(exc)}
                ) from exc
            await db.commit()
            out["stored"] = True
            out["theme"] = themes.view(row)
    if body.task_id is not None:
        store = ProjectStore(request.app.state.sessionmaker, PERSON)
        try:
            task = await store.get_node(user.uuid, body.task_id)
            if task.kind != "task":
                raise HTTPException(
                    400, detail={"code": "not_a_task", "message": "a result belongs below a task"}
                )
            report = await store.create_node(
                user.uuid,
                task.project_id,
                kind="report",
                report_kind="document",
                title=f"Ergebnis: {task.title}"[:200],
                body=_result_body(doc, body.steps, lint),
                parent_id=task.id,
            )
            feedback = "; ".join(s.message.strip() for s in body.steps)[:4000]
            number = await IterationStore(request.app.state.sessionmaker, PERSON).file(
                user.uuid,
                task.id,
                report.id,
                note={
                    "based_on": str(body.based_on) if body.based_on else None,
                    "feedback": feedback,
                },
            )
        except ProjectError as exc:
            raise HTTPException(404, detail={"code": "not_found", "message": str(exc)}) from exc
        out["result"] = str(report.id)
        out["number"] = report.number
        out["iteration"] = number
    return out


@router.get("/start")
async def workshop_start(
    request: Request,
    base: uuid.UUID | None = None,
    key: str | None = None,
    user: CurrentUser = Depends(api_user),
) -> dict[str, Any]:
    """What a workshop begins with: the theme of a result (``base``), a stored theme (``key``), or
    an empty one."""
    if base is not None:
        try:
            node = await ProjectStore(request.app.state.sessionmaker).get_node(user.uuid, base)
        except ProjectError as exc:
            raise HTTPException(404, detail={"code": "not_found", "message": str(exc)}) from exc
        found = theme_in(node.body)
        if found is None:
            raise HTTPException(
                404, detail={"code": "no_theme", "message": "the result holds no theme"}
            )
        doc, _, lint = check(found)
        return {"theme": doc, "lint": lint, "from": node.number}
    if key:
        async with request.app.state.sessionmaker() as db:
            for row in await themes.ThemeStore(db).all():
                if row.key == key:
                    doc, _, lint = check(themes.view(row))
                    return {"theme": doc, "lint": lint, "from": key}
        raise HTTPException(404, detail={"code": "not_found", "message": f"no theme {key!r}"})
    suffix = uuid.uuid4().hex[:4]
    return {
        "theme": theme_patch.starter(f"werkstatt-{suffix}", "Neues Theme"),
        "lint": [],
        "from": None,
    }
