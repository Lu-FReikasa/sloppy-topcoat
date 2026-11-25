"""The hub's lint of a theme's css: what matches nothing or is void (live 2026-11-15, task 1.7)."""

import json
from pathlib import Path

from topcoat_hub.project_runs import theme_result
from topcoat_hub.ui.themes import lint_theme_answer, surface, theme_lint

BASE = {
    "key": "windows-xp-luna",
    "name": "Windows XP Luna",
    "base": "schreibtisch",
    "layout": {"kind": "cascade"},
    "bar": {"position": "bottom", "items": ["start", "programs", "clock"]},
    "chrome": {"buttons": ["minimise", "maximise", "close"], "titlebar": "top"},
    "motion": {"style": "fade", "ms": 200},
}

# the css of the theme 1.7 wrote (shortened): it passed the model's check and drew almost nothing
LIVE_CSS = """.dt-theme-windows-xp-luna {
  --taskbar-grad: linear-gradient(to bottom, #245edb 0%, #003399 100%);
}
.dt-root { background: #4da6ff; }
.dt-taskbar { background: var(--taskbar-grad); }
.dt-min, .dt-maxi {
  width: 22px;
  height: 22px;
}
.dt-close { background: #e81123; }
"""


def lint(css: str, **doc: str) -> list[str]:
    return theme_lint({**BASE, **doc, "css": css})


def test_the_live_theme_is_told_that_state_classes_are_no_buttons() -> None:
    # its variables and .dt-root are drawn now (the web nests them as the root) and .dt-close is
    # a class of the desktop; what is left is the 22px maximised window
    found = lint(LIVE_CSS)
    assert len(found) == 1 and ".dt-minimise, .dt-maximise, .dt-close" in found[0]
    assert lint(".dt-program.dt-maxi { border-radius: 0; } .dt-task.dt-min { opacity: .6; }") == []
    assert lint(".dt-maxi .dt-titlebar { cursor: default; }") == []


def test_a_class_the_desktop_does_not_draw_is_named_with_the_one_meant() -> None:
    found = lint(".dt-window { color: red; }\n.dt-taskbr { color: red; }")
    assert len(found) == 2 and ".dt-window is not drawn" in found[0]
    assert "meant .dt-taskbar" in found[1]
    # the root's own classes (its key, its look, the bar's place) are not unknown
    assert lint(".dt-theme-x { } .dt-look-terminal .dt-task, .dt-bar-top .dt-taskbar { }") == []


def test_a_variable_set_nowhere_is_named_and_one_of_the_base_is_not() -> None:
    found = lint(".dt-task { background: var(--grad); color: var(--text); }")
    assert len(found) == 1 and "var(--grad) is used but set nowhere" in found[0]
    assert lint("--grad: red;\n.dt-task { background: var(--grad); }") == []
    assert lint(".dt-task { background: var(--grad, red); }") == []  # it has a fallback
    assert lint(".dt-stage { background: var(--mat); }") == []  # the desk sets it
    assert lint(".dt-stage { background: var(--mat); }", base="terminal") != []


def test_a_declaration_without_its_semicolon_is_named_and_a_long_value_is_not() -> None:
    found = lint("--bg: #000\n--text: #fff;\n.dt-task {\n  color: red\n}")
    assert len(found) == 1 and "line 1" in found[0]
    assert lint(".dt-task {\n  background: linear-gradient(\n    red,\n    blue);\n}") == []


def test_an_answer_is_read_like_the_hub_reads_it() -> None:
    assert "no JSON" in lint_theme_answer("I cannot")[0]
    assert "not valid JSON" in lint_theme_answer("{ nope }")[0]
    good = {**BASE, "css": ".dt-task { color: red; }"}
    assert lint_theme_answer("Here:\n" + json.dumps(good)) == []
    assert any("required" in p for p in lint_theme_answer(json.dumps({"key": "x"})))


def test_what_the_revisions_left_stands_under_the_theme_and_it_is_still_drawn() -> None:
    text, problems = theme_result(json.dumps({**BASE, "css": ".dt-window { color: red; }"}))
    assert problems == [] and "## Hinweise" in text and ".dt-window" in text


