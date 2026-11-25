"""A change to a theme as a patch (the theme workshop, 2026-11-17).

In the workshop the person talks a theme into shape: "darker", "the title bar thinner", "let the
windows open with a zoom". A model that writes the whole theme for every turn needs 650-1100
tokens (30-40 s on the house model) and, live, forgets what earlier turns settled. A patch names
only what changes, so a turn costs 100-300 tokens and everything else stays by construction:

    {"reply": "…",                       one or two sentences to the person
     "set": {"name": …, "base": …, "layout": {…}, "bar": {…}, "chrome": {…}, "motion": {…}},
     "vars": {"--bg": "#05070a", "--old": null},             null removes
     "rules": [{"selector": ".dt-titlebar", "css": "height: 22px;"},
               {"selector": ".dt-front", "css": null}],   css replaces the block, null removes
     "keyframes": {"wire-in": "from { opacity: 0; } to { opacity: 1; }", "old": null},
     "css": "…"}                         the whole sheet, only when everything changes

The key never changes (a stored theme of that key is the one replaced). What is not understood is
left out and named; the caller checks the result (theme_problems, theme_lint) like any theme."""

import json
import re
from typing import Any

from topcoat_hub.ui import theme_css
from topcoat_hub.ui.themes import MAX_CSS

_FIELDS = ("name", "base", "layout", "bar", "chrome", "motion")
_OBJECTS = ("layout", "bar", "chrome", "motion")  # merged key by key
_VAR = re.compile(r"^--[a-zA-Z0-9_-]{1,60}$")
_KEYFRAMES = re.compile(r"^[a-zA-Z_][a-zA-Z0-9_-]{0,60}$")


def parse_patch(text: str) -> dict[str, Any] | None:
    """The patch in a model's answer: the first JSON object of it (a code fence is allowed)."""
    start = text.find("{")
    while start >= 0:
        depth, in_str, esc = 0, False, False
        for i in range(start, len(text)):
            c = text[i]
            if in_str:
                if esc:
                    esc = False
                elif c == "\\":
                    esc = True
                elif c == '"':
                    in_str = False
                continue
            if c == '"':
                in_str = True
            elif c == "{":
                depth += 1
            elif c == "}":
                depth -= 1
                if depth == 0:
                    try:
                        doc = json.loads(text[start : i + 1])
                    except ValueError:
                        break
                    return doc if isinstance(doc, dict) else None
        start = text.find("{", start + 1)
    return None


def _set_part(
    parts: list[theme_css.Part], kind: str, name: str, make: theme_css.Part | None
) -> str | None:
    """Replace, add or (``make`` None) remove the part ``name`` of ``kind``; what happened."""
    index = next((i for i, p in enumerate(parts) if p.kind == kind and p.name == name), None)
    if make is None:
        if index is None:
            return None
        del parts[index]
        return f"removed {name}"
    if index is None:
        parts.append(make)
        return f"added {name}"
    if (parts[index].head, parts[index].body) == (make.head, make.body):
        return None
    parts[index] = make
    return f"changed {name}"


