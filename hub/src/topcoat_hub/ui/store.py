"""The interface data in the database (D60, D70).

``PrefsStore`` belongs to one person and names them in every statement (D20: no helper takes a
bare id). ``TemplateStore`` and ``ProfileStore`` belong to the installation and are
only reached from the admin API (and, read-only, to compute what a person is offered)."""

import re
import uuid
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import delete, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from topcoat_hub.models import (
    PSEUDO_USER_IDS,
    SYSTEM_ID,
    UiGrant,
    UiProfile,
    UiTemplate,
    User,
)
from topcoat_hub.ui import prefs
from topcoat_hub.ui.access import clean_agent
from topcoat_hub.ui.manifest import Manifest

KEY = re.compile(r"^[a-z0-9][a-z0-9-]{0,39}$")
MAX_MODULES = 24  # views of one program


class UiError(Exception):
    """A change cannot be made; ``code`` says why, ``extra`` goes into the answer."""

    def __init__(self, status: int, code: str, message: str, **extra: Any) -> None:
        self.status, self.code, self.extra = status, code, extra
        super().__init__(message)


class PrefsStore:
    def __init__(self, db: AsyncSession, user_id: uuid.UUID) -> None:
        self.db, self.user_id = db, user_id

    async def get(self) -> dict[str, Any]:
        stored = await self.db.scalar(select(User.ui_prefs).where(User.id == self.user_id))
        return prefs.effective(stored)

    async def apply(self, patch: dict[str, Any]) -> dict[str, Any]:
        stored = await self.db.scalar(select(User.ui_prefs).where(User.id == self.user_id))
        try:
            new = prefs.apply_patch(stored, patch)
        except prefs.PrefsError as exc:
            raise UiError(422, "invalid_prefs", str(exc)) from exc
        await self.db.execute(update(User).where(User.id == self.user_id).values(ui_prefs=new))
        return new


# --- workspaces the installation offers ----------------------------------------------------------


def clean_template_fields(raw: dict[str, Any], manifest: Manifest) -> dict[str, Any]:
    """A template as submitted by an admin, checked and normalised; ``UiError`` 422 names the first
    problem."""
    title = raw.get("title")
    if not isinstance(title, str) or not title.strip():
        raise UiError(422, "title", "a workspace needs a title")
    modules = raw.get("modules")
    if not isinstance(modules, list) or not all(isinstance(m, str) for m in modules):
        raise UiError(422, "modules", "modules is a list of module ids")
    if len(set(modules)) != len(modules):
        raise UiError(422, "modules", "a module is listed twice")
    if len(modules) > MAX_MODULES:
        raise UiError(422, "modules", f"at most {MAX_MODULES} modules")
    unknown = [m for m in modules if m not in manifest]
    if unknown:
        raise UiError(422, "modules", f"unknown module {unknown[0]}")
    opened = raw.get("open")
    if opened is not None:
        if not isinstance(opened, list) or not all(isinstance(m, str) for m in opened):
            raise UiError(422, "open", "open is a list of module ids, or null")
        if len(set(opened)) != len(opened) or any(m not in modules for m in opened):
            raise UiError(422, "open", "open lists modules of the workspace, each once")
    role = raw.get("requires_role")
    if role not in (None, "admin"):
        raise UiError(422, "requires_role", "requires_role is null or 'admin'")
    position = raw.get("position", 100)
    if isinstance(position, bool) or not isinstance(position, int) or not 0 <= position <= 1000:
        raise UiError(422, "position", "position is a number from 0 to 1000")
    icon = raw.get("icon", "")
    if not isinstance(icon, str) or len(icon) > 8:
        raise UiError(422, "icon", "icon is at most 8 characters")
    return {
        "title": " ".join(title.split())[:80],
        "icon": icon,
        "position": position,
        "modules": modules,
        "open": opened,
        "agent": clean_agent(raw.get("agent")),
        "requires_role": role,
    }


