"""Research > Recherchieren (the maintainer, 2026-11-13): start a research from the Research
application itself. It runs the shipped block program ``research_report`` (a thorough agent run,
a check, a revision) as a job of its own; the report lands in the application's shelf
(Ablage > Recherchen). Before, a research only started from a chat in Research with the quality
"thorough" — a balanced chat ran a plain check instead and filled the model's context.

Only that one program, only for the person, only with a question: this is not the admin's
blocks API (``api/blocks.py``), which starts any program."""

import uuid
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field

from topcoat_hub import research_chat, research_profile
from topcoat_hub.auth import CurrentUser, api_user
from topcoat_hub.blocks import builtin as blocks_builtin
from topcoat_hub.blocks.service import BlockError, LiveBlockRun
from topcoat_hub.chat import hub_language
from topcoat_hub.ui.access import resolve_access
from topcoat_hub.ui.store import TemplateStore

router = APIRouter(prefix="/api/research")


class StartBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    question: str = Field(min_length=3, max_length=2000)


async def _may_research(request: Request, user: CurrentUser) -> None:
    """The person has the Research application (its template is visible to them)."""
    async with request.app.state.sessionmaker() as db:
        access = await resolve_access(db, user.uuid, user.role)
        for template in await TemplateStore(db).all():
            if template.key == research_profile.APPLICATION and access.workspace_visible(template):
                return
    raise HTTPException(403, {"code": "forbidden", "message": "Research is not yours to use"})


def _service(request: Request) -> Any:
    service = getattr(request.app.state, "blocks", None)
    if service is None:
        raise HTTPException(503, {"code": "unavailable", "message": "runs are not available"})
    return service


def _view(run: LiveBlockRun) -> dict[str, Any]:
    result = run.result
    question = str(run.inputs.get("question") or "")
    language = hub_language("", question)
    running = not run.finished and result.status == "running"
    return {
        "id": str(run.run_id),
        "question": question,
        "status": "running" if running else result.status,
        "progress": research_chat.progress_line(run, language) if running else None,
        "error": result.error or (result.reason if result.status == "failed" else None),
        # the report it filed in the shelf (the last one is the revised report); a run that failed
        # after the research filed its report still leads to it
        "report_id": result.filed[-1]
        if result.filed
        else (str(rid) if (rid := getattr(run.engine.ports, "report_id", None)) else None),
    }


@router.post("/runs", status_code=202)
async def start(body: StartBody, request: Request, user: CurrentUser = Depends(api_user)) -> Any:
    await _may_research(request, user)
    try:
        run = _service(request).start(
            user,
            blocks_builtin.PROGRAMS[research_chat.PROGRAM],
            {"question": body.question.strip()},
        )
    except BlockError as exc:
        raise HTTPException(exc.status, {"code": exc.code, "message": exc.message}) from exc
    return _view(run)


@router.get("/runs")
async def listing(request: Request, user: CurrentUser = Depends(api_user)) -> dict[str, Any]:
    """The person's researches the hub still holds: running ones and those that ended a little
    while ago (their reports stay in the shelf for good)."""
    runs = _service(request).runs_of(user.uuid, research_chat.PROGRAM)
    return {"runs": [_view(r) for r in runs]}


@router.post("/runs/{run_id}/stop")
async def stop(run_id: uuid.UUID, request: Request, user: CurrentUser = Depends(api_user)) -> Any:
    stopped = await _service(request).stop(user.uuid, run_id)
    if not stopped:
        raise HTTPException(404, {"code": "no_run", "message": "there is no such research"})
    return {"stopped": True}
