"""The frame of a program (ARCHITECTURE D74): the tone and the rules of its assistant stand in the
core block of every conversation of that program, after the system rules and before the person's
style and voice; another program's conversation gets another frame."""

from typing import Any

from fastapi.testclient import TestClient

from tests.conftest import AUTH
from topcoat_hub import core_rules
from topcoat_hub.models import DEV_ADMIN_ID
from topcoat_hub.ui.access import clean_agent, clean_frame, frame_text
from topcoat_hub.ui.seeds import FRAMES


def core_text(client: TestClient, conv: str) -> str:
    import uuid

    async def run() -> str:
        result = await client.app.state.core.block(DEV_ADMIN_ID, uuid.UUID(conv), None)
        return result.text

    return client.portal.call(run)


def conv(client: TestClient, application: str) -> str:
    r = client.post("/api/conversations", headers=AUTH, json={"application": application})
    assert r.status_code == 201, r.text
    return r.json()["id"]


def test_a_frame_is_cleaned_like_everything_an_admin_writes() -> None:
    assert clean_frame(None) is None and clean_frame({"tone": " ", "rules": []}) is None
    frame = clean_frame({"tone": "  Calm\n and  short ", "rules": ["a", "", 3, "b" * 500], "x": 1})
    assert frame == {"tone": "Calm and short", "rules": ["a", "b" * 240]}
    assert clean_frame({"rules": [f"r{i}" for i in range(20)]})["rules"][-1] == "r7"  # type: ignore[index]
    assert frame_text(frame) == f"- Tone: Calm and short\n- a\n- {'b' * 240}"
    assert clean_agent({"frame": {"tone": "x"}})["frame"] == {"tone": "x", "rules": []}
    assert clean_agent({})["frame"] is None


def test_each_program_speaks_in_its_own_frame(client: TestClient) -> None:
    admin = core_text(client, conv(client, "admin"))
    chat = core_text(client, conv(client, "chat"))
    assert core_rules.HEADINGS["frame"] in admin and FRAMES["admin"]["tone"] in admin
    assert FRAMES["chat"]["tone"] in chat and FRAMES["admin"]["tone"] not in chat
    # the frame comes after the system rules and before the person's own style and rules
    order = [admin.index(core_rules.HEADINGS[h]) for h in ("system", "frame", "rules")]
    assert order == sorted(order)


def test_a_program_without_a_frame_has_none(client: TestClient) -> None:
    body: dict[str, Any] = {
        "title": "Quiet room",
        "icon": "Q",
        "position": 50,
        "modules": ["page.chat"],
        "agent": {"tool_scope": {"nodes": None, "groups": ["notes"], "internet": None}},
    }
    assert client.put("/api/ui/templates/quiet", headers=AUTH, json=body).status_code == 201
    text = core_text(client, conv(client, "quiet"))
    assert core_rules.HEADINGS["frame"] not in text
    # an admin gives it one: the next block of its conversation carries it
    body["agent"]["frame"] = {"tone": "Whisper.", "rules": ["Never more than two sentences."]}
    assert client.put("/api/ui/templates/quiet", headers=AUTH, json=body).status_code == 200
    again = core_text(client, conv(client, "quiet"))
    assert "- Tone: Whisper.\n- Never more than two sentences." in again


def test_the_frame_is_part_of_the_stamp() -> None:
    common: dict[str, Any] = {
        "budget": 900,
        "language": "de",
        "memory_enabled": True,
        "system": [],
        "rules": [],
        "pinned": [],
        "household": [],
        "recent": [],
    }
    assert core_rules.stamp(**common, frame="a") != core_rules.stamp(**common, frame="b")
