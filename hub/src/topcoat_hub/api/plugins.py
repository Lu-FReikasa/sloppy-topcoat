"""The admin API of the plug-in registry (D105 step 2): list, versions with their diff, install,
update, roll back, disable and remove data plug-ins while the hub runs.

Admin on the GUI only: every route depends on ``require_admin``, which an API key never passes,
not even an admin's. Every change is audited (``plugin_changes``) and published as ``ui_plugins``,
so open pages and ``GET /api/ui/commands`` show it without a restart or a web build."""

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict
from sqlalchemy.exc import IntegrityError

from topcoat_hub import plugin_registry as registry
from topcoat_hub.auth import CurrentUser, require_admin
from topcoat_hub.models import Plugin

router = APIRouter(prefix="/api/plugins")


def _fail(exc: registry.RegistryError) -> HTTPException:
    return HTTPException(
        exc.status, detail={"code": exc.code, "message": str(exc), "problems": exc.problems}
    )


def _conflict() -> HTTPException:
    """Another admin stored the same key or version a moment earlier."""
    return HTTPException(
        409,
        detail={
            "code": "conflict",
            "message": "another change came first; look again and retry",
            "problems": [],
        },
    )


def _publish(request: Request, key: str) -> None:
    request.app.state.bus.publish(registry.EVENT, {"key": key})


def _row(p: Plugin, manifest: dict[str, Any] | None = None) -> dict[str, Any]:
    return {
        "key": p.key,
        "version": p.version,
        "enabled": p.enabled,
        "builtin": p.builtin,
        "title_key": (manifest or {}).get("title_key"),
        "updated_at": p.updated_at.isoformat() if p.updated_at else None,
    }


class EnabledBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool


class RollbackBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    version: str


@router.get("")
async def list_plugins(
    request: Request, _admin: CurrentUser = Depends(require_admin)
) -> dict[str, Any]:
    async with request.app.state.sessionmaker() as db:
        entries = await registry.Registry(db).entries()
    return {"plugins": [_row(e.plugin, e.manifest) for e in entries]}


@router.post("/check")
async def check_manifest(
    manifest: dict[str, Any], request: Request, _admin: CurrentUser = Depends(require_admin)
) -> dict[str, Any]:
    """What is wrong with a manifest, without storing it."""
    async with request.app.state.sessionmaker() as db:
        reg = registry.Registry(db)
        key = manifest.get("id")
        row = await reg.get(key) if isinstance(key, str) else None
        return {"problems": await reg.check(manifest, builtin=bool(row and row.builtin))}


@router.get("/{key}")
async def get_plugin(
    key: str, request: Request, _admin: CurrentUser = Depends(require_admin)
) -> dict[str, Any]:
    """One plug-in with every version (newest first) and its last changes."""
    async with request.app.state.sessionmaker() as db:
        reg = registry.Registry(db)
        row = await reg.get(key)
        if row is None:
            raise HTTPException(404, detail={"code": "not_found", "message": "no such plug-in"})
        versions = await reg.versions(key)
        changes = await reg.changes(key)
    current = next((v.manifest for v, _ in versions if v.version == row.version), None)
    return {
        **_row(row, current),
        "versions": [
            {
                "version": v.version,
                "sha256": v.sha256,
                "installed_by": name,
                "installed_at": v.installed_at.isoformat() if v.installed_at else None,
                "manifest": v.manifest,
            }
            for v, name in versions
        ],
        "changes": [
            {
                "action": c.action,
                "from_version": c.from_version,
                "to_version": c.to_version,
                "by": name,
                "at": c.at.isoformat() if c.at else None,
            }
            for c, name in changes
        ],
    }


@router.get("/{key}/diff")
async def diff_versions(
    key: str,
    a: str,
    b: str,
    request: Request,
    _admin: CurrentUser = Depends(require_admin),
) -> dict[str, Any]:
    """How the manifest of version ``b`` differs from version ``a`` (a unified diff)."""
    async with request.app.state.sessionmaker() as db:
        reg = registry.Registry(db)
        old, new = await reg.version(key, a), await reg.version(key, b)
    if old is None or new is None:
        raise HTTPException(404, detail={"code": "not_found", "message": "no such version"})
    return {"a": a, "b": b, "lines": registry.diff(old.manifest, new.manifest, a, b)}


@router.post("", status_code=201)
async def install_plugin(
    manifest: dict[str, Any], request: Request, admin: CurrentUser = Depends(require_admin)
) -> dict[str, Any]:
    try:
        async with request.app.state.sessionmaker() as db:
            row = await registry.Registry(db).install(manifest, admin.uuid)
            await db.commit()
            view = _row(row, manifest)
    except registry.RegistryError as exc:
        raise _fail(exc) from exc
    except IntegrityError as exc:
        raise _conflict() from exc
    _publish(request, view["key"])
    return view


@router.post("/{key}/versions", status_code=201)
async def update_plugin(
    key: str,
    manifest: dict[str, Any],
    request: Request,
    admin: CurrentUser = Depends(require_admin),
) -> dict[str, Any]:
    try:
        async with request.app.state.sessionmaker() as db:
            row = await registry.Registry(db).update(key, manifest, admin.uuid)
            await db.commit()
            await db.refresh(row)
            view = _row(row, manifest)
    except registry.RegistryError as exc:
        raise _fail(exc) from exc
    except IntegrityError as exc:
        raise _conflict() from exc
    _publish(request, key)
    return view


@router.post("/{key}/rollback")
async def rollback_plugin(
    key: str, body: RollbackBody, request: Request, admin: CurrentUser = Depends(require_admin)
) -> dict[str, Any]:
    try:
        async with request.app.state.sessionmaker() as db:
            row = await registry.Registry(db).rollback(key, body.version, admin.uuid)
            await db.commit()
            await db.refresh(row)
            view = _row(row)
    except registry.RegistryError as exc:
        raise _fail(exc) from exc
    _publish(request, key)
    return view


@router.put("/{key}/enabled")
async def enable_plugin(
    key: str, body: EnabledBody, request: Request, admin: CurrentUser = Depends(require_admin)
) -> dict[str, Any]:
    try:
        async with request.app.state.sessionmaker() as db:
            row = await registry.Registry(db).set_enabled(key, body.enabled, admin.uuid)
            await db.commit()
            await db.refresh(row)
            view = _row(row)
    except registry.RegistryError as exc:
        raise _fail(exc) from exc
    _publish(request, key)
    return view


@router.delete("/{key}")
async def remove_plugin(
    key: str, request: Request, admin: CurrentUser = Depends(require_admin)
) -> JSONResponse:
    try:
        async with request.app.state.sessionmaker() as db:
            await registry.Registry(db).remove(key, admin.uuid)
            await db.commit()
    except registry.RegistryError as exc:
        raise _fail(exc) from exc
    _publish(request, key)
    return JSONResponse({"removed": key})
