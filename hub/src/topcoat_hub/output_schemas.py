"""The JSON forms a model's answer must have, enforced by the server (plan step 1.12, B4).

The hub asked for JSON in the prompt and repaired what came back (``json_repair``, a tolerant
reader); "the answer holds no JSON" was a common refusal of plans and verdicts. llama-server
turns a JSON schema in ``response_format`` into a grammar, so the answer cannot leave the form.
The schemas live in ``proto/agents/outputs/<name>.schema.json``; a block names one with its field
``schema`` (blocks/program.py), the ``check`` block uses ``check``. The tolerant reader stays as
the net for models that refuse the field (blocks/service.py remembers them).
"""

from __future__ import annotations

import json
import os
from functools import cache
from pathlib import Path
from typing import Any

from topcoat_hub.config import REPO_ROOT

NAMES = ("plan_v2", "verdict", "check", "investigate", "area")
# the image sets TOPCOAT_PROTO_ROOT; main.py sets the configured root again
_PROTO: list[Path] = [Path(os.environ.get("TOPCOAT_PROTO_ROOT") or REPO_ROOT / "proto")]


def set_proto_root(root: Path) -> None:
    _PROTO[0] = Path(root)
    get.cache_clear()


def _root() -> Path:
    return _PROTO[0] / "agents" / "outputs"


@cache
def get(name: str) -> dict[str, Any] | None:
    """The schema ``name`` without its ``$``-keys (a grammar does not need them), or ``None`` when
    there is no such schema or its file is not there (the step then asks as before)."""
    if name not in NAMES:
        return None
    path = _root() / f"{name}.schema.json"
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return {k: v for k, v in doc.items() if not k.startswith("$") and k != "title"}


def response_format(name: str) -> dict[str, Any] | None:
    """The ``response_format`` of an OpenAI-style request that enforces ``name``."""
    schema = get(name)
    if schema is None:
        return None
    return {"type": "json_schema", "json_schema": {"name": name, "schema": schema}}
