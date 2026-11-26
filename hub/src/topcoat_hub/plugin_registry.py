"""The registry of data plug-ins (D105 step 2): install, update, roll back, disable and remove a
plug-in while the hub runs, every version kept, every change audited.

A data plug-in is a manifest (``proto/plugin/manifest.schema.json``), nothing else: no code reaches
the hub or the GUI. What a manifest changes today:

- the commands every menu reads (``GET /api/ui/commands``) and the plug-ins a person is offered
  (``GET /api/ui/plugins``);
- the tool ceiling of the program it describes, and only downwards. ``agent.tools_ceiling`` may
  leave out tools of the program's contract (``proto/agents/``, D118), never add one; a plug-in
  that is not a program has no contract and so no tools. The person's grants (D20) narrow it
  further where a turn's scope is settled (``app_scope.ceiling``), as before.

The built-in programs are registered as version ``1.0.0`` at start, derived from their rows in
``ui_templates``; that version belongs to the code and follows it. An admin's versions are never
edited. The admin API is in ``api/plugins.py``; every change is published as ``ui_plugins``.
"""

from __future__ import annotations

import difflib
import hashlib
import json
import logging
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from topcoat_hub import agent_contract, plugins, tool_groups
from topcoat_hub.models import Plugin, PluginChange, PluginVersion, UiTemplate, User

log = logging.getLogger(__name__)

BUILTIN_VERSION = "1.0.0"
VERSION_MAX = 20  # the length of the version columns
INTERNET = plugins.INTERNET
EVENT = "ui_plugins"


class RegistryError(Exception):
    """A change cannot be made; ``code`` says why, ``problems`` what is wrong with a manifest."""

    def __init__(
        self, status: int, code: str, message: str, problems: list[str] | None = None
    ) -> None:
        self.status, self.code, self.problems = status, code, problems or []
        super().__init__(message)


# --- manifests ------------------------------------------------------------------------------------


