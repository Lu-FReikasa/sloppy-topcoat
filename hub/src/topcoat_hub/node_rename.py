"""Renaming a node (2026-11-09 plan, problem 7).

A node is named once, when it is paired, and the name stands in many places. The person wants the
nodes named like their machines, so that the assistant and the person mean the same computer.

A rename does three things in one transaction:

1. ``nodes.name`` gets the new name and the old one becomes a row of ``node_aliases``.
2. The **living** settings that name the node are rewritten: open conversations (their pinned node,
   ``model@node`` and server choice), the hub settings (pools, model proofs), model ratings, the
   personal defaults and the scopes of programs and projects.
3. The **history** stays as it was written (messages, reports, tool calls, records): it says what
   was true then. An old name that still arrives (an old note, an API client, a model that read it
   somewhere) is resolved through the alias, see :func:`current_name`.

The session of a node does not depend on its name (the node is known by id and key), so a rename
does not disconnect it. A pairing may not take a name that is an alias of another node.
"""

from __future__ import annotations

import logging
import re
import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from topcoat_hub.models import (
    Conversation,
    HubSetting,
    ModelRating,
    Node,
    NodeAlias,
    NodeEvent,
    Project,
    UiTemplate,
    User,
    UserSettings,
)

log = logging.getLogger(__name__)

NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}$")

# old name -> current name, for the one hub process; loaded at the start and after every rename
_ALIASES: dict[str, str] = {}


class RenameError(ValueError):
    def __init__(self, status: int, code: str, message: str) -> None:
        super().__init__(message)
        self.status, self.code, self.message = status, code, message


def current_name(name: str) -> str:
    """The name a node has now, for a name it had before; any other name unchanged."""
    return _ALIASES.get(name, name)


def aliases() -> dict[str, str]:
    """Every earlier name and the node's current name (a copy)."""
    return dict(_ALIASES)


def earlier_names(name: str) -> list[str]:
    """The names a node had before it was called ``name``."""
    return sorted(old for old, now in _ALIASES.items() if now == name)


async def load_aliases(sessionmaker: async_sessionmaker[AsyncSession]) -> int:
    """Read the aliases into this process (at the start and after a rename)."""
    async with sessionmaker() as db:
        rows = (
            await db.execute(
                select(NodeAlias.name, Node.name).join(Node, Node.id == NodeAlias.node_id)
            )
        ).all()
    _ALIASES.clear()
    _ALIASES.update({old: now for old, now in rows})
    return len(_ALIASES)


# --- rewriting a value -----------------------------------------------------------------------


def rewrite_model(value: str, old: str, new: str) -> str:
    """``model@a,b`` with the node ``old`` replaced; a value without ``@`` is left alone."""
    model, sep, nodes = value.rpartition("@")
    if not sep:
        return value
    return f"{model}@{','.join(new if n == old else n for n in nodes.split(','))}"


def rewrite_json(value: Any, old: str, new: str) -> Any:
    """``value`` with every mention of the node replaced: a string that is the name, a key that is
    the name (pools are keyed by node), and the node part of a ``model@node`` string."""
    if isinstance(value, dict):
        return {(new if k == old else k): rewrite_json(v, old, new) for k, v in value.items()}
    if isinstance(value, list):
        return [rewrite_json(v, old, new) for v in value]
    if isinstance(value, str):
        if value == old:
            return new
        if "@" in value:
            return rewrite_model(value, old, new)
    return value


def _mentions(value: Any, old: str) -> bool:
    return rewrite_json(value, old, "\x00") != value


# --- the rename --------------------------------------------------------------------------------


async def rename(
    db: AsyncSession, node_id: uuid.UUID, new: str, actor: uuid.UUID | None
) -> dict[str, Any]:
    """Rename one node in the caller's transaction (it commits). Returns what was rewritten."""
    new = new.strip()
    if not NAME_RE.match(new):
        raise RenameError(
            422,
            "invalid_name",
            "a node name has small letters, digits and hyphens, starts with a letter or digit and "
            "is at most 63 characters long (a host name in small letters, e.g. office-pc)",
        )
    node = await db.get(Node, node_id, with_for_update=True)
    if node is None:
        raise RenameError(404, "not_found", "unknown node")
    old = node.name
    if new == old:
        raise RenameError(409, "same_name", f"the node is already called {old!r}")
    if await db.scalar(select(Node.id).where(Node.name == new)) is not None:
        raise RenameError(409, "name_taken", f"a node named {new!r} already exists")
    taken = await db.get(NodeAlias, new)
    if taken is not None and taken.node_id != node_id:
        raise RenameError(
            409, "name_taken", f"{new!r} was the name of another node and still leads to it"
        )
    if taken is not None:  # taking back an earlier name of this node
        await db.delete(taken)
    db.add(NodeAlias(name=old, node_id=node_id))
    node.name = new
    counts = await _rewrite_living(db, old, new)
    db.add(
        NodeEvent(
            node_id=node_id,
            kind="renamed",
            data={"from": old, "to": new, "by": str(actor) if actor else None, "rewritten": counts},
            ts=datetime.now(UTC),
        )
    )
    await db.commit()
    log.info("node %s renamed %s -> %s; rewritten %s", node_id, old, new, counts)
    return {"id": str(node_id), "from": old, "to": new, "rewritten": counts}


async def _rewrite_living(db: AsyncSession, old: str, new: str) -> dict[str, int]:
    counts: dict[str, int] = {}

    def count(what: str) -> None:
        counts[what] = counts.get(what, 0) + 1

    for conv in await db.scalars(select(Conversation)):
        changed = False
        if conv.node == old:
            conv.node, changed = new, True
        if conv.model and "@" in conv.model:
            model = rewrite_model(conv.model, old, new)
            if model != conv.model:
                conv.model, changed = model, True
        if conv.tool_scope and _mentions(conv.tool_scope, old):
            conv.tool_scope, changed = rewrite_json(conv.tool_scope, old, new), True
        if changed:
            count("conversations")
    for row in await db.scalars(select(HubSetting)):
        if _mentions(row.value, old):
            row.value = rewrite_json(row.value, old, new)
            count(f"hub_settings.{row.key}")
    for rating in await db.scalars(select(ModelRating).where(ModelRating.node == old)):
        rating.node = new
        count("model_ratings")
    for us in await db.scalars(select(UserSettings)):
        changed = False
        if us.default_model and "@" in us.default_model:
            model = rewrite_model(us.default_model, old, new)
            if model != us.default_model:
                us.default_model, changed = model, True
        if us.tool_scope and _mentions(us.tool_scope, old):
            us.tool_scope, changed = rewrite_json(us.tool_scope, old, new), True
        if changed:
            count("user_settings")
    for user in await db.scalars(select(User)):
        if user.ui_prefs and _mentions(user.ui_prefs, old):
            user.ui_prefs = rewrite_json(user.ui_prefs, old, new)
            count("users.ui_prefs")
    for tpl in await db.scalars(select(UiTemplate)):
        if tpl.agent and _mentions(tpl.agent, old):
            tpl.agent = rewrite_json(tpl.agent, old, new)
            count("ui_templates")
    for project in await db.scalars(select(Project)):
        if project.scope and _mentions(project.scope, old):
            project.scope = rewrite_json(project.scope, old, new)
            count("projects")
    return counts
