"""Custom themes of the desktop (docs/THEMES.md): checked, stored, listed.

A theme is data and a style sheet, never code. The shape is ``proto/ui/theme.schema.json``; the
rules for the style sheet are here (and, for defence, once more in the web build -- both are tested
against ``proto/ui/theme-css-cases.json``). The built-in themes live in the web build, their
keys are reserved."""

import difflib
import json
import os
import re
import uuid
from datetime import UTC, datetime
from functools import cache
from pathlib import Path
from typing import Any

import jsonschema
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from topcoat_hub.config import REPO_ROOT
from topcoat_hub.models import UiTheme

BUILTIN = ("terminal", "leitstand", "schreibtisch", "terminal-nest")
MAX_CSS = 40_000
MAX_THEMES = 50
_DATA_IMAGE = re.compile(r"^data:image/(png|jpeg|gif|webp|svg\+xml)[;,]", re.I)


class ThemeError(ValueError):
    def __init__(self, problems: list[str], status: int = 400) -> None:
        super().__init__("; ".join(problems))
        self.problems = problems
        self.status = status


def _proto_dir() -> Path:
    root = os.environ.get("TOPCOAT_PROTO_ROOT")
    return (Path(root) if root else REPO_ROOT / "proto") / "ui"


@cache
def _validator() -> jsonschema.Draft202012Validator:
    schema = json.loads((_proto_dir() / "theme.schema.json").read_text(encoding="utf-8"))
    return jsonschema.Draft202012Validator(schema)


def schema_text() -> str:
    """What a model that writes a theme needs of the format: the guide (a copy of docs/THEMES.md,
    shipped with the protocol files: the classes a style sheet may reach, the limits) and the JSON
    Schema the hub checks."""
    guide = _proto_dir() / "theme-guide.md"
    schema = (_proto_dir() / "theme.schema.json").read_text(encoding="utf-8")
    text = guide.read_text(encoding="utf-8") if guide.exists() else ""
    return f"{text}\n\nJSON Schema:\n{schema}".strip()


def css_problems(css: str) -> list[str]:
    """Why a style sheet may not be used: it loads nothing, runs nothing and cannot hide from the
    check (comments and backslash escapes are removed before it looks)."""
    out: list[str] = []
    if len(css) > MAX_CSS:
        out.append(f"css: at most {MAX_CSS} characters")
    flat = re.sub(r"/\*.*?\*/", "", css, flags=re.S).replace("\\", "")
    if re.search(r"@import", flat, re.I):
        out.append("css: @import is not allowed")
    if re.search(r"@charset|@namespace", flat, re.I):
        out.append("css: @charset and @namespace are not allowed")
    for m in re.finditer(r"""url\(\s*(['"]?)([^'")]*)\1\s*\)""", flat, re.I):
        if not _DATA_IMAGE.match(m.group(2).strip()):
            out.append("css: url() may only hold an inline data:image")
            break
    if re.search(
        r"expression\s*\(|javascript:|behavior\s*:|-moz-binding|<\/?\s*style|<\s*script",
        flat,
        re.I,
    ):
        out.append("css: script-like constructs are not allowed")
    return out


_SHAPES: dict[str, tuple[str, dict[str, Any]]] = {
    # field: (the key a bare value stands for, the defaults of the rest)
    "layout": ("kind", {}),
    "bar": ("position", {"items": ["start", "programs", "clock", "status", "me"]}),
    "motion": ("style", {"ms": 150}),
}
_ALLOWED = {
    "layout": {"kind"},
    "bar": {"position", "items"},
    "chrome": {"buttons", "titlebar"},
    "motion": {"style", "ms", "close", "overlay"},
}


def normalise(doc: Any) -> tuple[Any, list[str]]:
    """A theme in the shape the schema wants, from the shapes small models write (live
    2026-11-11: Qwen3.5-9B and gpt-oss-20b wrote ``"layout": "tile"``; Qwen3-30B added
    ``columns``/``rows`` to ``layout``). A bare value becomes its object, keys the schema does
    not know are dropped. Nothing else is changed; what is still wrong, the check names."""
    if not isinstance(doc, dict):
        return doc, []
    out, notes = dict(doc), []
    for field, (key, rest) in _SHAPES.items():
        value = out.get(field)
        if isinstance(value, str):
            out[field] = {key: value, **rest}
            notes.append(f"{field}: {value!r} read as {{{key!r}: {value!r}}}")
    for field, keys in _ALLOWED.items():
        value = out.get(field)
        if isinstance(value, dict) and set(value) - keys:
            dropped = sorted(set(value) - keys)
            out[field] = {k: v for k, v in value.items() if k in keys}
            notes.append(f"{field}: left out {', '.join(dropped)}")
    return out, notes


