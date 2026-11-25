"""Pictures of a theme (2026-11-15): after ``theme_design`` wrote a theme that passes the hub's
check, the dev node's workshop browser (E6-02) draws the window desktop in it, once wide and once
narrow, and the pictures go into the task's result. The theme is data in a container without
network; the node checks its size and key, the hub checked the rest before.

Nothing here decides anything: a node without the browser, a busy browser or a failed shot leaves
the result without pictures and says why in a note."""

from __future__ import annotations

import base64
import binascii
import json
import logging
import uuid
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import select

from topcoat_hub.models import ToolCall
from topcoat_hub.tools import Caller, ToolError

log = logging.getLogger(__name__)

OPEN, SHOT, VIEWPORT, CLOSE = "browser.open", "browser.shot", "browser.viewport", "browser.close"
# what is drawn: the desktop as on a monitor, then as on a phone (the same theme, a narrow stage)
VIEWS: tuple[tuple[str, dict[str, Any] | None, float], ...] = (
    ("Desktop, 1280 x 800", None, 0.6),
    ("Schmal, 390 x 844", {"width": 390, "height": 844}, 0.8),
)


@dataclass
class Preview:
    pictures: list[tuple[str, bytes]] = field(default_factory=list)  # caption, JPEG
    note: str = ""


def _output(res: dict[str, Any]) -> dict[str, Any]:
    out = res.get("output")
    if isinstance(out, str | bytes):
        try:
            out = json.loads(out)
        except ValueError:
            out = {}
    return out if isinstance(out, dict) else {}


def _error(res: dict[str, Any]) -> str:
    err = res.get("error") or {}
    return str(err.get("message") if isinstance(err, dict) else err) or "failed"


async def _call(
    tools: Any, caller: Caller, node_id: uuid.UUID, capability: str, tool_input: dict[str, Any]
) -> dict[str, Any]:
    """One call; a refusal (ToolError) is an answer that is not ok, like a failed run."""
    try:
        return await tools.invoke(caller, node_id, capability, tool_input)
    except ToolError as exc:
        return {"ok": False, "error": {"message": exc.message}}


def browser_node(usable: list[dict[str, Any]]) -> str | None:
    """The node whose browser this caller may drive: it offers both open and shot."""
    by_node: dict[str, set[str]] = {}
    for t in usable:
        by_node.setdefault(str(t["node_id"]), set()).add(str(t["capability"]))
    for node_id, caps in by_node.items():
        if {OPEN, SHOT, CLOSE} <= caps:
            return node_id
    return None


async def last_repo(sm: Any, user_id: uuid.UUID, node_id: uuid.UUID) -> str | None:
    """The workshop repository this person last worked with on the node (the browser serves a
    copy of a worktree; which one does not matter for a theme, the image brings its own GUI)."""
    async with sm() as db:
        rows = await db.scalars(
            select(ToolCall.input)
            .where(
                ToolCall.user_id == user_id,
                ToolCall.node_id == node_id,
                ToolCall.ok.is_(True),
                ToolCall.capability.like("repo.%"),
            )
            .order_by(ToolCall.id.desc())
            .limit(20)
        )
        for row in rows:
            repo = row.get("repo") if isinstance(row, dict) else None
            if isinstance(repo, str) and repo:
                return repo
    return None


async def take(tools: Any, sm: Any, caller: Caller, theme: dict[str, Any]) -> Preview:
    """Open the browser with the theme, take the pictures, close it again (also on failure)."""
    node = browser_node(await tools.usable_tools(caller))
    if node is None:
        return Preview(note="no node with the workshop browser for this person: no pictures")
    node_id = uuid.UUID(node)
    repo = await last_repo(sm, caller.user_id, node_id)
    if repo is None:
        return Preview(note="no workshop repository known on the browser's node: no pictures")
    opened = await _call(tools, caller, node_id, OPEN, {"repo": repo, "theme": theme})
    if not opened.get("ok", True):
        return Preview(note=f"the browser did not open: {_error(opened)}")
    out = Preview()
    try:
        for caption, viewport, scale in VIEWS:
            if viewport is not None:
                res = await _call(tools, caller, node_id, VIEWPORT, viewport)
                if not res.get("ok", True):
                    out.note = f"{caption}: {_error(res)}"
                    continue
            res = await _call(tools, caller, node_id, SHOT, {"scale": scale})
            if not res.get("ok", True):
                out.note = f"{caption}: {_error(res)}"
                continue
            try:
                data = base64.b64decode(str(_output(res).get("base64") or ""), validate=True)
            except (binascii.Error, ValueError):
                data = b""
            if data[:3] == b"\xff\xd8\xff":  # a JPEG, nothing else is kept
                out.pictures.append((caption, data))
            else:
                out.note = f"{caption}: the node sent no picture"
    finally:
        closed = await _call(tools, caller, node_id, CLOSE, {})
        if not closed.get("ok", True):  # the idle limit ends it anyway
            log.warning("browser.close after the theme pictures: %s", _error(closed))
    return out
