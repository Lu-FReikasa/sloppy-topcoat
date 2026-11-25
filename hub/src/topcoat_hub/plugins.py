"""Applications as plug-ins (D105), first step: the programs that are built in, described by the
manifest of ``proto/plugin/manifest.schema.json``, and the one list of commands every menu reads.

Nothing here changes how a program works: the manifests are derived from the program rows the hub
already has (``ui_templates``), and the commands from them. The registry (versions, install,
remove, live reload) is the next step; until then a program row is its own plug-in, version
``1.<rev>.0``.

The command list is what a person may do, filtered by what they may see: the web's command
palette, the menus a theme draws, the desktop client's selector and tray, and the Windows command
palette read this one list (D105).
"""

from __future__ import annotations

import json
import os
from functools import lru_cache
from pathlib import Path
from typing import Any

import jsonschema

from topcoat_hub.config import REPO_ROOT

CHAT_MODULE = "page.chat"


@lru_cache(maxsize=1)
def schema() -> dict[str, Any]:
    root = os.environ.get("TOPCOAT_PROTO_ROOT")
    path = (Path(root) if root else REPO_ROOT / "proto") / "plugin" / "manifest.schema.json"
    return json.loads(path.read_text(encoding="utf-8"))


def problems(manifest: Any) -> list[str]:
    """What is wrong with a manifest (empty when it is valid)."""
    validator = jsonschema.Draft202012Validator(schema())
    return [
        f"{'.'.join(str(p) for p in e.absolute_path) or '(root)'}: {e.message}"
        for e in sorted(validator.iter_errors(manifest), key=lambda e: list(e.absolute_path))
    ]


def _tools(agent: Any) -> list[str]:
    if not isinstance(agent, dict):
        return []
    for key in ("tools", "capabilities", "groups"):
        value = agent.get(key)
        if isinstance(value, list):
            return [str(v)[:80] for v in value if isinstance(v, str)][:200]
    return []


def manifest_of(program: dict[str, Any]) -> dict[str, Any]:
    """The manifest of one built-in program (a ``template_view`` of the UI API)."""
    key = str(program["key"])
    title_key = str(program.get("title_key") or f"workspace.{key}")
    modules = [str(m) for m in program.get("modules") or [] if isinstance(m, str)]
    role = program.get("requires_role") if program.get("requires_role") == "admin" else None
    commands: list[dict[str, Any]] = [
        {
            "id": f"open.{key}",
            "label_key": title_key,
            "action": {"kind": "open_program", "program": key},
            **({"requires_role": role} if role else {}),
        }
    ]
    if CHAT_MODULE in modules:
        commands.append(
            {
                "id": f"chat.{key}",
                "label_key": "commands.newChat",
                "action": {"kind": "new_chat", "program": key},
                **({"requires_role": role} if role else {}),
            }
        )
    manifest: dict[str, Any] = {
        "id": key,
        "version": f"1.{int(program.get('rev') or 0)}.0",
        "builtin": bool(program.get("builtin", True)),
        "title_key": title_key,
        "requires_role": role,
        "views": [
            {"id": m, "type": "chat" if m == CHAT_MODULE else "module"}
            for m in modules
            if isinstance(m, str) and m[:1].isalpha()
        ][:30],
        "commands": commands,
        "menus": {"tray": [f"open.{key}"], "window": [c["id"] for c in commands]},
    }
    if program.get("icon"):
        manifest["icon"] = str(program["icon"])[:40]
    tools = _tools(program.get("agent"))
    if tools:
        manifest["agent"] = {"tools_ceiling": tools}
    return manifest


def commands(manifests: list[dict[str, Any]], admin: bool) -> list[dict[str, Any]]:
    """Every command of the given plug-ins a person may run, with the plug-in it belongs to."""
    out: list[dict[str, Any]] = []
    for m in manifests:
        if m.get("requires_role") == "admin" and not admin:
            continue
        for c in m.get("commands") or []:
            if c.get("requires_role") == "admin" and not admin:
                continue
            out.append({**c, "plugin": m["id"]})
    return out
