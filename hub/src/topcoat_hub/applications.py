"""The applications of the desktop, from one file (``proto/ui/applications.json``).

An application is a program with its own memory, tools and models (docs/PLATFORM.md). The hub used
to list their keys in several places (the memory tags, the hand-over sentence, the seeds); this
module is where they come from now. Which tools an application has and how its assistant talks are
still in ``ui/seeds.py`` (they are behaviour, not names)."""

import functools
import json
import os
from dataclasses import dataclass
from pathlib import Path

from topcoat_hub.config import REPO_ROOT

DEFAULT = "chat"


@dataclass(frozen=True)
class Application:
    key: str
    accent: str
    hue: int
    names: dict[str, str]

    def name(self, language: str) -> str:
        return self.names.get(language) or self.names.get("en") or self.key


def _path() -> Path:
    root = os.environ.get("TOPCOAT_PROTO_ROOT")
    return (Path(root) if root else REPO_ROOT / "proto") / "ui" / "applications.json"


@functools.cache
def load() -> tuple[Application, ...]:
    raw = json.loads(_path().read_text(encoding="utf-8"))
    out = tuple(
        Application(a["key"], a["accent"], int(a["hue"]), dict(a["name"]))
        for a in raw["applications"]
    )
    if not out or len({a.key for a in out}) != len(out):
        raise ValueError(f"{_path()}: applications must be a non-empty list of unique keys")
    return out


def keys() -> tuple[str, ...]:
    return tuple(a.key for a in load())


def get(key: str) -> Application | None:
    return next((a for a in load() if a.key == key), None)


def names(language: str) -> dict[str, str]:
    """``key -> how a sentence calls the application`` in ``language`` (English if unknown)."""
    return {a.key: a.name(language) for a in load()}