def apply_patch(
    theme: dict[str, Any], patch: dict[str, Any]
) -> tuple[dict[str, Any], list[str], list[str]]:
    """The theme with the patch applied, what changed, and what of the patch was left out."""
    out = {k: (dict(v) if isinstance(v, dict) else v) for k, v in theme.items()}
    changed: list[str] = []
    notes: list[str] = []
    fields = patch.get("set")
    if isinstance(fields, dict):
        for field, value in fields.items():
            if field == "key":
                notes.append("set.key: the key stays (the stored theme of that key is replaced)")
            elif field not in _FIELDS:
                notes.append(f"set.{field}: not a field of a theme")
            elif field in _OBJECTS and isinstance(value, dict):
                merged = {**(out.get(field) or {}), **value}
                if merged != out.get(field):
                    out[field] = merged
                    changed.append(field)
            elif value != out.get(field):
                out[field] = value
                changed.append(field)
    css = patch.get("css")
    if isinstance(css, str) and css.strip() and css.strip() != (out.get("css") or "").strip():
        out["css"] = css.strip()
        changed.append("css (all of it)")
    parts = theme_css.split(out.get("css") or "")
    before = len(changed)
    variables = patch.get("vars")
    if isinstance(variables, dict):
        for name, value in variables.items():
            if not _VAR.match(str(name)):
                notes.append(f"vars.{name}: a variable starts with --")
                continue
            if value is not None and (
                not isinstance(value, str | int | float) or "{" in str(value)
            ):
                notes.append(f"vars.{name}: the value must be one CSS value")
                continue
            make = (
                None
                if value is None
                else theme_css.Part("decl", f"{name}: {str(value).strip().rstrip(';')};")
            )
            if done := _set_part(parts, "decl", str(name), make):
                changed.append(done)
    rules = patch.get("rules")
    if isinstance(rules, list):
        for rule in rules:
            if not isinstance(rule, dict) or not isinstance(rule.get("selector"), str):
                notes.append("rules: each rule needs a selector")
                continue
            selector = theme_css.norm_selector(rule["selector"])
            body = rule.get("css")
            if not selector or selector.startswith("@") or "{" in selector:
                notes.append(f"rules: {selector!r} is not a selector")
                continue
            if body is not None and (not isinstance(body, str) or "{" in body or "}" in body):
                notes.append(f"rules.{selector}: css holds the declarations only, no braces")
                continue
            make = None if body is None else theme_css.Part("rule", selector, body.strip())
            if done := _set_part(parts, "rule", selector, make):
                changed.append(done)
    frames = patch.get("keyframes")
    if isinstance(frames, dict):
        for name, body in frames.items():
            if not _KEYFRAMES.match(str(name)):
                notes.append(f"keyframes.{name}: not a name")
                continue
            if body is not None and not isinstance(body, str):
                notes.append(f"keyframes.{name}: the frames as text")
                continue
            head = theme_css.norm_selector(f"@keyframes {name}")
            make = None if body is None else theme_css.Part("at", head, body.strip())
            if done := _set_part(parts, "at", head, make):
                changed.append(done)
    if len(changed) > before:
        out["css"] = theme_css.join(parts)
    if len(out.get("css") or "") > MAX_CSS:
        notes.append(f"css: longer than {MAX_CSS} characters")
    if not out.get("css"):
        out.pop("css", None)
    return out, changed, notes


_HEX_OR_WHITE = re.compile(r"#(?:[0-9a-f]{6}|[0-9a-f]{3})\b|\bwhite\b", re.I)


def fix_contrast(theme: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
    """In a dark theme (``--bg`` dark), the rules' own light backgrounds become the variables
    (the stage ``var(--bg)``, everything else ``var(--surface)``) and their own dark text
    ``var(--text)``: what the lint's contrast finding names, done without a model."""
    from topcoat_hub.ui.themes import _luminance

    parts = theme_css.split(theme.get("css") or "")
    bg = next((p.head.split(":", 1)[1] for p in parts if p.kind == "decl" and p.name == "--bg"), "")
    lum = _luminance(bg.strip().rstrip(";").strip())
    if lum is None or lum > 0.25:
        return theme, []
    changed: list[str] = []

    def fix_rule(part: theme_css.Part) -> None:
        decls = theme_css.declarations(part.body)
        out: list[str] = []
        touched = False
        for prop, value in decls:
            colours = _HEX_OR_WHITE.findall(value)
            if prop in ("background", "background-color", "background-image") and any(
                (_luminance(c) or 0) > 0.7 for c in colours
            ):
                stage = ".dt-stage" in part.head or ".dt-root" in part.head
                value = "var(--bg)" if stage else "var(--surface)"
                prop = "background"
                touched = True
            elif prop == "color" and any((_luminance(c) or 1) < 0.25 for c in colours):
                value = "var(--text)"
                touched = True
            out.append(f"{prop}: {value};")
        if touched:
            part.body = "\n".join(out)
            changed.append(f"changed {part.name}")

    for part in parts:
        if part.kind == "rule":
            fix_rule(part)
        elif part.kind == "at" and part.head.lower().startswith(("@media", "@supports")):
            inner = theme_css.split(part.body)
            before = len(changed)
            for p in inner:
                if p.kind == "rule":
                    fix_rule(p)
            if len(changed) > before:
                part.body = theme_css.join(inner)
    if not changed:
        return theme, []
    return {**theme, "css": theme_css.join(parts)}, changed


def starter(key: str, name: str) -> dict[str, Any]:
    """An empty theme to begin with: valid, dark, nothing of its own yet."""
    return {
        "key": key,
        "name": name,
        "base": "terminal",
        "layout": {"kind": "cascade"},
        "bar": {"position": "bottom", "items": ["start", "programs", "clock", "status", "me"]},
        "chrome": {"buttons": ["minimise", "maximise", "close"], "titlebar": "top"},
        "motion": {"style": "fade", "ms": 180},
    }
