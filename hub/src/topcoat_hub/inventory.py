"""The inventory: what runs on which node (E3-10, D45).

Every connected node that offers ``inventory.snapshot`` is read on a timer (and on demand). The
answer has one section per source (host, containers, services) and the hub keeps, per node, when
it looked, which section worked and why one did not, plus one row per container and service.

The rules that make it something to rely on:

* A section that **failed** changes nothing. An item is marked gone only when a look that
  succeeded for that section no longer listed it (and was not cut short); a Docker daemon that did
  not answer never turns into "no containers".
* Every answer says how old it is. A look older than three intervals is ``stale``, a node that is
  not connected is ``offline``: the data is then shown as *last known*, never as current.
* Read-only from end to end: the only thing the hub asks a node is ``inventory.snapshot``, through
  the same checks as any capability (the node's config, read-only, schema) — no grant is needed for
  the hub's own periodic read, but a *person* sees a node's inventory only with the right to run
  that capability on it (D20: an admin on the GUI, anybody else by grant, never an API key by
  inheritance).
"""

import asyncio
import logging
import time
import uuid
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from topcoat_hub.config import Settings
from topcoat_hub.live import NodeSessions
from topcoat_hub.models import (
    SYSTEM_ID,
    CapabilityGrant,
    InventoryItem,
    InventoryNode,
    Node,
)
from topcoat_hub.tools import ALL, Caller, ToolError, ToolService, enabled

log = logging.getLogger(__name__)

CAPABILITY = "inventory.snapshot"
# the chat's tool: it reads the hub's stored inventory, it asks no node
LIST_CAPABILITY = "inventory.list"
INVENTORY_NODE = "inventory"
INVENTORY_NODE_ID = uuid.UUID("00000000-0000-4000-8000-00000000a0d1")

SECTIONS = (("containers", "container"), ("services", "service"))
KINDS = ("container", "service", "compose")
MAX_NAME = 200
REFRESH_COOLDOWN_SECONDS = 5.0
TICK_SECONDS = 30.0
RETRY_AFTER_FAILURE_SECONDS = 60.0

LIST_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "query": {
            "type": "string",
            "maxLength": 100,
            "description": "part of a container, service, image or compose project name",
        },
        "kind": {"type": "string", "enum": list(KINDS)},
        "node": {"type": "string", "maxLength": 63, "description": "only this node (its name)"},
        "state": {
            "type": "string",
            "maxLength": 32,
            "description": "e.g. running, exited, active, failed",
        },
        "include_gone": {
            "type": "boolean",
            "description": "also what was seen before and has disappeared (default false)",
        },
        "limit": {"type": "integer", "minimum": 1, "maximum": 100, "description": "default 50"},
    },
}
LIST_DESCRIPTION = (
    "The hub's inventory of what runs on the user's machines: Docker containers, compose projects "
    "and systemd services per node, with state, since when the hub has seen that state, and how "
    "old the last look at each node is. It is a stored look, not a live one: every answer says "
    "which nodes are offline or stale and which sources could not be read — say so, and do not "
    "treat 'not listed' as 'not running' for a node whose look failed or is old. Use it first to "
    "find where something runs; use the live tools to confirm or to look closer."
)


def utcnow() -> datetime:
    return datetime.now(UTC)


def list_tool() -> dict[str, Any]:
    return {
        "node_id": str(INVENTORY_NODE_ID),
        "node": INVENTORY_NODE,
        "capability": LIST_CAPABILITY,
        "description": LIST_DESCRIPTION,
        "input_schema": LIST_SCHEMA,
    }


def _text(value: Any, limit: int) -> str | None:
    return value[:limit] if isinstance(value, str) and value else None