@cache
def surface() -> dict[str, Any]:
    """What a style sheet can reach (proto/ui/theme-surface.json): the classes the desktop draws,
    the prefixes of the classes on its root, the variables the bases and pages read."""
    data: dict[str, Any] = json.loads(
        (_proto_dir() / "theme-surface.json").read_text(encoding="utf-8")
    )
    return data


_CLASS = re.compile(r"\.(dt-[a-z0-9-]+)", re.I)
_VAR_USE = re.compile(r"var\(\s*(--[\w-]+)\s*([,)])")
_VAR_SET = re.compile(r"(--[\w-]+)\s*:")
_BARE_LINE = re.compile(r"^[a-z-][\w-]*\s*:[^{}]*$", re.I)
_COMBINATOR = re.compile(r"\s*[\s>+~]\s*")  # the compounds of one selector
_PRELUDE = re.compile(r"([^{};]+)\{")  # the selector before a block (or an at-rule's prelude)
# a rule whose subject is a state class alone (".dt-maxi {", "x .dt-min:hover,"): it styles every
# maximised window or minimised entry, which a model means as a button (live 2026-11-15)
_STATE_ALONE = re.compile(r"(^|[\s>+~])\.dt-(min|maxi)(:[\w-]+(\([^)]*\))?)*$")
MAX_LINT = 8


def theme_lint(doc: Any) -> list[str]:
    """What in a valid theme's css probably does not do what was meant: rules for classes the
    desktop does not draw (they match nothing), variables that are used but set nowhere (the whole
    declaration is void) and declarations a line break ended without ``;`` (the next line is
    swallowed). Not a refusal: the findings go back to the writer of a theme (theme_design), who
    could not see the drawn desktop. Live 2026-11-15: ``.dt-close`` and ``var(--taskbar-grad)``
    passed every check and the theme drew almost nothing of its css."""
    if not isinstance(doc, dict) or not isinstance(doc.get("css"), str):
        return []
    flat = re.sub(r"/\*.*?\*/", "", doc["css"], flags=re.S)
    known = surface()
    classes = set(known["classes"])
    prefixes = tuple(known["root_prefixes"])
    out: list[str] = []
    unknown = list(
        dict.fromkeys(
            c.lower()
            for c in _CLASS.findall(flat)
            if c.lower() not in classes and not c.lower().startswith(prefixes)
        )
    )
    for name in unknown:
        near = difflib.get_close_matches(name, known["classes"], n=2, cutoff=0.6)
        hint = f" (meant {' or '.join('.' + n for n in near)}?)" if near else ""
        out.append(
            f"css: .{name} is not drawn by the desktop, a rule for it matches nothing{hint}; "
            "use only the classes the guide lists"
        )
    selectors = [s.strip() for prelude in _PRELUDE.findall(flat) for s in prelude.split(",")]
    if any(_STATE_ALONE.search(s) for s in selectors):
        out.append(
            "css: .dt-min and .dt-maxi are states, not buttons (a minimised taskbar entry, a "
            "maximised window); write .dt-task.dt-min or .dt-program.dt-maxi if that is meant; "
            "the title bar buttons are .dt-minimise, .dt-maximise, .dt-close"
        )
    states: dict[str, list[str]] = known["states"]
    for compound in dict.fromkeys(c for s in selectors for c in _COMBINATOR.split(s) if c):
        names = {n.lower() for n in _CLASS.findall(compound)}
        for state in sorted(names & set(states)):
            wrong = sorted(n for n in names - set(states) if n not in states[state])
            if wrong:  # live 2026-11-15: .dt-tasks.dt-open (the entries are .dt-task)
                on = " or ".join("." + n for n in states[state])
                out.append(
                    f"css: {compound} matches nothing: .{state} stands only on {on}, "
                    f"never on .{wrong[0]}"
                )
    given = set(_VAR_SET.findall(flat)) | set(known["variables"]) | set(known["set_by_desktop"])
    base = doc.get("base")
    if isinstance(base, str):
        given |= set(known["base_variables"].get(base, []))
    void = dict.fromkeys(
        name for name, end in _VAR_USE.findall(flat) if end == ")" and name not in given
    )
    for name in void:
        out.append(
            f"css: var({name}) is used but set nowhere, so every declaration with it is void; set "
            f"it as a bare declaration at the top of css ({name}: ...;) or use a variable of "
            "the base"
        )
    out.extend(motion_lint(flat))
    out.extend(contrast_lint(flat))
    lines = [raw.strip() for raw in flat.split("\n")]
    for i, line in enumerate(lines, start=1):
        following = next((x for x in lines[i:] if x), "}")
        open_value = line.count("(") != line.count(")")  # a value that goes on (a gradient)
        ends = line.endswith((";", ",", "{", "}")) or following.startswith("}")
        if _BARE_LINE.match(line) and not open_value and not ends:
            out.append(f"css line {i}: {line[:60]!r} ends without ';' and swallows the next line")
    return out[:MAX_LINT]


