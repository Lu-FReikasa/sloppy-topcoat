"""The parameters an admin changes while the hub runs (E7-03, D58): what can be changed, its value
now and by default, a change, a reset, the history, and a preset to keep and put back.

Admin only. Only what ``live_settings.PARAMS`` lists can be reached; a secret, a path or an address
is not on the list, whatever the request names."""

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict

from topcoat_hub.auth import CurrentUser, require_admin
from topcoat_hub.live_settings import LiveSettings, SettingError

router = APIRouter(prefix="/api/admin/params")


class ParamValue(BaseModel):
    model_config = ConfigDict(extra="forbid")

    value: Any


class ParamPreset(BaseModel):
    model_config = ConfigDict(extra="forbid")

    version: int = 1
    values: dict[str, Any]


def _live(request: Request) -> LiveSettings:
    live: LiveSettings = request.app.state.live
    return live


def _error(exc: SettingError) -> HTTPException:
    status = 404 if exc.code == "unknown" else 422
    return HTTPException(status, detail={"code": exc.code, "message": str(exc)})


@router.get("")
async def list_params(request: Request, _admin: CurrentUser = Depends(require_admin)) -> Any:
    return _live(request).view()


@router.get("/export")
async def export_params(request: Request, _admin: CurrentUser = Depends(require_admin)) -> Any:
    return _live(request).export()


@router.put("/import")
async def import_params(
    body: ParamPreset, request: Request, admin: CurrentUser = Depends(require_admin)
) -> Any:
    """Put a preset back: every value is checked first, and nothing is changed if one is not
    acceptable. Parameters that are overridden now and not in the preset keep their value."""
    live = _live(request)
    try:
        checked = {k: live.check(k, v) for k, v in body.values.items()}
    except SettingError as exc:
        raise _error(exc) from exc
    for key, value in checked.items():
        await live.set(key, value, admin.uuid)
    return {"applied": sorted(checked)}


@router.put("/{key}")
async def set_param(
    key: str, body: ParamValue, request: Request, admin: CurrentUser = Depends(require_admin)
) -> Any:
    live = _live(request)
    try:
        value = await live.set(key, body.value, admin.uuid)
    except SettingError as exc:
        raise _error(exc) from exc
    return {"key": key, "value": value, "overridden": True}


@router.delete("/{key}")
async def reset_param(
    key: str, request: Request, admin: CurrentUser = Depends(require_admin)
) -> Any:
    live = _live(request)
    try:
        value = await live.reset(key, admin.uuid)
    except SettingError as exc:
        raise _error(exc) from exc
    return {"key": key, "value": value, "overridden": False}


@router.get("/{key}/history")
async def param_history(
    key: str, request: Request, _admin: CurrentUser = Depends(require_admin)
) -> Any:
    try:
        return await _live(request).history(key)
    except SettingError as exc:
        raise _error(exc) from exc
