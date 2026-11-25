"""A change to a theme as a patch (ui/theme_patch.py, the theme workshop)."""

from topcoat_hub.ui import theme_css
from topcoat_hub.ui.theme_patch import apply_patch, parse_patch, starter
from topcoat_hub.ui.themes import theme_problems

THEME = {
    **starter("wire", "Wire"),
    "css": "--bg: #f0f0f0;\n--text: #333;\n.dt-titlebar { height: 30px; }\n"
    "@keyframes pop { from { opacity: 0; } to { opacity: 1; } }",
}


def test_the_starter_is_a_valid_theme() -> None:
    assert theme_problems(starter("neu-1", "Neu")) == []


def test_a_patch_changes_only_what_it_names() -> None:
    patch = {
        "reply": "Dunkler.",
        "set": {"motion": {"style": "zoom"}, "key": "other"},
        "vars": {"--bg": "#05070a", "--glow": "0 0 4px #0ff", "--text": None},
        "rules": [{"selector": ".dt-close", "css": "color: var(--glow);"}],
        "keyframes": {"pop": None},
    }
    out, changed, notes = apply_patch(THEME, patch)
    assert out["key"] == "wire" and any("key stays" in n for n in notes)
    assert out["motion"] == {"style": "zoom", "ms": 180}  # merged, ms kept
    parts = {p.name: p for p in theme_css.split(out["css"])}
    assert parts["--bg"].head == "--bg: #05070a;" and "--text" not in parts
    assert "--glow" in parts and parts[".dt-close"].body == "color: var(--glow);"
    assert parts[".dt-titlebar"].body == "height: 30px;"  # untouched
    assert "@keyframes pop" not in parts
    assert {"motion", "changed --bg", "removed --text", "removed @keyframes pop"} <= set(changed)


def test_a_rule_is_found_in_any_spelling_and_replaced_whole() -> None:
    out, changed, _ = apply_patch(
        THEME, {"rules": [{"selector": " .dt-titlebar ", "css": "height: 20px"}]}
    )
    assert changed == ["changed .dt-titlebar"]
    assert "height: 30px" not in out["css"] and "height: 20px" in out["css"]


def test_what_is_not_understood_is_left_out_and_named() -> None:
    out, changed, notes = apply_patch(
        THEME,
        {
            "vars": {"bg": "#000", "--x": "a { b }"},
            "rules": [{"selector": ".a", "css": "x: y } .b { z: w"}, {"css": "x: y;"}],
            "set": {"colour": "red"},
        },
    )
    assert out == THEME and changed == []
    assert len(notes) == 5


def test_the_patch_is_read_from_a_fenced_answer_with_braces_in_strings() -> None:
    text = 'Gern:\n```json\n{"reply": "ok", "keyframes": {"a": "from { x: 1 } to { x: 2 }"}}\n```'
    assert parse_patch(text) == {"reply": "ok", "keyframes": {"a": "from { x: 1 } to { x: 2 }"}}
    assert parse_patch("no json here") is None