def normalize(kind: str, row: Any) -> tuple[str, str, dict[str, Any]] | None:
    """One row of a node's answer as ``(name, state, detail)``, or None when it is no usable row
    (a node of another version, a bug: a bad row is dropped, it never fails the look)."""
    if not isinstance(row, dict):
        return None
    if kind == "container":
        name, state = _text(row.get("name"), MAX_NAME), _text(row.get("state"), 32)
        keys = ("image", "health", "started_at", "compose_project", "compose_service")
    else:
        name, state = _text(row.get("unit"), MAX_NAME), _text(row.get("active"), 32)
        keys = ("sub", "enabled", "description")
    if name is None or state is None:
        return None
    detail: dict[str, Any] = {k: v for k in keys if (v := _text(row.get(k), 300))}
    if kind == "container":
        restarts = row.get("restart_count")
        if isinstance(restarts, int) and not isinstance(restarts, bool) and restarts > 0:
            detail["restart_count"] = restarts
        ports = row.get("ports")
        if isinstance(ports, list):
            detail["ports"] = [p[:40] for p in ports if isinstance(p, str)][:20]
    return name, state, detail


class InventoryService:
    def __init__(
        self,
        sessionmaker: async_sessionmaker[AsyncSession],
        tools: ToolService,
        sessions: NodeSessions,
        settings: Settings,
        publish: Callable[[str, dict[str, Any]], None] | None = None,
    ) -> None:
        self.sm = sessionmaker
        self.tools = tools
        self.sessions = sessions
        self.settings = settings
        self.publish = publish
        self._locks: dict[uuid.UUID, asyncio.Lock] = {}

    # --- collecting ---------------------------------------------------------------------

    def _interval(self) -> float:
        return self.settings.inventory_interval_seconds

    def stale_after(self) -> float:
        """How old a look may be before it is shown as stale."""
        interval = self._interval()
        return max(3 * interval, 300.0) if interval > 0 else 900.0

    async def collect(self, node_id: uuid.UUID) -> InventoryNode | None:
        """Read one node now. One read per node at a time; a second request inside the cooldown
        gets the stored result."""
        lock = self._locks.setdefault(node_id, asyncio.Lock())
        async with lock:
            async with self.sm() as db:
                row = await db.get(InventoryNode, node_id)
                if (
                    row is not None
                    and row.attempted_at is not None
                    and (utcnow() - row.attempted_at).total_seconds() < REFRESH_COOLDOWN_SECONDS
                ):
                    return row
            return await self._collect(node_id)

    async def _stored(self, node_id: uuid.UUID) -> InventoryNode | None:
        async with self.sm() as db:
            return await db.get(InventoryNode, node_id)

    async def _collect(self, node_id: uuid.UUID) -> InventoryNode | None:
        started = time.monotonic()
        caller = Caller(SYSTEM_ID, True, "inventory")
        outcome: dict[str, Any] | None = None
        failure: tuple[str, str] | None = None  # (status, message) when nothing was read
        try:
            result = await self.tools.invoke(caller, node_id, CAPABILITY, {}, quiet=True)
            if result.get("ok"):
                outcome = result.get("output")
            else:
                failure = ("error", str((result.get("error") or {}).get("message") or "failed"))
        except ToolError as exc:
            if exc.code in ("not_found", "node_offline"):
                return await self._stored(node_id)  # nobody to ask: what is stored stays as it is
            asked = exc.code in ("timeout", "upgrade_required")
            failure = ("error" if asked else "unavailable", exc.message)
        took = int((time.monotonic() - started) * 1000)
        now = utcnow()
        async with self.sm() as db:
            node = await db.get(Node, node_id)
            if node is None:
                return None
            row = await db.get(InventoryNode, node_id)
            if row is None:
                row = InventoryNode(node_id=node_id, sections={})
                db.add(row)
            row.attempted_at, row.duration_ms = now, took
            if failure is not None or not isinstance(outcome, dict):
                status, message = failure or ("error", "the node's answer was not understood")
                row.status, row.error = status, message[:500]
                # the sections keep what they had; each says its last good read
                if status == "unavailable":
                    row.sections = {}
            else:
                await self._reconcile(db, row, node_id, outcome, now)
            await db.commit()
            await db.refresh(row)
        if self.publish is not None:
            self.publish("inventory_updated", {"node_id": str(node_id), "status": row.status})
        return row

    async def _reconcile(
        self,
        db: AsyncSession,
        row: InventoryNode,
        node_id: uuid.UUID,
        out: dict[str, Any],
        now: datetime,
    ) -> None:
        """Apply one answer: the sections that worked update their items, the others change none."""
        sections: dict[str, Any] = {}
        old: dict[str, Any] = row.sections or {}
        any_ok = failed = 0
        host = out.get("host")
        for name in ("host", *(s for s, _ in SECTIONS)):
            sec = out.get(name)
            prev = old.get(name) or {}
            entry: dict[str, Any] = {"read_at": prev.get("read_at")}
            if not isinstance(sec, dict):
                entry |= {"state": "failed", "error": "the node did not send this section"}
                failed += 1
            elif sec.get("ok") is True:
                entry |= {"state": "ok", "read_at": now.isoformat()}
                if sec.get("truncated"):
                    entry["truncated"] = True
                if isinstance(sec.get("total"), int):
                    entry["total"] = sec["total"]
                any_ok += 1
            elif sec.get("code") == "not_configured":
                # this node has no such source (no Docker here): not a fault, nothing to list
                entry |= {"state": "n/a", "error": str(sec.get("error") or "")[:300]}
            else:
                entry |= {
                    "state": "failed",
                    "code": str(sec.get("code") or "error")[:40],
                    "error": str(sec.get("error") or "could not be read")[:300],
                }
                failed += 1
            sections[name] = entry
        if isinstance(host, dict) and host.get("ok") is True and isinstance(host.get("data"), dict):
            row.host = host["data"]
        for name, kind in SECTIONS:
            sec = out.get(name)
            if not isinstance(sec, dict):
                continue
            if sections[name]["state"] == "ok" and isinstance(sec.get("data"), list):
                await self._apply(db, node_id, kind, sec["data"], now, bool(sec.get("truncated")))
            elif sections[name]["state"] == "n/a":
                await self._apply(db, node_id, kind, [], now, False)  # nothing runs from a source
        row.sections = sections
        row.error = None
        if any_ok:
            row.collected_at = now
        row.status = "ok" if failed == 0 else ("partial" if any_ok else "error")
        await db.execute(
            delete(InventoryItem).where(
                InventoryItem.node_id == node_id,
                InventoryItem.gone_at < now - timedelta(days=self.settings.inventory_gone_days),
            )
        )

    async def _apply(
        self,
        db: AsyncSession,
        node_id: uuid.UUID,
        kind: str,
        rows: list[Any],
        now: datetime,
        truncated: bool,
    ) -> None:
        existing = {
            i.name: i
            for i in await db.scalars(
                select(InventoryItem).where(
                    InventoryItem.node_id == node_id, InventoryItem.kind == kind
                )
            )
        }
        seen: set[str] = set()
        for raw in rows:
            norm = normalize(kind, raw)
            if norm is None or norm[0] in seen:
                continue
            name, state, detail = norm
            seen.add(name)
            item = existing.get(name)
            if item is None:
                db.add(
                    InventoryItem(
                        node_id=node_id,
                        kind=kind,
                        name=name,
                        state=state,
                        state_since=now,
                        detail=detail,
                        first_seen=now,
                        last_seen=now,
                    )
                )
                continue
            if item.state != state or item.gone_at is not None:
                item.state_since = now
            item.state, item.detail, item.last_seen, item.gone_at = state, detail, now, None
        if truncated:
            return  # the list was cut: what is missing from it is not known to be gone
        for name, item in existing.items():
            if name not in seen and item.gone_at is None:
                item.gone_at = now

    def due(self, row: InventoryNode | None, now: datetime) -> bool:
        interval = self._interval()
        if interval <= 0:
            return False
        if row is None or row.attempted_at is None:
            return True
        wait = (
            interval
            if row.status in ("ok", "partial")
            else min(interval, RETRY_AFTER_FAILURE_SECONDS)
        )
        if row.status == "unavailable":
            wait = interval  # a node that does not offer it is asked again slowly (config changes)
        return (now - row.attempted_at).total_seconds() >= wait

    async def collect_due(self) -> int:
        """Read every connected node that is due. Returns how many were read."""
        async with self.sm() as db:
            nodes = {n.id: n for n in (await db.scalars(select(Node))).all()}
            rows = {r.node_id: r for r in await db.scalars(select(InventoryNode))}
        now, count = utcnow(), 0
        # A node whose config does not switch the capability on is not asked at all: a call
        # would only be refused, and every refusal is an audit row (one per node per interval).
        # A manual refresh still asks, so the refusal says why.
        todo = [
            n
            for n, node in nodes.items()
            if self.sessions.get(n) is not None
            and CAPABILITY in enabled(node.config or {})
            and self.due(rows.get(n), now)
        ]

        async def one(node_id: uuid.UUID) -> None:
            try:
                await self.collect(node_id)
            except Exception:
                log.exception("inventory: reading node %s failed", node_id)

        await asyncio.gather(*(one(n) for n in todo))
        count = len(todo)
        return count

    async def scheduler(self) -> None:
        interval = self._interval()
        if interval <= 0:
            return
        while True:
            try:
                await self.collect_due()
            except Exception:
                log.exception("inventory: the periodic read failed")
            await asyncio.sleep(min(TICK_SECONDS, interval))

    # --- who may look ------------------------------------------------------------------

    async def visible(self, caller: Caller) -> list[Node]:
        """The nodes whose inventory this caller may see: an admin on the GUI all of them, anybody
        else those they hold ``inventory.snapshot`` (or ``*``) on."""
        async with self.sm() as db:
            nodes = list((await db.scalars(select(Node).order_by(Node.name))).all())
            if caller.admin:
                return nodes
            granted = {
                n
                for n, cap in (
                    await db.execute(
                        select(CapabilityGrant.node_id, CapabilityGrant.capability).where(
                            CapabilityGrant.user_id == caller.user_id
                        )
                    )
                ).all()
                if cap in (ALL, CAPABILITY)
            }
        return [n for n in nodes if n.id in granted]

    # --- reading -----------------------------------------------------------------------

    async def _states(
        self, nodes: list[Node]
    ) -> tuple[dict[uuid.UUID, InventoryNode], dict[uuid.UUID, list[InventoryItem]]]:
        ids = [n.id for n in nodes]
        async with self.sm() as db:
            rows = {
                r.node_id: r
                for r in await db.scalars(
                    select(InventoryNode).where(InventoryNode.node_id.in_(ids))
                )
            }
            items: dict[uuid.UUID, list[InventoryItem]] = {}
            for i in await db.scalars(
                select(InventoryItem)
                .where(InventoryItem.node_id.in_(ids))
                .order_by(InventoryItem.kind, InventoryItem.name)
            ):
                items.setdefault(i.node_id, []).append(i)
        return rows, items

    def _freshness(self, row: InventoryNode | None, online: bool, now: datetime) -> dict[str, Any]:
        age = (
            None
            if row is None or row.collected_at is None
            else int((now - row.collected_at).total_seconds())
        )
        sections = (row.sections if row else None) or {}
        stale = age is None or age > self.stale_after()
        out_sections: dict[str, Any] = {}
        for name, sec in sections.items():
            read_at = sec.get("read_at")
            sec_age = None
            if read_at:
                sec_age = int((now - datetime.fromisoformat(read_at)).total_seconds())
            out_sections[name] = {
                "state": sec.get("state", "failed"),
                "error": sec.get("error"),
                "read_at": read_at,
                "age_seconds": sec_age,
                "truncated": bool(sec.get("truncated")),
                # a section is current when its own last look worked and is not old
                "current": sec.get("state") in ("ok", "n/a")
                and (
                    sec.get("state") == "n/a"
                    or (sec_age is not None and sec_age <= self.stale_after())
                ),
            }
        return {
            "online": online,
            "status": row.status if row else "never",
            "error": row.error if row else None,
            "attempted_at": row.attempted_at.isoformat() if row and row.attempted_at else None,
            "collected_at": row.collected_at.isoformat() if row and row.collected_at else None,
            "age_seconds": age,
            "stale": stale,
            "current": online and not stale and bool(row) and row.status == "ok",
            "sections": out_sections,
        }

    @staticmethod
    def _item(i: InventoryItem) -> dict[str, Any]:
        return {
            "kind": i.kind,
            "name": i.name,
            "state": i.state,
            "since": i.state_since.isoformat(),
            "first_seen": i.first_seen.isoformat(),
            "last_seen": i.last_seen.isoformat(),
            "gone": i.gone_at is not None,
            "gone_at": i.gone_at.isoformat() if i.gone_at else None,
            "detail": i.detail,
        }

    @staticmethod
    def _compose(items: list[InventoryItem]) -> list[dict[str, Any]]:
        """Compose projects, derived from the containers of the same look (no second source)."""
        by: dict[str, list[InventoryItem]] = {}
        for i in items:
            project = (i.detail or {}).get("compose_project")
            if i.kind == "container" and project:
                by.setdefault(project, []).append(i)
        out = []
        for project, members in sorted(by.items()):
            live = [m for m in members if m.gone_at is None]
            running = [m for m in live if m.state == "running"]
            if not live:
                state = "gone"
            elif len(running) == len(live):
                state = "running"
            else:
                state = "partial" if running else "stopped"
            out.append(
                {
                    "kind": "compose",
                    "name": project,
                    "state": state,
                    "services": len(live),
                    "running": len(running),
                    "containers": [m.name for m in live],
                }
            )
        return out

    async def view(self, caller: Caller, node_id: uuid.UUID | None = None) -> list[dict[str, Any]]:
        """The inventory of the nodes this caller may see (or of one of them)."""
        nodes = await self.visible(caller)
        if node_id is not None:
            nodes = [n for n in nodes if n.id == node_id]
        rows, items = await self._states(nodes)
        now = utcnow()
        out: list[dict[str, Any]] = []
        for n in nodes:
            row, its = rows.get(n.id), items.get(n.id, [])
            fresh = self._freshness(row, self.sessions.get(n.id) is not None, now)
            out.append(
                {
                    "node": {"id": str(n.id), "name": n.name},
                    **fresh,
                    "host": row.host if row else None,
                    "containers": [self._item(i) for i in its if i.kind == "container"],
                    "services": [self._item(i) for i in its if i.kind == "service"],
                    "compose": self._compose(its),
                }
            )
        return out

    # --- the chat's tool ----------------------------------------------------------------

    async def offer(self, caller: Caller) -> list[dict[str, Any]]:
        """The virtual tool, if this caller may see the inventory of at least one node."""
        return [list_tool()] if await self.visible(caller) else []

    async def run_tool(self, caller: Caller, tool_input: dict[str, Any]) -> dict[str, Any]:
        unknown = set(tool_input) - set(LIST_SCHEMA["properties"])
        if unknown:
            raise ToolError(422, "invalid_input", f"unknown input: {', '.join(sorted(unknown))}")
        query = tool_input.get("query")
        kind, node, state = tool_input.get("kind"), tool_input.get("node"), tool_input.get("state")
        limit = tool_input.get("limit", 50)
        include_gone = tool_input.get("include_gone", False)
        if kind is not None and kind not in KINDS:
            raise ToolError(422, "invalid_input", f"kind must be one of {', '.join(KINDS)}")
        if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 100:
            raise ToolError(422, "invalid_input", "limit must be 1..100")
        for key, val in (("query", query), ("node", node), ("state", state)):
            if val is not None and (not isinstance(val, str) or len(val) > 100):
                raise ToolError(422, "invalid_input", f"{key} must be a short text")
        views = await self.view(caller)
        needle = (query or "").strip().lower()
        items: list[dict[str, Any]] = []
        nodes: list[dict[str, Any]] = []
        notes: list[str] = []
        for v in views:
            name = v["node"]["name"]
            if node and name.lower() != node.strip().lower():
                continue
            nodes.append(_node_line(v))
            notes.extend(_caveats(name, v))
            for row in (*v["containers"], *v["services"], *v["compose"]):
                if kind and row["kind"] != kind:
                    continue
                if row.get("gone") and not include_gone:
                    continue
                if state and row["state"].lower() != state.strip().lower():
                    continue
                if needle and needle not in _haystack(row):
                    continue
                items.append({"node": name, **_brief(row)})
        total = len(items)
        out: dict[str, Any] = {
            "nodes": nodes,
            "items": items[:limit],
            "count": total,
            "truncated": total > limit,
        }
        if notes:
            out["caveats"] = notes
        if not nodes:
            out["caveats"] = [*notes, "no node matches, or you may not see its inventory"]
        return {
            "ok": True,
            "output": out,
            "at": utcnow().isoformat(),
            "bytes": len(str(out)),
        }


