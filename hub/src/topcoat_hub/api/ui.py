"""The interface as data over REST (D60, D70): what a person is offered (the programs of the
desktop), their choices, and for admins the programs, profiles and who has which profile.

Every route that belongs to a person works with that person's own row only (``PrefsStore`` names it
in every statement). What a person is offered is computed from their profile; a program they may
not open does not exist for them (404, not 403). Changes reach open pages as events on the stream
(``ui_prefs`` for one person, ``ui_access`` for everybody)."""

import uuid
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.ext.asyncio import AsyncSession

from topcoat_hub import agent_contract
from topcoat_hub import plugins as plugins_mod
from topcoat_hub.auth import CurrentUser, api_user, require_admin
from topcoat_hub.models import UiGrant, UiProfile, UiTemplate, User
from topcoat_hub.ui import prefs as prefs_mod
from topcoat_hub.ui import themes as themes_mod
from topcoat_hub.ui.access import Access, effective_agent, resolve_access
from topcoat_hub.ui.runtime import UiRuntime
from topcoat_hub.ui.store import (
    PrefsStore,
    ProfileStore,
    TemplateStore,
    UiError,
    clean_grants,
    clean_template_fields,
)

router = APIRouter(prefix="/api/ui")


def _fail(exc: UiError) -> HTTPException:
    return HTTPException(exc.status, detail={"code": exc.code, "message": str(exc), **exc.extra})


def _runtime(request: Request) -> UiRuntime:
    runtime: UiRuntime = request.app.state.ui
    return runtime


def _publish(request: Request, event: str, data: dict[str, Any]) -> None:
    request.app.state.bus.publish(event, data)


def _not_found() -> HTTPException:
    return HTTPException(404, detail={"code": "not_found", "message": "there is no such workspace"})


# --- views ----------------------------------------------------------------------------------------


def template_view(t: UiTemplate) -> dict[str, Any]:
    return {
        "key": t.key,
        "title": t.title,
        "title_key": t.title_key,
        "icon": t.icon,
        "position": t.position,
        "modules": t.modules,
        "open": t.open,
        "agent": t.agent,
        "requires_role": t.requires_role,
        "builtin": t.builtin,
        "rev": t.rev,
    }


def workspace_view(t: UiTemplate, access: Access) -> dict[str, Any]:
    """A program as one person sees it."""
    return template_view(t)


async def _visible(db: AsyncSession, user: CurrentUser, key: str) -> tuple[UiTemplate, Access]:
    access = await resolve_access(db, user.uuid, user.role)
    template = await TemplateStore(db).get(key)
    if template is None or not access.workspace_visible(template):
        raise _not_found()
    return template, access


# --- what a person is offered --------------------------------------------------------------------


@router.get("/bootstrap")
async def bootstrap(request: Request, user: CurrentUser = Depends(api_user)) -> dict[str, Any]:
    """Everything the web needs to draw: the person's access, choices (the desktop), the modules
    they may open and the programs they are offered."""
    runtime = _runtime(request)
    async with request.app.state.sessionmaker() as db:
        access = await resolve_access(db, user.uuid, user.role)
        templates = await TemplateStore(db).all()
        prefs = await PrefsStore(db, user.uuid).get()
        themes = await themes_mod.ThemeStore(db).all()
    return {
        "v": 1,
        "access": access.view(),
        "prefs": prefs,
        "themes": [themes_mod.view(t) for t in themes],
        "modules": [
            {**m.view(), "allowed": access.module_allowed(m)}
            for m in runtime.manifest.modules.values()
        ],
        "workspaces": [workspace_view(t, access) for t in templates if access.workspace_visible(t)],
    }


@router.get("/plugins")
async def plugins_list(request: Request, user: CurrentUser = Depends(api_user)) -> dict[str, Any]:
    """The plug-ins a person is offered (D105): for now the built-in programs as manifests."""
    async with request.app.state.sessionmaker() as db:
        access = await resolve_access(db, user.uuid, user.role)
        templates = await TemplateStore(db).all()
    shown = [template_view(t) for t in templates if access.workspace_visible(t)]
    return {"plugins": [plugins_mod.manifest_of(p) for p in shown]}


@router.get("/commands")
async def commands_list(request: Request, user: CurrentUser = Depends(api_user)) -> dict[str, Any]:
    """The one list of commands every menu reads (D105): the palette, a theme's menus, the
    desktop client's selector and tray, the Windows command palette."""
    async with request.app.state.sessionmaker() as db:
        access = await resolve_access(db, user.uuid, user.role)
        templates = await TemplateStore(db).all()
    shown = [
        plugins_mod.manifest_of(template_view(t)) for t in templates if access.workspace_visible(t)
    ]
    return {"commands": plugins_mod.commands(shown, user.role == "admin")}


class PrefsBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    hue: int | None = None
    look: str | None = None
    desktop: dict[str, Any] | None = None


@router.get("/prefs")
async def get_prefs(request: Request, user: CurrentUser = Depends(api_user)) -> dict[str, Any]:
    async with request.app.state.sessionmaker() as db:
        return await PrefsStore(db, user.uuid).get()


