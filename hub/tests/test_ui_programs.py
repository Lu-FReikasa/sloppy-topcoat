"""The programs of the desktop (D70, docs/DESKTOP.md): the built-in ones name known views and tool
groups, admin views sit in admin programs, and a person's choices keep one clamped shape."""

import pytest

from topcoat_hub import tool_groups
from topcoat_hub.config import REPO_ROOT
from topcoat_hub.ui import prefs
from topcoat_hub.ui.access import EVERYONE, PLANNED_GROUPS
from topcoat_hub.ui.runtime import build_runtime
from topcoat_hub.ui.seeds import BUILTIN_PROFILES, BUILTIN_TEMPLATES
from topcoat_hub.ui.store import MAX_MODULES

RUNTIME = build_runtime(REPO_ROOT / "proto")


def test_the_manifest_is_consistent() -> None:
    ids = RUNTIME.manifest.ids()
    assert len(ids) == len(set(ids))
    known = {*tool_groups.KEYS, *PLANNED_GROUPS}
    for info in RUNTIME.manifest.modules.values():
        assert info.kind in ("page", "widget")
        assert info.needs is None or info.needs in known, info.id
        if info.needs is not None:
            assert info.needs != "web", "the web is the internet switch, not a group a module needs"
    assert "layout.doc" not in ids  # the developer window is gone


def test_every_builtin_program_names_known_views_and_groups() -> None:
    keys = [t["key"] for t in BUILTIN_TEMPLATES]
    assert len(keys) == len(set(keys))
    assert set(EVERYONE) <= set(keys)
    for t in BUILTIN_TEMPLATES:
        assert 1 <= len(t["modules"]) <= MAX_MODULES
        assert t["open"] == t["modules"][:1]  # a program starts with its first view
        for m in t["modules"]:
            assert m in RUNTIME.manifest, (t["key"], m)
            info = RUNTIME.manifest.get(m)
            if info and info.admin:
                assert t["requires_role"] == "admin", f"{t['key']} holds the admin view {m}"
        for g in t["agent"]["tool_scope"]["groups"] or []:
            assert g in tool_groups.SELECTABLE or g in PLANNED_GROUPS, (t["key"], g)
    assert sum(1 for p in BUILTIN_PROFILES if p["is_default"]) == 1


def test_the_programs_everybody_has_need_no_admin_and_have_no_tools() -> None:
    by_key = {t["key"]: t for t in BUILTIN_TEMPLATES}
    for key in EVERYONE:
        t = by_key[key]
        assert t["requires_role"] is None
        assert (
            t["agent"]["tool_scope"]["groups"] == []
            and t["agent"]["tool_scope"]["internet"] is False
        )


def test_prefs_have_one_shape_whatever_was_stored() -> None:
    assert prefs.effective(None) == prefs.DEFAULT_PREFS
    old = {"shell": "desk", "theme": {"hue": 300, "ornament": "double"}}
    assert prefs.effective(old) == {
        "hue": 300,
        "look": "terminal",
        "desktop": prefs.DEFAULT_DESKTOP,
    }
    messy = {
        "hue": 999,
        "desktop": {
            "open": ["chat", "chat", "Bad Key", "inbox", 3],
            "minimised": ["inbox", "admin"],
            "focus": "admin",
            "maximised": "chat",
            "views": {
                "chat": {"module": "page.chat", "props": {"id": "c1", "x": [1], "BAD": "y"}},
                "inbox": {"module": "<script>"},
                "../x": {"module": "page.chat"},
            },
        },
    }
    clean = prefs.effective(messy)
    assert clean["hue"] == 359
    d = clean["desktop"]
    assert d["open"] == ["chat", "inbox"]
    assert d["minimised"] == ["inbox"] and d["focus"] is None and d["maximised"] == "chat"
    assert d["views"] == {"chat": {"module": "page.chat", "props": {"id": "c1"}}}


def test_a_change_must_be_valid() -> None:
    assert prefs.apply_patch(None, {"hue": 20})["hue"] == 20
    with pytest.raises(prefs.PrefsError):
        prefs.apply_patch(None, {"hue": 400})
    with pytest.raises(prefs.PrefsError):
        prefs.apply_patch(None, {"desktop": []})
    after = prefs.apply_patch({"hue": 10}, {"desktop": {"open": ["settings"], "focus": "settings"}})
    assert after == {
        "hue": 10,
        "look": "terminal",
        "desktop": {
            "open": ["settings"],
            "minimised": [],
            "focus": "settings",
            "maximised": None,
            "views": {},
        },
    }


def test_the_look_is_the_key_of_a_theme() -> None:
    assert prefs.apply_patch(None, {"look": "leitstand"})["look"] == "leitstand"
    assert prefs.apply_patch(None, {"look": "schreibtisch"})["look"] == "schreibtisch"
    # a custom theme: only the shape is checked; if it is gone the web draws the terminal
    assert prefs.apply_patch(None, {"look": "wall-2"})["look"] == "wall-2"
    assert prefs.effective({"look": "glas"})["look"] == "glas"
    assert prefs.effective({"look": "Not A Key"})["look"] == "terminal"
    assert prefs.effective({"look": 5})["look"] == "terminal"
    for bad in ("Cyber Punk", "", "x" * 41, "-dash", 7):
        with pytest.raises(prefs.PrefsError):
            prefs.apply_patch(None, {"look": bad})
