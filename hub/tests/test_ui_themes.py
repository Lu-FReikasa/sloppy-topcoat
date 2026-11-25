"""Custom themes of the desktop: checked, stored, offered to everybody, changed by an admin."""

import json
from pathlib import Path

from fastapi.testclient import TestClient

from tests.conftest import AUTH
from tests.users import make_user
from topcoat_hub.ui import themes

PROTO = Path(__file__).resolve().parents[2] / "proto" / "ui"

WALL = {
    "key": "wall",
    "name": "Wall",
    "base": "terminal",
    "layout": {"kind": "columns"},
    "bar": {"position": "left", "items": ["programs", "status"]},
    "chrome": {"buttons": ["close"], "titlebar": "top"},
    "motion": {"style": "slide", "ms": 200},
    "css": "--accent: hotpink;\n.dt-task { letter-spacing: 1px; }",
}


def test_the_style_sheet_rules_agree_with_the_shared_cases() -> None:
    """The web build runs the same list (theme.shared.test.ts): the two checks cannot drift."""
    cases = json.loads((PROTO / "theme-css-cases.json").read_text(encoding="utf-8"))["cases"]
    assert len(cases) >= 10
    for case in cases:
        assert (themes.css_problems(case["css"]) == []) is case["ok"], case["css"]


def test_a_theme_is_checked_against_the_schema() -> None:
    assert themes.theme_problems(WALL) == []
    assert themes.theme_problems([]) == ["the theme must be an object"]
    bad = {
        **WALL,
        "key": "Bad Key",
        "layout": {"kind": "spiral"},
        "bar": {"position": "middle", "items": ["clock", "clock", "nothing"]},
        "chrome": {"buttons": ["close", "close"], "titlebar": "side"},
        "motion": {"style": "spin", "ms": 5000},
        "base": "elsewhere",
        "extra": 1,
    }
    text = "\n".join(themes.theme_problems(bad))
    for part in (
        "key",
        "layout.kind",
        "bar.position",
        "bar.items",
        "chrome",
        "motion",
        "base",
        "extra",
    ):
        assert part in text, part
    assert any("built-in" in p for p in themes.theme_problems({**WALL, "key": "terminal"}))
    assert any("@import" in p for p in themes.theme_problems({**WALL, "css": "@import 'x';"}))


def test_an_admin_makes_changes_and_deletes_a_theme_and_everybody_sees_it(
    client: TestClient,
) -> None:
    assert client.get("/api/ui/themes", headers=AUTH).json() == []
    r = client.put("/api/ui/themes/wall", headers=AUTH, json=WALL)
    assert r.status_code == 201 and r.json()["key"] == "wall" and r.json()["css"] == WALL["css"]
    r = client.put("/api/ui/themes/wall", headers=AUTH, json={**WALL, "name": "Wall 2"})
    assert r.status_code == 200 and r.json()["name"] == "Wall 2"
    listed = client.get("/api/ui/themes", headers=AUTH).json()
    assert [t["key"] for t in listed] == ["wall"] and listed[0]["name"] == "Wall 2"
    # the bootstrap carries them, so the page draws with them at once
    boot = client.get("/api/ui/bootstrap", headers=AUTH).json()
    assert [t["key"] for t in boot["themes"]] == ["wall"]
    # a person can choose it
    assert client.put("/api/ui/prefs", headers=AUTH, json={"look": "wall"}).json()["look"] == "wall"
    assert client.delete("/api/ui/themes/wall", headers=AUTH).status_code == 204
    assert client.delete("/api/ui/themes/wall", headers=AUTH).status_code == 404
    assert client.get("/api/ui/themes", headers=AUTH).json() == []


def test_a_bad_theme_is_refused_with_its_problems(client: TestClient) -> None:
    r = client.put("/api/ui/themes/wall", headers=AUTH, json={**WALL, "css": "@import 'x';"})
    assert r.status_code == 400
    assert r.json()["detail"]["code"] == "invalid_theme"
    assert any("@import" in p for p in r.json()["detail"]["problems"])
    # the key in the address and in the document are the same thing
    other = client.put("/api/ui/themes/other", headers=AUTH, json=WALL)
    assert other.status_code == 400
    assert client.get("/api/ui/themes", headers=AUTH).json() == []
    # the editor can ask without storing
    check = client.post("/api/ui/themes/check", headers=AUTH, json={**WALL, "key": "terminal"})
    assert check.status_code == 200 and check.json()["problems"]


def test_only_an_admin_changes_themes(client: TestClient) -> None:
    _, user = make_user(client.app.state.settings.database_url, "mia")
    assert client.put("/api/ui/themes/wall", headers=user, json=WALL).status_code == 403
    assert client.delete("/api/ui/themes/wall", headers=user).status_code == 403
    assert client.post("/api/ui/themes/check", headers=user, json=WALL).status_code == 403
    client.put("/api/ui/themes/wall", headers=AUTH, json=WALL)
    assert client.get("/api/ui/themes", headers=user).status_code == 200  # everybody may choose


def test_there_is_a_limit_on_custom_themes(client: TestClient) -> None:
    for i in range(themes.MAX_THEMES):
        r = client.put(f"/api/ui/themes/t{i}", headers=AUTH, json={**WALL, "key": f"t{i}"})
        assert r.status_code == 201
    r = client.put("/api/ui/themes/extra", headers=AUTH, json={**WALL, "key": "extra"})
    assert r.status_code == 409
