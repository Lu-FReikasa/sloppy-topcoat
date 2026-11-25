"""A person's interface choices (docs/DESKTOP.md): the look and its hue, and the desktop, which
programs are open, minimised, focused or maximised and which view each shows.

Everything that comes in is clamped to a fixed shape, so the preferences are data and never code:
a stored or sent value that is not valid becomes the default of that field, never an error that
locks the person out of their own interface. Whether a program or view exists for the person is the
page's business (it shows only what the bootstrap offers); the hub keeps the shape."""

import re
from typing import Any

KEY = re.compile(r"^[a-z0-9][a-z0-9-]{0,39}$")
MODULE = re.compile(r"^[a-z]+\.[a-z0-9_.-]{1,60}$")
PROP_NAME = re.compile(r"^[a-z][a-z_]{0,30}$")
# the programs plus the document windows of the Ablage (`r-<id>`, `a-<id>`, web desktop/docs.ts)
MAX_PROGRAMS = 24
MAX_PROPS = 8
MAX_PROP_CHARS = 200

DEFAULT_HUE = 140
# the look is the key of a theme (docs/THEMES.md): a built-in one, or a custom theme of the hub. A
# key that does not exist (a theme that was deleted) draws the terminal, so only the shape is
# checked here.
LOOK = re.compile(r"^[a-z][a-z0-9-]{0,39}$")
DEFAULT_LOOK = "terminal"
DEFAULT_DESKTOP: dict[str, Any] = {
    "open": ["chat"],
    "minimised": [],
    "focus": "chat",
    "maximised": None,
    "views": {},
}
DEFAULT_PREFS: dict[str, Any] = {
    "hue": DEFAULT_HUE,
    "look": DEFAULT_LOOK,
    "desktop": DEFAULT_DESKTOP,
}


class PrefsError(ValueError):
    pass


def _hue(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return DEFAULT_HUE
    return max(0, min(359, round(value)))


def _keys(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    out: list[str] = []
    for k in value:
        if isinstance(k, str) and KEY.match(k) and k not in out:
            out.append(k)
    return out[:MAX_PROGRAMS]


def _props(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    out: dict[str, Any] = {}
    for name, v in value.items():
        if len(out) >= MAX_PROPS or not isinstance(name, str) or not PROP_NAME.match(name):
            continue
        if isinstance(v, str):
            out[name] = v[:MAX_PROP_CHARS]
        elif v is None or isinstance(v, bool | int | float):
            out[name] = v
    return out


def sanitize_desktop(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, dict):
        return {**DEFAULT_DESKTOP, "open": list(DEFAULT_DESKTOP["open"]), "views": {}}
    opened = _keys(raw.get("open"))
    minimised = [k for k in _keys(raw.get("minimised")) if k in opened]
    focus = raw.get("focus") if raw.get("focus") in opened else None
    maximised = raw.get("maximised") if raw.get("maximised") in opened else None
    views: dict[str, Any] = {}
    raw_views = raw.get("views") if isinstance(raw.get("views"), dict) else {}
    for key, view in raw_views.items():
        if len(views) >= MAX_PROGRAMS or not isinstance(key, str) or not KEY.match(key):
            continue
        module = view.get("module") if isinstance(view, dict) else None
        if isinstance(module, str) and MODULE.match(module):
            views[key] = {"module": module, "props": _props(view.get("props"))}
    return {
        "open": opened,
        "minimised": minimised,
        "focus": focus,
        "maximised": maximised,
        "views": views,
    }


def effective(stored: Any) -> dict[str, Any]:
    """The prefs of a person as stored, with every field valid (fields of older versions, such as a
    shell or a theme, are dropped)."""
    stored = stored if isinstance(stored, dict) else {}
    theme = stored.get("theme") if isinstance(stored.get("theme"), dict) else {}
    return {
        # the hue was part of a theme before the desktop (D70): it is kept
        "hue": _hue(stored["hue"] if "hue" in stored else theme.get("hue", DEFAULT_HUE)),
        "look": look
        if isinstance(look := stored.get("look"), str) and LOOK.match(look)
        else DEFAULT_LOOK,
        "desktop": sanitize_desktop(stored.get("desktop")),
    }


def apply_patch(stored: Any, patch: dict[str, Any]) -> dict[str, Any]:
    """The prefs after a change. ``hue`` must be a number from 0 to 359; ``desktop`` replaces the
    desktop as a whole (it is one picture, not a set of fields), clamped."""
    current = effective(stored)
    if "hue" in patch:
        value = patch["hue"]
        if isinstance(value, bool) or not isinstance(value, int | float) or not 0 <= value <= 359:
            raise PrefsError("hue is a number from 0 to 359")
        current["hue"] = round(value)
    if "look" in patch:
        if not isinstance(patch["look"], str) or not LOOK.match(patch["look"]):
            raise PrefsError("look is the key of a theme: lower case letters, digits and '-'")
        current["look"] = patch["look"]
    if "desktop" in patch:
        if not isinstance(patch["desktop"], dict):
            raise PrefsError("desktop must be an object")
        current["desktop"] = sanitize_desktop(patch["desktop"])
    return current