class TemplateStore:
    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    async def all(self) -> Sequence[UiTemplate]:
        return (
            await self.db.scalars(select(UiTemplate).order_by(UiTemplate.position, UiTemplate.key))
        ).all()

    async def get(self, key: str) -> UiTemplate | None:
        return await self.db.get(UiTemplate, key)

    async def create(self, key: str, fields: dict[str, Any], user_id: uuid.UUID) -> UiTemplate:
        if not KEY.match(key):
            raise UiError(422, "key", "a key is lower case letters, digits and dashes, at most 40")
        if await self.get(key) is not None:
            raise UiError(409, "key_taken", f"there is a workspace {key} already")
        row = UiTemplate(
            key=key,
            owner_id=SYSTEM_ID,
            rev=1,
            builtin=False,
            updated_by=user_id,
            **fields,
        )
        self.db.add(row)
        await self.db.flush()
        return row

    async def replace(
        self, row: UiTemplate, fields: dict[str, Any], user_id: uuid.UUID
    ) -> UiTemplate:
        if row.builtin:
            raise UiError(
                409, "builtin", "a built-in workspace cannot be changed: duplicate it first"
            )
        for name, value in fields.items():
            setattr(row, name, value)
        row.rev += 1
        row.updated_by = user_id
        row.updated_at = datetime.now(UTC)
        await self.db.flush()
        return row

    async def delete(self, row: UiTemplate) -> None:
        if row.builtin:
            raise UiError(409, "builtin", "a built-in workspace cannot be deleted")
        await self.db.delete(row)
        await self.db.flush()


def clean_grants(raw: Any) -> list[tuple[str, str, str]]:
    """``[{"kind": "workspace"|"tool", "key": …, "effect": "allow"|"deny"}]`` as tuples; the last
    line
    for a (kind, key) wins."""
    if not isinstance(raw, list):
        raise UiError(422, "grants", "grants is a list")
    out: dict[tuple[str, str], str] = {}
    for g in raw:
        if not isinstance(g, dict):
            raise UiError(422, "grants", "a grant is an object")
        kind, key, effect = g.get("kind"), g.get("key"), g.get("effect")
        if kind not in ("workspace", "tool") or effect not in ("allow", "deny"):
            raise UiError(
                422, "grants", "a grant is {kind: workspace|tool, key, effect: allow|deny}"
            )
        if not isinstance(key, str) or not KEY.match(key):
            raise UiError(422, "grants", f"{key!r} is not a valid key")
        out[(kind, key)] = effect
    return [(k, key, e) for (k, key), e in sorted(out.items())]


class ProfileStore:
    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    async def all(self) -> list[tuple[UiProfile, list[UiGrant]]]:
        profiles = (await self.db.scalars(select(UiProfile).order_by(UiProfile.name))).all()
        grants = (await self.db.scalars(select(UiGrant))).all()
        by: dict[uuid.UUID, list[UiGrant]] = {}
        for g in grants:
            by.setdefault(g.profile_id, []).append(g)
        return [(p, sorted(by.get(p.id, []), key=lambda g: (g.kind, g.key))) for p in profiles]

    async def get(self, name: str) -> UiProfile | None:
        return (await self.db.scalars(select(UiProfile).where(UiProfile.name == name))).first()

    async def put(
        self,
        name: str,
        description: str,
        grants: list[tuple[str, str, str]],
        make_default: bool = False,
    ) -> UiProfile:
        name = " ".join(name.split())
        if not name or len(name) > 60:
            raise UiError(422, "name", "a profile name is 1 to 60 characters")
        row = await self.get(name)
        if row is None:
            row = UiProfile(id=uuid.uuid4(), name=name)
            self.db.add(row)
        row.description = description[:2000]
        await self.db.flush()
        await self.db.execute(delete(UiGrant).where(UiGrant.profile_id == row.id))
        for kind, key, effect in grants:
            self.db.add(UiGrant(profile_id=row.id, kind=kind, key=key, effect=effect))
        if make_default and not row.is_default:
            await self.db.execute(update(UiProfile).values(is_default=False))
            row.is_default = True
        await self.db.flush()
        return row

    async def delete(self, row: UiProfile) -> None:
        if row.builtin or row.is_default:
            raise UiError(409, "builtin", "the built-in and the default profile cannot be deleted")
        await self.db.delete(row)
        await self.db.flush()

    async def users(self) -> list[dict[str, Any]]:
        rows = (
            await self.db.execute(
                select(User.id, User.name, User.role, UiProfile.name)
                .outerjoin(UiProfile, UiProfile.id == User.profile_id)
                .where(User.id.notin_(PSEUDO_USER_IDS))
                .order_by(User.name)
            )
        ).all()
        return [{"id": str(i), "name": n, "role": r, "profile": p} for i, n, r, p in rows]

    async def set_user_profile(self, user_id: uuid.UUID, name: str | None) -> None:
        profile_id = None
        if name is not None:
            profile = await self.get(name)
            if profile is None:
                raise UiError(404, "not_found", f"there is no profile {name}")
            profile_id = profile.id
        exists = await self.db.scalar(
            select(func.count()).select_from(User).where(User.id == user_id)
        )
        if not exists:
            raise UiError(404, "not_found", "there is no such person")
        await self.db.execute(update(User).where(User.id == user_id).values(profile_id=profile_id))
