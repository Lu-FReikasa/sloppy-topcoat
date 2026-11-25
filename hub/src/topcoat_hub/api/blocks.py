"""Block programs over HTTP (PRG-00 R3): the catalogue, validation, and runs (admin)."""

import uuid
from typing import Any

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict
from sqlalchemy.ext.asyncio import AsyncSession

from topcoat_hub.auth import CurrentUser, require_admin
from topcoat_hub.blocks import program as prog
from topcoat_hub.blocks.service import BlockError, BlockService, LiveBlockRun
from topcoat_hub.chat_store import get_hub_setting, set_hub_setting
from topcoat_hub.db import get_db

router = APIRouter(prefix="/api/blocks")

CUSTOM_KEY = "block_programs"  # hub_settings: {name: program}
MAX_CUSTOM = 50


async def _custom(db: AsyncSession) -> dict[str, dict[str, Any]]:
    stored = await get_hub_setting(db, CUSTOM_KEY)
    return stored if isinstance(stored, dict) else {}


class SaveRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    program: dict[str, Any]


def _error(status: int, code: str, message: str) -> JSONResponse:
    return JSONResponse({"detail": {"code": code, "message": message}}, status_code=status)


def _service(request: Request) -> BlockService:
    return request.app.state.blocks  # type: ignore[no-any-return]


class ValidateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    program: dict[str, Any]


class RunRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str | None = None  # a program that ships
    program: dict[str, Any] | None = None  # or one written out
    inputs: dict[str, Any]


def _view(run: LiveBlockRun, full: bool = False) -> dict[str, Any]:
    result = run.result
    out: dict[str, Any] = {
        "id": str(run.run_id),
        "program": run.program["name"],
        "status": "running" if not run.finished and result.status == "running" else result.status,
        "error": result.error,
        "reason": result.reason,
        "notes": result.notes,
        "filed": result.filed,
        "variables": {
            k: v if isinstance(v, str | dict | list) else str(v) for k, v in result.vars.items()
        },
        "authors": result.authors,
    }
    if full:
        out["steps"] = result.steps
    else:
        out["step_count"] = len(result.steps)
    return out


@router.get("/catalogue")
async def get_catalogue(_user: CurrentUser = Depends(require_admin)) -> Any:
    return prog.catalogue()


@router.get("/programs")
async def list_programs(
    request: Request,
    db: AsyncSession = Depends(get_db),
    _user: CurrentUser = Depends(require_admin),
) -> Any:
    """The programs that ship (read only: copy one to change it) and the ones saved here."""
    shipped = [
        {
            "name": n,
            "title": p.get("title", n),
            "inputs": p["inputs"],
            "program": p,
            "builtin": True,
        }
        for n, p in _service(request).programs().items()
    ]
    saved = [
        {
            "name": n,
            "title": p.get("title", n),
            "inputs": p["inputs"],
            "program": p,
            "builtin": False,
        }
        for n, p in sorted((await _custom(db)).items())
    ]
    return shipped + saved


@router.put("/programs/{name}")
async def save_program(
    name: str,
    body: SaveRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
    _user: CurrentUser = Depends(require_admin),
) -> Any:
    if name in _service(request).programs():
        return _error(
            409, "builtin", "a program that ships is not changed: save a copy under another name"
        )
    try:
        checked = prog.validate(body.program)
    except prog.ProgramError as exc:
        return _error(422, "invalid_program", str(exc))
    if checked["name"] != name:
        return _error(
            422, "invalid_program", "the name in the program must be the name in the path"
        )
    custom = await _custom(db)
    if name not in custom and len(custom) >= MAX_CUSTOM:
        return _error(409, "too_many", f"at most {MAX_CUSTOM} saved programs")
    await set_hub_setting(db, CUSTOM_KEY, {**custom, name: checked})
    await db.commit()
    return {"saved": name, "program": checked}


@router.delete("/programs/{name}")
async def delete_program(
    name: str,
    db: AsyncSession = Depends(get_db),
    _user: CurrentUser = Depends(require_admin),
) -> Any:
    custom = await _custom(db)
    if name not in custom:
        return _error(404, "no_program", f"there is no saved program {name!r}")
    del custom[name]
    await set_hub_setting(db, CUSTOM_KEY, custom or None)
    await db.commit()
    return {"deleted": name}


@router.post("/validate")
async def validate_program(
    body: ValidateRequest, _user: CurrentUser = Depends(require_admin)
) -> Any:
    try:
        return {"ok": True, "program": prog.validate(body.program)}
    except prog.ProgramError as exc:
        return {"ok": False, "error": str(exc)}


@router.post("/runs", status_code=202)
async def start_run(
    body: RunRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
    user: CurrentUser = Depends(require_admin),
) -> Any:
    service = _service(request)
    program = body.program
    if body.name is not None:
        program = service.programs().get(body.name) or (await _custom(db)).get(body.name)
        if program is None:
            return _error(404, "no_program", f"there is no program {body.name!r}")
    if program is None:
        return _error(422, "invalid_input", "name or program is required")
    try:
        run = service.start(user, program, body.inputs)
    except BlockError as exc:
        return _error(exc.status, exc.code, exc.message)
    return _view(run)


@router.get("/runs/{run_id}")
async def get_run(
    run_id: uuid.UUID, request: Request, user: CurrentUser = Depends(require_admin)
) -> Any:
    run = _service(request).get(user.uuid, run_id)
    if run is None:
        return _error(
            404, "no_run", "there is no such run (it may be gone: runs are kept for a while)"
        )
    return _view(run, full=True)


@router.post("/runs/{run_id}/stop")
async def stop_run(
    run_id: uuid.UUID, request: Request, user: CurrentUser = Depends(require_admin)
) -> Any:
    if not await _service(request).stop(user.uuid, run_id):
        return _error(409, "not_running", "this run is not going on")
    return {"stopped": True}
