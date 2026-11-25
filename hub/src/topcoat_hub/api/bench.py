"""The bench over HTTP (PRG-00 R6): suites, runs and ratings (admin)."""

import uuid
from typing import Any

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from topcoat_hub.auth import CurrentUser, require_admin
from topcoat_hub.bench import suggest, suites, variants
from topcoat_hub.bench.service import BenchError, BenchService, latest_ratings

router = APIRouter(prefix="/api/bench")


def _error(status: int, code: str, message: str) -> JSONResponse:
    return JSONResponse({"detail": {"code": code, "message": message}}, status_code=status)


def _service(request: Request) -> BenchService:
    return request.app.state.bench  # type: ignore[no-any-return]


class StartRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    model: str = Field(min_length=1, max_length=255)
    node: str = Field(min_length=1, max_length=63)
    suites: list[str] = Field(min_length=1, max_length=20)
    variant: str = Field("", max_length=40)  # a preset of bench/variants.py


@router.get("/suites")
async def list_suites(_user: CurrentUser = Depends(require_admin)) -> Any:
    return suites.listing()


@router.get("/variants")
async def list_variants(_user: CurrentUser = Depends(require_admin)) -> Any:
    """The settings a run can try (one changed, the rest as every day)."""
    return variants.listing()


@router.get("/ratings")
async def get_ratings(request: Request, _user: CurrentUser = Depends(require_admin)) -> Any:
    """The newest rating of every model, node and suite."""
    return await latest_ratings(request.app.state.sessionmaker)


@router.get("/suggestion")
async def get_suggestion(request: Request, _user: CurrentUser = Depends(require_admin)) -> Any:
    """A better default model of the pool, if the ratings show one (null otherwise). It changes
    nothing: the admin sets the model per application in the table of applications."""
    state = request.app.state
    pool = {v.name: set(v.models) for v in await state.gateway.node_views() if v.connected}
    default = await state.gateway.resolver.default_model()
    return {
        "suggestion": suggest.suggest_default(
            await latest_ratings(state.sessionmaker), default, pool
        )
    }


@router.get("/runs")
async def list_runs(request: Request, _user: CurrentUser = Depends(require_admin)) -> Any:
    return [r.view() for r in _service(request).runs()]


@router.post("/runs", status_code=202)
async def start_run(
    body: StartRequest, request: Request, user: CurrentUser = Depends(require_admin)
) -> Any:
    try:
        run = await _service(request).start(
            user.uuid, body.model, body.node, body.suites, body.variant
        )
    except BenchError as exc:
        return _error(exc.status, exc.code, exc.message)
    return run.view()


@router.get("/runs/{run_id}")
async def get_run(
    run_id: uuid.UUID, request: Request, _user: CurrentUser = Depends(require_admin)
) -> Any:
    run = _service(request).get(run_id)
    if run is None:
        return _error(404, "unknown_run", "no such run (runs are kept for an hour)")
    return run.view()


@router.delete("/runs/{run_id}")
async def stop_run(
    run_id: uuid.UUID, request: Request, _user: CurrentUser = Depends(require_admin)
) -> Any:
    if _service(request).get(run_id) is None:
        return _error(404, "unknown_run", "no such run")
    stopped = await _service(request).stop(run_id)
    return {"stopped": stopped}
