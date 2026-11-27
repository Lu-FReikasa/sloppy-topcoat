"""The committed OpenAPI document: the contract between the hub's routes and the web client.

``proto/api/openapi.json`` is FastAPI's schema of the app, written in a stable form. A hub test
fails when the app's schema differs from the committed file, and a web test checks that every path
the web client calls is in it. Regenerate after an API change::

    cd hub && uv run python -m topcoat_hub.openapi_contract

``--check`` only compares and exits 1 on a difference.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from fastapi import FastAPI
from fastapi.routing import APIWebSocketRoute, iter_route_contexts

from topcoat_hub.config import REPO_ROOT, Settings

CONTRACT = REPO_ROOT / "proto" / "api" / "openapi.json"
REGENERATE = "cd hub && uv run python -m topcoat_hub.openapi_contract"

# not part of OpenAPI, so listed beside it: the web client may open these as WebSockets
WEBSOCKETS_KEY = "x-topcoat-websockets"


def document(app: FastAPI) -> dict[str, Any]:
    """The app's OpenAPI document plus the paths of its WebSocket routes."""
    doc = dict(app.openapi())
    doc[WEBSOCKETS_KEY] = sorted(
        {
            c.path or c.original_route.path  # type: ignore[attr-defined]
            for c in iter_route_contexts(app.router.routes)
            if isinstance(c.original_route, APIWebSocketRoute)
        }
    )
    return doc


def render(app: FastAPI) -> str:
    """The document as the committed file holds it: sorted keys, so a diff shows only the change."""
    return json.dumps(document(app), indent=1, sort_keys=True, ensure_ascii=False) + "\n"


def build_app() -> FastAPI:
    """The app with default settings; its routes do not depend on the configuration."""
    from topcoat_hub.main import create_app

    return create_app(Settings(_env_file=None))  # type: ignore[call-arg]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--check", action="store_true", help="compare only, exit 1 on a difference")
    parser.add_argument("--out", type=Path, default=CONTRACT, help="file to write or compare")
    args = parser.parse_args(argv)
    text = render(build_app())
    current = args.out.read_text(encoding="utf-8") if args.out.exists() else None
    if args.check:
        if current is not None and current.replace("\r\n", "\n") == text:
            return 0
        print(f"{args.out} is out of date; regenerate it: {REGENERATE}", file=sys.stderr)
        return 1
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(text, encoding="utf-8", newline="\n")
    print(f"wrote {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