@router.put("/prefs")
async def put_prefs(
    body: PrefsBody, request: Request, user: CurrentUser = Depends(api_user)
) -> dict[str, Any]:
    """Change the choices that are present; each value is checked against its list or range."""
    patch = body.model_dump(exclude_none=True)
    try:
        async with request.app.state.sessionmaker() as db:
            new = await PrefsStore(db, user.uuid).apply(patch)
            await db.commit()
    except UiError as exc:
        raise _fail(exc) from exc
    _publish(request, "ui_prefs", {"_user": user.id})
    return new


# --- themes (docs/THEMES.md) ---------------------------------------------------------------------


@router.get("/themes")
async def list_themes(
    request: Request, _user: CurrentUser = Depends(api_user)
) -> list[dict[str, Any]]:
    """The custom themes everybody can choose from (the built-in ones come with the web build)."""
    async with request.app.state.sessionmaker() as db:
        return [themes_mod.view(t) for t in await themes_mod.ThemeStore(db).all()]


@router.post("/themes/check")
async def check_theme(doc: dict[str, Any], _admin: CurrentUser = Depends(require_admin)) -> Any:
    """What is wrong with a theme document, without storing it (the editor asks while typing)."""
    fixed, notes = themes_mod.normalise(doc)
    return {"problems": themes_mod.theme_problems(fixed), "normalised": notes}


@router.put("/themes/{key}")
async def put_theme(
    key: str, doc: dict[str, Any], request: Request, admin: CurrentUser = Depends(require_admin)
) -> Any:
    """Create a theme or replace the one of this key. The document must carry the same key."""
    if doc.get("key") != key:
        raise HTTPException(
            400, detail={"code": "invalid_theme", "message": "the key of the theme must be " + key}
        )
    try:
        async with request.app.state.sessionmaker() as db:
            row, created = await themes_mod.ThemeStore(db).put(doc, admin.uuid)
            await db.commit()
            view = themes_mod.view(row)
    except themes_mod.ThemeError as exc:
        raise HTTPException(
            exc.status,
            detail={"code": "invalid_theme", "message": str(exc), "problems": exc.problems},
        ) from exc
    _publish(request, "ui_themes", {})
    return JSONResponse(view, status_code=201 if created else 200)


@router.delete("/themes/{key}", status_code=204)
async def delete_theme(
    key: str, request: Request, _admin: CurrentUser = Depends(require_admin)
) -> None:
    """Delete a custom theme; whoever had chosen it draws the terminal from now on."""
    async with request.app.state.sessionmaker() as db:
        gone = await themes_mod.ThemeStore(db).delete(key)
        await db.commit()
    if not gone:
        raise HTTPException(404, detail={"code": "not_found", "message": "there is no such theme"})
    _publish(request, "ui_themes", {})


@router.get("/prefs/defaults")
async def prefs_defaults(_user: CurrentUser = Depends(api_user)) -> dict[str, Any]:
    return {"prefs": prefs_mod.DEFAULT_PREFS}


@router.get("/workspaces/{key}/agent")
async def workspace_agent(
    key: str,
    request: Request,
    language: str | None = None,
    user: CurrentUser = Depends(api_user),
) -> dict[str, Any]:
    """What a conversation started in this workspace begins with for this person: the tool scope
    (in the shape the chat API takes) narrowed by their profile, the answer path, the persona, the
    model role. Nothing is applied here; the web passes it on when it starts a conversation.

    ``language`` (``de``/``en``) is the language the GUI shows; without it the contract is in the
    person's stored language, else English."""
    async with request.app.state.sessionmaker() as db:
        template, access = await _visible(db, user, key)
        if language not in agent_contract.LANGUAGES:
            row = await db.get(User, user.uuid)
            language = (row.language if row else None) or "en"
        return {
            "workspace": key,
            **effective_agent(template.agent, access),
            # what the employee of this program is for and may do, in the person's language (1.9)
            "contract": agent_contract.view(key, language),
        }


# --- the admin: workspaces -----------------------------------------------------------------------


class TemplateBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str
    icon: str = ""
    position: int = 100
    modules: list[str]
    agent: dict[str, Any] = Field(default_factory=dict)
    requires_role: str | None = None


class DuplicateBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    key: str
    title: str | None = None


@router.get("/templates")
async def list_templates(
    request: Request, _admin: CurrentUser = Depends(require_admin)
) -> dict[str, Any]:
    async with request.app.state.sessionmaker() as db:
        return {"templates": [template_view(t) for t in await TemplateStore(db).all()]}


@router.get("/templates/{key}")
async def get_template(
    key: str, request: Request, _admin: CurrentUser = Depends(require_admin)
) -> dict[str, Any]:
    async with request.app.state.sessionmaker() as db:
        template = await TemplateStore(db).get(key)
        if template is None:
            raise _not_found()
        return template_view(template)


