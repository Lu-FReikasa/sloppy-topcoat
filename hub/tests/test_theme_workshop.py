"""The theme workshop API (api/theme_workshop.py): a turn is one patch, checked by the hub; a
broken patch goes back to the model once; saving stores the theme and files an iteration."""

import json
import uuid
from typing import Any

from fastapi.testclient import TestClient

from tests.conftest import AUTH
from tests.fakenode import online_node
from tests.test_chat import use_model
from topcoat_hub.models import DEV_ADMIN_ID as USER
from topcoat_hub.project_iterations import theme_in
from topcoat_hub.project_store import ProjectStore
from topcoat_hub.ui.theme_patch import starter

DRAFT = {**starter("wire-ws", "Wire"), "css": "--bg: #f0f0f0;"}


def _model(client: TestClient, connect: Any, answers: list[str]) -> list[dict[str, Any]]:
    fake = online_node(client, connect, "node-a", loaded=["small"])
    use_model(client)
    asked: list[dict[str, Any]] = []

    def answer(req: dict) -> str | None:
        if req["messages"][0]["content"].startswith("You are the theme workshop"):
            asked.append(req)
            return answers[min(len(asked), len(answers)) - 1]
        return None

    fake.answer_fn = answer
    return asked


def test_a_turn_applies_the_patch_and_sends_only_the_current_theme(
    client: TestClient, connect: Any
) -> None:
    asked = _model(client, connect, [json.dumps({"reply": "Schwarz.", "vars": {"--bg": "#000"}})])
    r = client.post(
        "/api/ui/workshop/turn",
        json={
            "theme": DRAFT,
            "message": "dunkler",
            "requirements": ["ruhig"],
            "history": [
                {"role": "person", "text": "mach es retro"},
                {"role": "workshop", "text": "Gern."},
            ],
        },
        headers=AUTH,
    )
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["applied"] and out["reply"] == "Schwarz." and out["calls"] == 1
    assert out["theme"]["css"] == "--bg: #000;" and out["changed"] == ["changed --bg"]
    prompt = asked[0]["messages"][1]["content"]
    assert "--bg: #f0f0f0" in prompt and "- ruhig" in prompt and "Person: mach es retro" in prompt
    assert asked[0].get("chat_template_kwargs") == {"enable_thinking": False}


def test_a_patch_that_brings_a_wobble_goes_back_once_and_the_fix_is_taken(
    client: TestClient, connect: Any
) -> None:
    bad = {
        "reply": "Wackelt.",
        "rules": [{"selector": ".dt-program.dt-front", "css": "animation: w 1s infinite;"}],
    }
    good = {
        "reply": "Einmal.",
        "rules": [{"selector": ".dt-program.dt-front", "css": "animation: w 200ms;"}],
    }
    asked = _model(client, connect, [json.dumps(bad), json.dumps(good)])
    out = client.post(
        "/api/ui/workshop/turn", json={"theme": DRAFT, "message": "Fokus animieren"}, headers=AUTH
    ).json()
    assert out["calls"] == 2 and out["applied"] and out["reply"].endswith("Einmal.")
    assert "infinite" not in out["theme"]["css"] and not any(
        "without end" in f for f in out["lint"]
    )
    assert "infinite" in asked[1]["messages"][-1]["content"]


def test_a_patch_that_still_breaks_the_theme_leaves_the_draft(
    client: TestClient, connect: Any
) -> None:
    broken = json.dumps({"reply": "x", "set": {"layout": {"kind": "spiral"}}})
    _model(client, connect, [broken, broken])
    out = client.post(
        "/api/ui/workshop/turn", json={"theme": DRAFT, "message": "Spirale"}, headers=AUTH
    ).json()
    assert not out["applied"] and out["problems"] and out["theme"] == DRAFT


def test_saving_stores_the_theme_and_files_the_next_iteration(client: TestClient) -> None:
    store = ProjectStore(client.app.state.sessionmaker)

    async def setup() -> Any:
        project = await store.create_project(USER, title=f"WS {uuid.uuid4().hex[:6]}")
        return await store.create_node(
            USER, project.id, kind="task", title="Theme", body="Zielanwendung: dev\n"
        )

    task = client.portal.call(setup)
    theme = {**DRAFT, "key": f"ws-{uuid.uuid4().hex[:6]}"}
    r = client.post(
        "/api/ui/workshop/save",
        json={
            "theme": theme,
            "task_id": str(task.id),
            "steps": [{"message": "dunkler", "reply": "Schwarz."}],
        },
        headers=AUTH,
    )
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["stored"] and out["iteration"] == 1
    listed = client.get("/api/ui/themes", headers=AUTH).json()
    assert any(
        t["key"] == theme["key"]
        for t in (listed.get("themes") if isinstance(listed, dict) else listed)
    )
    result = client.portal.call(lambda: store.get_node(USER, uuid.UUID(out["result"])))
    assert theme_in(result.body)["key"] == theme["key"] and "**Du:** dunkler" in result.body
    assert result.feedback == "dunkler" and result.iteration == 1
    start = client.get(f"/api/ui/workshop/start?base={out['result']}", headers=AUTH).json()
    assert start["theme"]["key"] == theme["key"]


def test_a_further_patch_builds_on_the_first_one(client: TestClient, connect: Any) -> None:
    """Live 2026-11-17: the correction was a patch to the old draft and the zoom of the first
    patch was gone. Now the first patch stays and the second one is laid on top."""
    first = {
        "reply": "Schwarz, Zoom.",
        "set": {"motion": {"style": "zoom"}},
        "vars": {"--bg": "#000"},
        "rules": [{"selector": ".dt-titlebar", "css": "background: #ddd;"}],
    }
    second = {
        "reply": "Titelleiste dunkel.",
        "rules": [{"selector": ".dt-titlebar", "css": "background: var(--surface);"}],
    }
    asked = _model(client, connect, [json.dumps(first), json.dumps(second)])
    out = client.post(
        "/api/ui/workshop/turn", json={"theme": DRAFT, "message": "schwarz"}, headers=AUTH
    ).json()
    assert out["calls"] == 2 and out["theme"]["motion"]["style"] == "zoom"
    assert (
        "background: var(--surface);" in out["theme"]["css"] and "#ddd" not in out["theme"]["css"]
    )
    assert ".dt-titlebar (#ddd)" in asked[1]["messages"][-1]["content"]


def test_contrast_is_fixed_without_a_model(client: TestClient) -> None:
    theme = {
        **DRAFT,
        "css": "--bg: #000;\n.dt-stage { background: #f8f8f8; }\n"
        ".dt-program-body { background: #fff; color: #333; }\n.dt-close { background: #f55; }",
    }
    out = client.post("/api/ui/workshop/fix-contrast", json={"theme": theme}, headers=AUTH).json()
    css = out["theme"]["css"]
    assert ".dt-stage {\n  background: var(--bg);" in css
    assert "background: var(--surface);" in css and "color: var(--text);" in css
    assert "#f55" in css and not any("light background" in f for f in out["lint"])
