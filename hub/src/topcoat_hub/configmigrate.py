"""Helpers for data migrations of node configs (ARCHITECTURE D14).

When a hub version adds default permissions, a migration calls :func:`add_default_actions`;
when it adds new config sections, :func:`add_config_defaults`. Existing nodes get a new
``config_hash`` (they receive the config at their next connect) and a ``config_changed``
event, so the change is visible in the GUI.
"""

import json
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import Connection, text

from topcoat_hub.protocol import config_hash


def _store(
    conn: Connection, node_id: Any, cfg: dict[str, Any], data: dict[str, Any], now: datetime
) -> None:
    conn.execute(
        text("UPDATE nodes SET config = CAST(:cfg AS jsonb), config_hash = :hash WHERE id = :id"),
        {"cfg": json.dumps(cfg), "hash": config_hash(cfg), "id": node_id},
    )
    conn.execute(
        text(
            "INSERT INTO node_events (node_id, kind, data, ts) "
            "VALUES (:id, 'config_changed', CAST(:data AS jsonb), :ts)"
        ),
        {"id": node_id, "data": json.dumps(data), "ts": now},
    )


def add_default_actions(
    conn: Connection, actions: list[str], source: str, only_with: str | None = None
) -> int:
    """Append missing ``actions`` to every node's ``allowed_actions``; return nodes changed.

    ``only_with``: leave nodes alone that do not allow this action (a tool node never runs
    ``models.*``, so a model action is only added where ``models.list`` is allowed)."""
    rows = conn.execute(text("SELECT id, config FROM nodes")).all()
    changed = 0
    now = datetime.now(UTC)
    for node_id, config in rows:
        cfg: dict[str, Any] = dict(config)
        allowed = list(cfg.get("allowed_actions", []))
        if only_with is not None and only_with not in allowed:
            continue
        added = [a for a in actions if a not in allowed]
        if not added:
            continue
        cfg["allowed_actions"] = allowed + added
        _store(conn, node_id, cfg, {"source": source, "added": added}, now)
        changed += 1
    return changed


def add_role_actions(conn: Connection, actions: list[str], source: str, role: str) -> int:
    """Append missing ``actions`` to the nodes of one role that take capability calls (a dev node
    for ``workshop.manage``); return nodes changed."""
    rows = conn.execute(text("SELECT id, config FROM nodes")).all()
    changed = 0
    now = datetime.now(UTC)
    for node_id, config in rows:
        cfg: dict[str, Any] = dict(config)
        allowed = list(cfg.get("allowed_actions", []))
        if role not in cfg.get("roles", []) or "capability.invoke" not in allowed:
            continue
        added = [a for a in actions if a not in allowed]
        if not added:
            continue
        cfg["allowed_actions"] = allowed + added
        _store(conn, node_id, cfg, {"source": source, "added": added}, now)
        changed += 1
    return changed


def add_config_defaults(
    conn: Connection,
    defaults: Callable[[dict[str, Any] | None], dict[str, Any]],
    source: str,
) -> int:
    """Add missing top-level config keys to every node; return nodes changed.

    ``defaults`` gets the node's latest inventory (or None) and returns the keys with their
    default values; only keys missing in a config are added.
    """
    rows = conn.execute(
        text(
            "SELECT n.id, n.config, i.inventory FROM nodes n "
            "LEFT JOIN node_inventory i ON i.node_id = n.id"
        )
    ).all()
    changed = 0
    now = datetime.now(UTC)
    for node_id, config, inventory in rows:
        cfg: dict[str, Any] = dict(config)
        added = {k: v for k, v in defaults(inventory).items() if k not in cfg}
        if not added:
            continue
        cfg.update(added)
        _store(conn, node_id, cfg, {"source": source, "added": sorted(added)}, now)
        changed += 1
    return changed


def add_default_capabilities(
    conn: Connection, capabilities: list[str], source: str, only_role: str | None = None
) -> int:
    """Switch new read-only ``capabilities`` on for every node that already takes capability calls
    (``capability.invoke`` allowed); return nodes changed. A node that does not offer one simply
    does not list it, so this only ever adds a name to a list. ``only_role``: only nodes with this
    role (a dev node for the workshop capabilities)."""
    rows = conn.execute(text("SELECT id, config FROM nodes")).all()
    changed = 0
    now = datetime.now(UTC)
    for node_id, config in rows:
        cfg: dict[str, Any] = dict(config)
        if "capability.invoke" not in cfg.get("allowed_actions", []):
            continue
        if only_role is not None and only_role not in cfg.get("roles", []):
            continue
        have = list(cfg.get("capabilities", []))
        added = [c for c in capabilities if c not in have]
        if not added:
            continue
        cfg["capabilities"] = have + added
        _store(conn, node_id, cfg, {"source": source, "added": added}, now)
        changed += 1
    return changed