@router.put("/templates/{key}")
async def put_template(
    key: str, body: TemplateBody, request: Request, admin: CurrentUser = Depends(require_admin)
) -> Any:
    """Create a workspace or replace an own one (a built-in one is duplicated first)."""
    runtime = _runtime(request)
    try:
        fields = clean_template_fields(body.model_dump(), runtime.manifest)
        async with request.app.state.sessionmaker() as db:
            store = TemplateStore(db)
            row = await store.get(key)
            created = row is None
            row = (
                await store.create(key, fields, admin.uuid)
                if row is None
                else await store.replace(row, fields, admin.uuid)
            )
            await db.commit()
            view = template_view(row)
    except UiError as exc:
        raise _fail(exc) from exc
    _publish(request, "ui_access", {})
    return JSONResponse(view, status_code=201 if created else 200)


@router.post("/templates/{key}/duplicate", status_code=201)
async def duplicate_template(
    key: str, body: DuplicateBody, request: Request, admin: CurrentUser = Depends(require_admin)
) -> dict[str, Any]:
    runtime = _runtime(request)
    try:
        async with request.app.state.sessionmaker() as db:
            store = TemplateStore(db)
            source = await store.get(key)
            if source is None:
                raise _not_found()
            fields = clean_template_fields(
                {
                    "title": body.title or f"{source.title} (copy)",
                    "icon": source.icon,
                    "position": source.position,
                    "modules": source.modules,
                    "open": source.open,
                    "agent": source.agent,
                    "requires_role": source.requires_role,
                },
                runtime.manifest,
            )
            row = await store.create(body.key, fields, admin.uuid)
            await db.commit()
            view = template_view(row)
    except UiError as exc:
        raise _fail(exc) from exc
    _publish(request, "ui_access", {})
    return view


@router.delete("/templates/{key}")
async def delete_template(
    key: str, request: Request, _admin: CurrentUser = Depends(require_admin)
) -> dict[str, Any]:
    try:
        async with request.app.state.sessionmaker() as db:
            store = TemplateStore(db)
            row = await store.get(key)
            if row is None:
                raise _not_found()
            await store.delete(row)
            await db.commit()
    except UiError as exc:
        raise _fail(exc) from exc
    _publish(request, "ui_access", {})
    return {"deleted": key}


# --- the admin: profiles and people --------------------------------------------------------------


class GrantBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: str
    key: str
    effect: str


class ProfileBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    description: str = ""
    default: bool = False
    grants: list[GrantBody] = Field(default_factory=list)


def profile_view(p: UiProfile, grants: list[UiGrant]) -> dict[str, Any]:
    return {
        "name": p.name,
        "description": p.description,
        "default": p.is_default,
        "builtin": p.builtin,
        "grants": [{"kind": g.kind, "key": g.key, "effect": g.effect} for g in grants],
    }


@router.get("/profiles")
async def list_profiles(
    request: Request, _admin: CurrentUser = Depends(require_admin)
) -> dict[str, Any]:
    async with request.app.state.sessionmaker() as db:
        rows = await ProfileStore(db).all()
        return {"profiles": [profile_view(p, g) for p, g in rows]}


@router.put("/profiles/{name}")
async def put_profile(
    name: str, body: ProfileBody, request: Request, _admin: CurrentUser = Depends(require_admin)
) -> dict[str, Any]:
    try:
        grants = clean_grants([g.model_dump() for g in body.grants])
        async with request.app.state.sessionmaker() as db:
            store = ProfileStore(db)
            row = await store.put(name, body.description, grants, body.default)
            await db.commit()
            view = profile_view(
                row, [UiGrant(profile_id=row.id, kind=k, key=key, effect=e) for k, key, e in grants]
            )
    except UiError as exc:
        raise _fail(exc) from exc
    _publish(request, "ui_access", {})
    return view


@router.delete("/profiles/{name}")
async def delete_profile(
    name: str, request: Request, _admin: CurrentUser = Depends(require_admin)
) -> dict[str, Any]:
    try:
        async with request.app.state.sessionmaker() as db:
            store = ProfileStore(db)
            row = await store.get(name)
            if row is None:
                raise UiError(404, "not_found", f"there is no profile {name}")
            await store.delete(row)
            await db.commit()
    except UiError as exc:
        raise _fail(exc) from exc
    _publish(request, "ui_access", {})
    return {"deleted": name}


@router.get("/users")
async def list_users(
    request: Request, _admin: CurrentUser = Depends(require_admin)
) -> dict[str, Any]:
    async with request.app.state.sessionmaker() as db:
        return {"users": await ProfileStore(db).users()}


class UserProfileBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    profile: str | None


@router.put("/users/{user_id}/profile")
async def put_user_profile(
    user_id: uuid.UUID,
    body: UserProfileBody,
    request: Request,
    _admin: CurrentUser = Depends(require_admin),
) -> dict[str, Any]:
    try:
        async with request.app.state.sessionmaker() as db:
            await ProfileStore(db).set_user_profile(user_id, body.profile)
            await db.commit()
    except UiError as exc:
        raise _fail(exc) from exc
    _publish(request, "ui_access", {})
    return {"user": str(user_id), "profile": body.profile}