def test_the_surface_names_every_variable_the_base_sheets_set() -> None:
    root = Path(__file__).resolve().parents[2] / "web/src/desktop"
    known = surface()
    import re

    for sheet, base in (("leitstand.css", "leitstand"), ("schreibtisch.css", "schreibtisch")):
        own = set(
            re.findall(r"^\s*(--[\w-]+)\s*:", (root / sheet).read_text(encoding="utf-8"), re.M)
        )
        listed = set(known["variables"]) | set(known["base_variables"][base])
        assert own <= listed, f"{sheet}: add {sorted(own - listed)} to theme-surface.json"


def test_a_state_on_a_part_that_never_carries_it_is_named() -> None:
    # the second live run of 1.7 wrote .dt-tasks.dt-open: the open entries are .dt-task.dt-open
    found = lint(".dt-tasks.dt-open { background: rgba(255,255,255,.2); }")
    assert len(found) == 1 and ".dt-open stands only on .dt-task, never on .dt-tasks" in found[0]
    fine = (
        ".dt-task.dt-open, .dt-start.dt-front, .dt-program.dt-focused .dt-titlebar,"
        " .dt-task:not(.dt-min) { color: red; }"
    )
    assert lint(fine) == []
    assert "stands only on .dt-program" in lint(".dt-titlebar.dt-focused { color: red; }")[0]


# --- motion (live 2026-11-16, Wireframe theme v1-v3 in task 1.8.2) --------------------------------


def _lint_css(css: str) -> list[str]:
    return theme_lint({**BASE, "base": "terminal", "css": css})


def test_an_endless_wobble_of_the_selected_window_is_named() -> None:
    css = (
        "@keyframes wobble { from { transform: rotate(-1deg); } to { transform: rotate(1deg); } }\n"
        ".dt-program.dt-front { animation: wobble 0.5s ease-in-out infinite; }\n"
    )
    found = _lint_css(css)
    assert any("without end" in f and ".dt-program.dt-front" in f for f in found)


def test_an_endless_lamp_is_a_signal_and_passes() -> None:
    css = "@keyframes blink { 50% { opacity: .3; } }\n.dt-lamp { animation: blink 1s infinite; }\n"
    assert not any("without end" in f for f in _lint_css(css))


def test_a_window_that_keeps_its_last_frame_is_named_but_closing_may() -> None:
    held = ".dt-program { animation: pop var(--dt-ms) ease-out forwards; }"
    assert any("forwards/both" in f for f in _lint_css(held))
    closing = ".dt-program.dt-closing { animation: shrink var(--dt-ms) ease-in forwards; }"
    assert not any("forwards/both" in f for f in _lint_css(closing))


def test_a_window_that_fades_when_the_pointer_leaves_is_named() -> None:
    css = ".dt-program:not(:hover) { opacity: 0.6; transition: opacity .3s; }"
    assert any("pointer passes over" in f for f in _lint_css(css))
    button = ".dt-close:hover { opacity: 0.8; }"
    assert not any("pointer passes over" in f for f in _lint_css(button))


def test_rules_inside_media_are_looked_at() -> None:
    css = "@media (min-width: 1px) { .dt-titlebar { animation: x 1s infinite; } }"
    assert any("without end" in f for f in _lint_css(css))


def test_a_dark_theme_with_light_rules_of_their_own_is_named() -> None:
    """Live 2026-11-17 (workshop, "make it black"): the variables went black, the rules that named
    their colours (#d0d0d0 title bars, #fff bodies, #333 text) stayed as they were."""
    css = (
        "--bg: #000000;\n--surface: #111;\n"
        ".dt-titlebar { background: linear-gradient(to bottom, #d0d0d0, #c0c0c0); }\n"
        ".dt-program-body { background: #fff; color: #333; }\n"
        ".dt-close { background: #ff5555; }\n"
    )
    found = _lint_css(css)
    light = next(f for f in found if "light background" in f)
    assert ".dt-titlebar (#d0d0d0)" in light and ".dt-program-body (#fff)" in light
    assert ".dt-close" not in light  # a red button is no light surface
    assert any("dark text" in f and "#333" in f for f in found)
    assert not any("light background" in f for f in _lint_css(css.replace("#000000", "#f0f0f0")))