# the parts of a window and the taskbar: an animation on them is felt at every look
_WINDOW_PARTS = re.compile(r"\.dt-(program|titlebar|program-body|title|task|taskbar|stage)\b")
# a lasting signal is meant to go on (a lamp, a busy mark)
_LASTING = re.compile(r"\.dt-(lamp|signal|busy|spinner|loading)\b")
# the moments a window animates on its own; any other state of .dt-program is "the window as it is"
_MOMENTS = re.compile(r"\.dt-(closing|arriving|leaving|opening)\b")


def _subject(selector: str) -> str:
    """The last compound of a selector: what the rule styles."""
    compounds = [c for c in _COMBINATOR.split(selector.strip()) if c]
    return compounds[-1] if compounds else ""


def motion_lint(css: str) -> list[str]:
    """Animations that the person feels as a fault (live 2026-11-16, Wireframe theme v1-v3): an
    endless animation on a window part (the selected window wobbled all the time), a window that
    keeps its last frame (``forwards`` on ``.dt-program`` holds ``transform``: the desktop moves a
    dragged window and the window did not follow), and a window that reacts to the mouse passing
    over it (windows faded when the pointer left them)."""
    from topcoat_hub.ui import theme_css

    out: list[str] = []
    for selector_list, body in theme_css.rules(theme_css.split(css)):
        decls = theme_css.declarations(body)
        motion = " ".join(v.lower() for p, v in decls if p.startswith("animation"))
        fades = any(p in ("opacity", "filter") or p.startswith("animation") for p, _ in decls)
        for selector in (s.strip() for s in selector_list.split(",")):
            subject = _subject(selector)
            if not subject:
                continue
            on_window = bool(_WINDOW_PARTS.search(subject))
            if motion and "infinite" in motion and on_window and not _LASTING.search(subject):
                out.append(
                    f"css: {selector} animates without end (infinite): a window or taskbar part "
                    "that never stops moving; animate a change once (opening, closing, focus) and "
                    "let it rest"
                )
            if (
                re.search(r"\b(forwards|both)\b", motion)
                and ".dt-program" in subject
                and not _MOMENTS.search(subject)
            ):
                out.append(
                    f"css: {selector} keeps the last frame of its animation (forwards/both): the "
                    "window then holds transform and opacity for good and does not follow when it "
                    "is dragged; leave the fill mode out on .dt-program (the end frame should be "
                    "its normal look)"
                )
            if (
                ":hover" in subject
                and ".dt-program" in subject
                and fades
                and not re.search(r"\.dt-(close|minimise|maximise|task)\b", subject)
            ):
                out.append(
                    f"css: {selector} changes a whole window when the pointer passes over or "
                    "leaves it (opacity, filter or an animation): windows flicker while the "
                    "mouse moves; animate opening, closing and focus instead"
                )
    return list(dict.fromkeys(out))


_HEX = re.compile(r"#([0-9a-f]{6}|[0-9a-f]{3})\b", re.I)
_NAMED = {"white": "#ffffff", "black": "#000000"}


def _luminance(colour: str) -> float | None:
    """0 (black) .. 1 (white) of a hex colour or white/black; None for anything else."""
    colour = _NAMED.get(colour.lower(), colour)
    m = _HEX.fullmatch(colour.strip())
    if not m:
        return None
    h = m.group(1)
    if len(h) == 3:
        h = "".join(c * 2 for c in h)
    r, g, b = (int(h[i : i + 2], 16) for i in (0, 2, 4))
    return (0.2126 * r + 0.7152 * g + 0.0722 * b) / 255


