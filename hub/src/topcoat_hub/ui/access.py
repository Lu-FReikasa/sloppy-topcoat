"""Who may open which workspace and which tool group (ARCHITECTURE D60).

A profile is a set of lines: a **workspace** line allows one (off unless allowed), a **tool** line
denies one group (on unless denied); deny wins. The admin role ignores profiles and is never
locked. Rights only narrow: the hub's own per-person grants for capabilities (D20) stay the gate of
every call, and what is computed here only takes tool groups away from a workspace's defaults.
Hiding a window is a courtesy; the calls behind it are checked again where they are made."""

import uuid
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from topcoat_hub import tool_groups
from topcoat_hub.models import UiGrant, UiProfile, UiTemplate, User
from topcoat_hub.ui.manifest import ModuleInfo

# tool groups a workspace may already name although no capability exists yet (the office, music and
# image workspaces need them); they are kept in a template and reported as "planned"
PLANNED_GROUPS = ("docs", "mail", "image", "chords")
# the built-in programs every person has, whatever their profile says (docs/DESKTOP.md)
EVERYONE = ("incognito", "settings")
GROUP_KEYS = (*tool_groups.KEYS, *PLANNED_GROUPS)
MODEL_ROLES = ("fast", "deep")


@dataclass(frozen=True)
class Access:
    user_id: uuid.UUID
    admin: bool
    profile: str | None = None
    workspaces: frozenset[str] = frozenset()
    denied_workspaces: frozenset[str] = frozenset()
    denied_tools: frozenset[str] = frozenset()

    def workspace_visible(self, template: UiTemplate) -> bool:
        if self.admin:
            return True
        if template.requires_role is not None:
            return False
        if template.builtin and template.key in EVERYONE:  # Incognito, Settings
            return True
        return template.key in self.workspaces and template.key not in self.denied_workspaces

    def tool_allowed(self, group: str) -> bool:
        return self.admin or group not in self.denied_tools

    def module_allowed(self, info: ModuleInfo) -> bool:
        if self.admin:
            return True
        return not info.admin and (info.needs is None or self.tool_allowed(info.needs))

    def view(self) -> dict[str, Any]:
        return {
            "admin": self.admin,
            "profile": self.profile,
            "denied_tools": sorted(self.denied_tools) if not self.admin else [],
        }


async def resolve_access(db: AsyncSession, user_id: uuid.UUID, role: str) -> Access:
    if role == "admin":
        return Access(user_id=user_id, admin=True)
    profile_id = await db.scalar(select(User.profile_id).where(User.id == user_id))
    stmt = select(UiProfile)
    stmt = (
        stmt.where(UiProfile.id == profile_id)
        if profile_id is not None
        else stmt.where(UiProfile.is_default.is_(True))
    )
    profile = (await db.scalars(stmt)).first()
    if profile is None:  # no profile at all: nothing is offered
        return Access(user_id=user_id, admin=False)
    grants = (await db.scalars(select(UiGrant).where(UiGrant.profile_id == profile.id))).all()
    allowed = {g.key for g in grants if g.kind == "workspace" and g.effect == "allow"}
    denied_ws = {g.key for g in grants if g.kind == "workspace" and g.effect == "deny"}
    denied_tools = {g.key for g in grants if g.kind == "tool" and g.effect == "deny"}
    return Access(
        user_id=user_id,
        admin=False,
        profile=profile.name,
        workspaces=frozenset(allowed),
        denied_workspaces=frozenset(denied_ws),
        denied_tools=frozenset(denied_tools),
    )


FRAME_TONE_MAX = 400
FRAME_RULE_MAX = 240
FRAME_RULES = 8


def clean_frame(raw: Any) -> dict[str, Any] | None:
    """The frame of a program (D74): the tone its assistant takes and the rules it keeps, set by an
    admin for everybody who uses the program; None when it has none."""
    if not isinstance(raw, dict):
        return None
    tone = " ".join(str(raw.get("tone") or "").split())[:FRAME_TONE_MAX]
    rules = [
        " ".join(r.split())[:FRAME_RULE_MAX]
        for r in (raw.get("rules") if isinstance(raw.get("rules"), list) else [])
        if isinstance(r, str) and r.strip()
    ][:FRAME_RULES]
    return {"tone": tone, "rules": rules} if tone or rules else None


def frame_text(frame: dict[str, Any] | None) -> str:
    """The frame as it stands in the core block."""
    if not frame:
        return ""
    lines = [f"- Tone: {frame['tone']}"] if frame.get("tone") else []
    lines += [f"- {r}" for r in frame.get("rules", [])]
    return "\n".join(lines)


def clean_agent(raw: Any) -> dict[str, Any]:
    """The agent defaults of a template as they are stored: only known keys, groups from the known
    list, a model role from the two roles."""
    raw = raw if isinstance(raw, dict) else {}
    scope = raw.get("tool_scope") if isinstance(raw.get("tool_scope"), dict) else {}
    groups = scope.get("groups")
    out_scope: dict[str, Any] = {
        "nodes": [n for n in scope["nodes"] if isinstance(n, str)][:100]
        if isinstance(scope.get("nodes"), list)
        else None,
        "groups": [g for g in GROUP_KEYS if g in set(groups)] if isinstance(groups, list) else None,
        "internet": scope["internet"] if isinstance(scope.get("internet"), bool) else None,
        "memory": scope["memory"] if isinstance(scope.get("memory"), bool) else None,
    }
    persona = raw.get("persona")
    role = raw.get("model_role")
    return {
        "tool_scope": out_scope,
        "persona": persona[:80] if isinstance(persona, str) and persona else None,
        "model_role": role if role in MODEL_ROLES else None,
        "frame": clean_frame(raw.get("frame")),
    }


def effective_agent(agent: dict[str, Any], access: Access) -> dict[str, Any]:
    """What a conversation started in the workspace begins with for this person: the template's
    tool scope narrowed by the profile. ``tool_scope`` is in the shape the chat API takes (known
    groups only; the internet is its own switch), ``denied`` lists what the profile took away and
    ``planned`` the groups the template names that do not exist yet."""
    agent = clean_agent(agent)
    scope = agent["tool_scope"]
    wanted: Iterable[str] = (
        scope["groups"] if scope["groups"] is not None else tool_groups.SELECTABLE
    )
    known = [g for g in wanted if g in tool_groups.SELECTABLE]
    usable = [g for g in known if access.tool_allowed(g)]
    denied = [g for g in known if not access.tool_allowed(g)]
    planned = [g for g in wanted if g in PLANNED_GROUPS]
    internet = scope["internet"]
    if not access.tool_allowed(tool_groups.WEB):
        internet = False
    memory = False if scope["memory"] is False else None
    out_scope: dict[str, Any] = {
        "nodes": scope["nodes"],
        "groups": None if scope["groups"] is None and not denied else usable,
        "internet": internet,
    }
    if memory is False:
        out_scope["memory"] = False
    unrestricted = out_scope["nodes"] is None and out_scope["groups"] is None
    tool_scope = None if unrestricted and internet is None and memory is None else out_scope
    return {
        "tool_scope": tool_scope,
        "persona": agent["persona"],
        "model_role": agent["model_role"],
        "frame": agent["frame"],
        "denied": denied,
        "planned": planned,
    }