def _haystack(row: dict[str, Any]) -> str:
    detail = row.get("detail") or {}
    parts = [row["name"], str(detail.get("image", "")), str(detail.get("compose_project", ""))]
    parts += [str(detail.get("description", ""))] + [str(c) for c in row.get("containers", [])]
    return " ".join(parts).lower()


def _brief(row: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {"kind": row["kind"], "name": row["name"], "state": row["state"]}
    if row.get("since"):
        out["state_seen_since"] = row["since"]
    if row.get("gone"):
        out["gone_at"] = row["gone_at"]
    detail = row.get("detail") or {}
    for key in ("image", "health", "compose_project", "sub", "enabled", "description", "ports"):
        if detail.get(key):
            out[key] = detail[key]
    if row["kind"] == "compose":
        out["services"], out["running"] = row["services"], row["running"]
    return out


def _node_line(v: dict[str, Any]) -> dict[str, Any]:
    host = v.get("host") or {}
    line: dict[str, Any] = {
        "node": v["node"]["name"],
        "online": v["online"],
        "last_look": v["collected_at"],
        "age_seconds": v["age_seconds"],
        "current": v["current"],
        "status": v["status"],
    }
    for key in ("os", "kernel"):
        if host.get(key):
            line[key] = host[key]
    return line


def _caveats(name: str, v: dict[str, Any]) -> list[str]:
    """What the reader must not over-read: a stored look is only as good as its age and sources."""
    out: list[str] = []
    if v["status"] == "never" or v["collected_at"] is None:
        out.append(f"{name}: the hub has not read this node yet, nothing is known")
        return out
    if not v["online"]:
        out.append(f"{name}: offline, this is the last known state ({_age(v['age_seconds'])} old)")
    elif v["stale"]:
        out.append(f"{name}: the last look is {_age(v['age_seconds'])} old, it may have changed")
    for sname, sec in v["sections"].items():
        if sec["state"] == "failed":
            out.append(f"{name}: {sname} could not be read ({sec.get('error') or 'error'})")
        elif sec.get("truncated"):
            out.append(f"{name}: the {sname} list was cut, more may exist")
    if v["status"] == "unavailable":
        out.append(f"{name}: this node does not offer the inventory ({v['error']})")
    return out


def _age(seconds: int | None) -> str:
    if seconds is None:
        return "unknown age"
    if seconds < 90:
        return f"{seconds} s"
    if seconds < 5400:
        return f"{round(seconds / 60)} min"
    return f"{round(seconds / 3600, 1)} h"