def contrast_lint(css: str) -> list[str]:
    """A dark theme (``--bg`` dark) whose rules still write light backgrounds or dark text as fixed
    colours: those rules do not follow the variables. Live 2026-11-17 (theme workshop, "make it
    black"): the model set every variable to black and cyan; the title bars (#d0d0d0) and the
    window bodies (#fff) stayed light because their rules named the colours themselves."""
    from topcoat_hub.ui import theme_css

    parts = theme_css.split(css)
    bg = next((p.head.split(":", 1)[1] for p in parts if p.kind == "decl" and p.name == "--bg"), "")
    dark = _luminance(bg.strip().rstrip(";").strip())
    if dark is None or dark > 0.25:
        return []
    light: list[str] = []
    dim: list[str] = []
    for selector, body in theme_css.rules(parts):
        for prop, value in theme_css.declarations(body):
            colours = [m.group(0) for m in _HEX.finditer(value)]
            colours += [w for w in re.findall(r"\b(white)\b", value, re.I)]
            if prop in ("background", "background-color", "background-image"):
                bright = [c for c in colours if (_luminance(c) or 0) > 0.7]
                if bright:
                    light.append(f"{selector} ({bright[0]})")
            elif prop == "color":
                low = [c for c in colours if (_luminance(c) or 1) < 0.25]
                if low:
                    dim.append(f"{selector} ({low[0]})")
    out: list[str] = []
    if light:
        out.append(
            f"css: the theme is dark (--bg {bg.strip().rstrip(';')}), but these rules keep a light "
            f"background of their own: {', '.join(dict.fromkeys(light[:6]))}; use var(--surface) / "
            "var(--bg) or a dark colour there"
        )
    if dim:
        out.append(
            "css: dark text on the dark theme in "
            f"{', '.join(dict.fromkeys(dim[:6]))}; use var(--text) or a light colour there"
        )
    return out


def lint_theme_answer(text: str) -> list[str]:
    """The hub's own findings on a theme a model answered (the JSON in its text): what keeps it
    from being stored (theme_problems) and what in its css matches nothing (theme_lint)."""
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        return ["the answer holds no JSON document; answer with the theme as ONE JSON document"]
    try:
        doc = json.loads(text[start : end + 1])
    except ValueError as exc:
        return [f"the theme is not valid JSON: {exc}"]
    doc, _ = normalise(doc)
    return (theme_problems(doc) + theme_lint(doc))[:MAX_LINT]


def theme_problems(doc: Any) -> list[str]:
    """What is wrong with a theme document (empty when it is fine)."""
    if not isinstance(doc, dict):
        return ["the theme must be an object"]
    out = [
        f"{'.'.join(str(p) for p in e.absolute_path) or 'theme'}: {e.message}"
        for e in sorted(_validator().iter_errors(doc), key=lambda e: list(e.absolute_path))
    ]
    if not out:
        if doc["key"] in BUILTIN:
            out.append(f"key: {doc['key']!r} belongs to a built-in theme")
        if isinstance(doc.get("css"), str):
            out.extend(css_problems(doc["css"]))
    return out


def view(row: UiTheme) -> dict[str, Any]:
    """The document the web draws with."""
    doc: dict[str, Any] = {"key": row.key, "name": row.name, **row.manifest}
    if row.base:
        doc["base"] = row.base
    if row.css:
        doc["css"] = row.css
    return doc


class ThemeStore:
    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    async def all(self) -> list[UiTheme]:
        return list((await self.db.scalars(select(UiTheme).order_by(UiTheme.key))).all())

    async def put(self, doc: dict[str, Any], by: uuid.UUID) -> tuple[UiTheme, bool]:
        """Create the theme or replace the one of this key; the row and whether it is new."""
        doc, _ = normalise(doc)
        problems = theme_problems(doc)
        if problems:
            raise ThemeError(problems)
        row = await self.db.get(UiTheme, doc["key"])
        created = row is None
        if row is None:
            if len(await self.all()) >= MAX_THEMES:
                raise ThemeError([f"at most {MAX_THEMES} custom themes"], status=409)
            row = UiTheme(key=doc["key"], rev=0)
            self.db.add(row)
        row.name = doc["name"]
        row.base = doc.get("base")
        row.manifest = {k: doc[k] for k in ("layout", "bar", "chrome", "motion")}
        row.css = doc.get("css", "")
        row.rev += 1
        row.updated_by = by
        row.updated_at = datetime.now(UTC)
        await self.db.flush()
        return row, created

    async def delete(self, key: str) -> bool:
        gone = await self.db.execute(delete(UiTheme).where(UiTheme.key == key))
        return bool(gone.rowcount)  # type: ignore[attr-defined]