def canonical(manifest: dict[str, Any]) -> str:
    return json.dumps(manifest, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def sha256(manifest: dict[str, Any]) -> str:
    return hashlib.sha256(canonical(manifest).encode("utf-8")).hexdigest()


def version_key(version: str) -> tuple[int, ...]:
    return tuple(int(p) for p in version.split("."))


def program_view(t: UiTemplate) -> dict[str, Any]:
    """What :func:`plugins.manifest_of` reads of a program row."""
    return {
        "key": t.key,
        "title_key": t.title_key,
        "icon": t.icon,
        "modules": t.modules,
        "agent": t.agent,
        "requires_role": t.requires_role,
        "builtin": t.builtin,
        "rev": t.rev,
    }


def reference_ceiling(key: str, template: UiTemplate | None) -> list[str]:
    """The most a plug-in ``key`` may name in ``agent.tools_ceiling``: its program's contract
    (D118), else the ceiling of the program it describes, else nothing."""
    contract = agent_contract.get(key)
    if contract is not None:
        return [*contract.groups, *([INTERNET] if contract.internet else [])]
    return [] if template is None else plugins.agent_ceiling(template.agent)


def ceiling_of(manifest: dict[str, Any] | None) -> list[str] | None:
    """The tool ceiling a manifest states, or None when it states none (no narrowing)."""
    agent = (manifest or {}).get("agent")
    if not isinstance(agent, dict) or not isinstance(agent.get("tools_ceiling"), list):
        return None
    return [str(t) for t in agent["tools_ceiling"]]


def widening(manifest: dict[str, Any], reference: Sequence[str]) -> list[str]:
    """The tools a manifest names beyond ``reference`` (empty when it only narrows)."""
    allowed = set(reference)
    return [t for t in ceiling_of(manifest) or [] if t not in allowed]


def narrow_agent(agent: Any, ceiling: Sequence[str] | None) -> dict[str, Any]:
    """The agent block of a program with its tool scope cut down to ``ceiling`` (None: as it is).
    Only removes: a group the program does not allow never comes back, and the web stays off
    when the program has it off."""
    agent = dict(agent) if isinstance(agent, dict) else {}
    if ceiling is None:
        return agent
    scope = dict(agent.get("tool_scope") or {}) if isinstance(agent.get("tool_scope"), dict) else {}
    have = scope.get("groups")
    wanted = list(tool_groups.SELECTABLE) if have is None else list(have)
    scope["groups"] = [g for g in wanted if g in ceiling]
    if INTERNET not in ceiling:
        scope["internet"] = False
    scope.setdefault("nodes", None)
    agent["tool_scope"] = scope
    return agent


def diff(old: dict[str, Any] | None, new: dict[str, Any] | None, a: str, b: str) -> list[str]:
    """A unified diff of two manifests, one key per line (for the admin page)."""

    def lines(m: dict[str, Any] | None) -> list[str]:
        return [] if m is None else json.dumps(m, indent=2, sort_keys=True).splitlines()

    return list(difflib.unified_diff(lines(old), lines(new), a, b, lineterm="", n=2))


async def program_agent(db: AsyncSession, template: UiTemplate) -> dict[str, Any]:
    """The agent block of a program with the ceiling of its plug-in applied (D105): what every
    place that reads a program's tool ceiling starts from. Only narrows (:func:`narrow_agent`)."""
    return narrow_agent(template.agent, await Registry(db).ceiling(template.key))


# --- the store ------------------------------------------------------------------------------------


@dataclass
class Entry:
    plugin: Plugin
    manifest: dict[str, Any]
    sha256: str


class Registry:
    """The registry over one session; the caller commits and publishes the event."""

    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    async def entries(self) -> list[Entry]:
        """Every plug-in with the manifest of the version in force."""
        rows = (
            await self.db.execute(
                select(Plugin, PluginVersion)
                .join(
                    PluginVersion,
                    (PluginVersion.plugin_key == Plugin.key)
                    & (PluginVersion.version == Plugin.version),
                )
                .order_by(Plugin.builtin.desc(), Plugin.key)
            )
        ).all()
        return [Entry(p, v.manifest, v.sha256) for p, v in rows]

    async def state(self) -> tuple[dict[str, dict[str, Any]], set[str]]:
        """The enabled manifests by key, and the keys of every registered plug-in."""
        entries = await self.entries()
        return (
            {e.plugin.key: e.manifest for e in entries if e.plugin.enabled},
            {e.plugin.key for e in entries},
        )

    async def get(self, key: str) -> Plugin | None:
        return await self.db.get(Plugin, key)

    async def versions(self, key: str) -> list[tuple[PluginVersion, str | None]]:
        """Every version of a plug-in, newest first, with the name of who installed it."""
        rows = (
            await self.db.execute(
                select(PluginVersion, User.name)
                .outerjoin(User, User.id == PluginVersion.installed_by)
                .where(PluginVersion.plugin_key == key)
            )
        ).all()
        out = [(v, name) for v, name in rows]
        out.sort(key=lambda r: version_key(r[0].version), reverse=True)
        return out

    async def changes(self, key: str, limit: int = 50) -> list[tuple[PluginChange, str | None]]:
        rows = (
            await self.db.execute(
                select(PluginChange, User.name)
                .outerjoin(User, User.id == PluginChange.user_id)
                .where(PluginChange.plugin_key == key)
                .order_by(PluginChange.id.desc())
                .limit(limit)
            )
        ).all()
        return [(c, name) for c, name in rows]

    async def version(self, key: str, version: str) -> PluginVersion | None:
        return await self.db.scalar(
            select(PluginVersion).where(
                PluginVersion.plugin_key == key, PluginVersion.version == version
            )
        )

    async def ceiling(self, key: str) -> list[str] | None:
        """The tool ceiling the enabled plug-in ``key`` puts on its program (None: none)."""
        row = (
            await self.db.execute(
                select(PluginVersion.manifest)
                .join(
                    Plugin,
                    (PluginVersion.plugin_key == Plugin.key)
                    & (PluginVersion.version == Plugin.version),
                )
                .where(Plugin.key == key, Plugin.enabled.is_(True))
            )
        ).scalar_one_or_none()
        return ceiling_of(row)

    # --- checks -------------------------------------------------------------------------------

    async def check(self, manifest: Any, builtin: bool = False) -> list[str]:
        """What is wrong with a manifest before it may be stored: the schema and the rule that a
        plug-in only narrows rights. ``builtin`` is whether the key is a built-in program."""
        found = plugins.problems(manifest)
        if found or not isinstance(manifest, dict):
            return found or ["(root): a manifest is an object"]
        key = manifest["id"]
        if len(manifest["version"]) > VERSION_MAX:  # the column holds no more (plugins.version)
            found.append(f"version: at most {VERSION_MAX} characters")
        if manifest.get("builtin") and not builtin:
            found.append("builtin: only the hub registers built-in programs")
        template = await self.db.get(UiTemplate, key)
        beyond = widening(manifest, reference_ceiling(key, template))
        if beyond:
            found.append(
                "agent.tools_ceiling: a plug-in may only narrow the tools of its program, "
                f"never add: {', '.join(beyond)}"
            )
        return found

    async def _checked(self, manifest: Any, builtin: bool) -> dict[str, Any]:
        found = await self.check(manifest, builtin)
        if found:
            raise RegistryError(422, "invalid_manifest", "the manifest is not valid", found)
        clean: dict[str, Any] = dict(manifest)
        clean["builtin"] = builtin
        return clean

    # --- changes ------------------------------------------------------------------------------

    def _audit(
        self,
        key: str,
        action: str,
        user_id: uuid.UUID | None,
        old: str | None,
        new: str | None,
        digest: str | None = None,
    ) -> None:
        self.db.add(
            PluginChange(
                plugin_key=key,
                action=action,
                from_version=old,
                to_version=new,
                sha256=digest,
                user_id=user_id,
            )
        )
        log.info("plug-ins: %s %s %s -> %s", action, key, old, new)

    async def install(self, manifest: Any, user_id: uuid.UUID) -> Plugin:
        key = manifest.get("id") if isinstance(manifest, dict) else None
        if isinstance(key, str) and await self.get(key) is not None:
            raise RegistryError(
                409, "exists", f"the plug-in {key!r} is installed; upload a new version instead"
            )
        clean = await self._checked(manifest, builtin=False)
        digest = sha256(clean)
        row = Plugin(key=clean["id"], version=clean["version"], enabled=True, builtin=False)
        self.db.add(row)
        await self.db.flush()
        self.db.add(
            PluginVersion(
                plugin_key=row.key,
                version=row.version,
                manifest=clean,
                sha256=digest,
                installed_by=user_id,
            )
        )
        self._audit(row.key, "install", user_id, None, row.version, digest)
        await self.db.flush()
        return row

    async def _require(self, key: str) -> Plugin:
        row = await self.get(key)
        if row is None:
            raise RegistryError(404, "not_found", "there is no such plug-in")
        return row

    async def update(self, key: str, manifest: Any, user_id: uuid.UUID) -> Plugin:
        row = await self._require(key)
        if isinstance(manifest, dict) and manifest.get("id") != key:
            raise RegistryError(
                422, "invalid_manifest", "the manifest is of another plug-in", [f"id: not {key!r}"]
            )
        clean = await self._checked(manifest, builtin=row.builtin)
        newest = max(
            (version_key(v.version) for v, _ in await self.versions(key)), default=(0, 0, 0)
        )
        if version_key(clean["version"]) <= newest:
            raise RegistryError(
                409,
                "version_exists",
                "a new version needs a higher number than every stored one",
                [f"version: {clean['version']} is not above {'.'.join(map(str, newest))}"],
            )
        digest = sha256(clean)
        self.db.add(
            PluginVersion(
                plugin_key=key,
                version=clean["version"],
                manifest=clean,
                sha256=digest,
                installed_by=user_id,
            )
        )
        old, row.version, row.updated_at = row.version, clean["version"], func.now()
        self._audit(key, "update", user_id, old, row.version, digest)
        await self.db.flush()
        return row

    async def rollback(self, key: str, version: str, user_id: uuid.UUID) -> Plugin:
        row = await self._require(key)
        target = await self.version(key, version)
        if target is None:
            raise RegistryError(404, "not_found", "this plug-in has no such version")
        if version == row.version:
            raise RegistryError(409, "current", "this version is already in force")
        # the rights rule holds at every change: a version from before a contract was narrowed
        # may not come back wider than the contract is now
        template = await self.db.get(UiTemplate, key)
        beyond = widening(target.manifest, reference_ceiling(key, template))
        if beyond:
            raise RegistryError(
                422,
                "invalid_manifest",
                "this version names tools its program may no longer use",
                [f"agent.tools_ceiling: {', '.join(beyond)}"],
            )
        old, row.version, row.updated_at = row.version, version, func.now()
        self._audit(key, "rollback", user_id, old, version, target.sha256)
        await self.db.flush()
        return row

    async def set_enabled(self, key: str, enabled: bool, user_id: uuid.UUID) -> Plugin:
        row = await self._require(key)
        if row.builtin:
            raise RegistryError(
                409,
                "builtin",
                "a built-in program cannot be switched off here; a profile decides who sees it",
            )
        if row.enabled != enabled:
            row.enabled, row.updated_at = enabled, func.now()
            self._audit(key, "enable" if enabled else "disable", user_id, row.version, row.version)
            await self.db.flush()
        return row

    async def remove(self, key: str, user_id: uuid.UUID) -> None:
        row = await self._require(key)
        if row.builtin:
            raise RegistryError(409, "builtin", "a built-in program cannot be removed")
        self._audit(key, "remove", user_id, row.version, None)
        await self.db.delete(row)  # its versions go with it (ON DELETE CASCADE)
        await self.db.flush()


# --- the built-in programs ------------------------------------------------------------------------


def builtin_manifest(template: UiTemplate) -> dict[str, Any]:
    manifest = plugins.manifest_of(program_view(template), version=BUILTIN_VERSION)
    manifest["builtin"] = True
    return manifest


async def ensure_builtin(sessionmaker: async_sessionmaker[AsyncSession]) -> int:
    """Register every built-in program as a plug-in of version 1.0.0 (D105), and keep that
    version in step with the code. A program the code no longer ships leaves the registry. An
    admin's later versions, and which version is in force, stay as they are. Returns how many
    rows were written."""
    written = 0
    async with sessionmaker() as db:
        templates = (await db.scalars(select(UiTemplate).where(UiTemplate.builtin.is_(True)))).all()
        keys = {t.key for t in templates}
        for template in templates:
            manifest = builtin_manifest(template)
            if plugins.problems(manifest):  # a program row the schema cannot describe stays out
                log.warning("plug-ins: the built-in program %r is no valid manifest", template.key)
                continue
            digest = sha256(manifest)
            row = await db.get(Plugin, template.key)
            if row is None:
                db.add(Plugin(key=template.key, version=BUILTIN_VERSION, builtin=True))
                await db.flush()
                db.add(
                    PluginVersion(
                        plugin_key=template.key,
                        version=BUILTIN_VERSION,
                        manifest=manifest,
                        sha256=digest,
                    )
                )
                db.add(
                    PluginChange(
                        plugin_key=template.key,
                        action="register",
                        to_version=BUILTIN_VERSION,
                        sha256=digest,
                    )
                )
                written += 1
                continue
            base = await Registry(db).version(template.key, BUILTIN_VERSION)
            if base is None:
                db.add(
                    PluginVersion(
                        plugin_key=template.key,
                        version=BUILTIN_VERSION,
                        manifest=manifest,
                        sha256=digest,
                    )
                )
                written += 1
            elif base.sha256 != digest:
                base.manifest, base.sha256 = manifest, digest
                written += 1
        gone = (
            await db.scalars(
                select(Plugin.key).where(
                    Plugin.builtin.is_(True), Plugin.key.not_in(sorted(keys) or [""])
                )
            )
        ).all()
        if gone:
            await db.execute(delete(Plugin).where(Plugin.key.in_(gone)))
            written += len(gone)
        await db.commit()
    return written


# --- what a person is offered ---------------------------------------------------------------------


def offered(
    templates: Sequence[UiTemplate],
    visible: set[str],
    active: dict[str, dict[str, Any]],
    registered: set[str],
    admin: bool,
) -> list[dict[str, Any]]:
    """The manifests a person is offered: each program they may see (its plug-in's manifest in
    force, or the one derived from its row when it is not registered, as an admin's own program),
    and the enabled plug-ins that are no program. A disabled plug-in offers nothing."""
    out: list[dict[str, Any]] = []
    keys: set[str] = set()
    for t in templates:
        keys.add(t.key)
        if t.key not in visible:
            continue
        if t.key in registered:
            if t.key in active:
                out.append(active[t.key])
        else:
            out.append(plugins.manifest_of(program_view(t)))
    for key, manifest in active.items():
        if key in keys:
            continue
        if manifest.get("requires_role") == "admin" and not admin:
            continue
        out.append(manifest)
    return out


def commands(
    manifests: list[dict[str, Any]], programs: set[str], admin: bool
) -> list[dict[str, Any]]:
    """The commands of the offered plug-ins, without any that open a program the person may not
    see: a plug-in cannot give a way into what the person's profile keeps closed."""
    return [
        c
        for c in plugins.commands(manifests, admin)
        if not isinstance(c.get("action", {}).get("program"), str)
        or c["action"]["program"] in programs
    ]
