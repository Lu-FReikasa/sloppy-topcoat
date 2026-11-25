"""Pictures in a report (2026-11-13): a research run places a picture through the hub, which fetches
it with the web node's web.image, keeps it and answers with a Markdown line; the model never gets
the bytes, the reader's browser never asks the picture's server, and only its person sees it."""

import base64
import uuid
from types import SimpleNamespace
from typing import Any

from fastapi.testclient import TestClient

from tests.conftest import AUTH, TEST_DB_URL
from tests.users import make_user
from topcoat_hub import tool_groups
from topcoat_hub.message_tools import IMAGE, MAX_IMAGES, MessageTools
from topcoat_hub.models import DEV_ADMIN_ID, AgentTask
from topcoat_hub.tools import Caller, ToolError

PNG = b"\x89PNG\r\n\x1a\nfake-png-bytes"
NODE = uuid.uuid4()


class FakeTools:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def available(self, caller: Caller) -> list[dict[str, Any]]:
        return [
            {"node_id": str(NODE), "node": "node-web", "capability": c}
            for c in ("web.search", "web.fetch", "web.images", "web.image")
        ]

    async def invoke(self, caller: Caller, node: uuid.UUID, capability: str, inp: dict) -> dict:
        self.calls.append((capability, inp))
        if "big" in inp["url"]:
            return {"ok": False, "error": {"message": "the picture is larger than 180 KB"}}
        data = base64.b64encode(PNG).decode()
        return {
            "ok": True,
            "output": {"url": inp["url"], "content_type": "image/png", "data": data},
        }


def _run(client: TestClient) -> tuple[MessageTools, FakeTools, Caller]:
    task_id = uuid.uuid4()

    async def make() -> None:
        async with client.app.state.sessionmaker() as db:
            db.add(
                AgentTask(
                    id=task_id,
                    user_id=DEV_ADMIN_ID,
                    prompt="p",
                    title="t",
                    application="research",
                    status="running",
                )
            )
            await db.commit()

    client.portal.call(make)
    tools = FakeTools()
    messages = MessageTools(client.app.state.sessionmaker)
    messages.agents = SimpleNamespace(tools=tools)
    caller = Caller(DEV_ADMIN_ID, True, "agent", application="research", task_id=task_id)
    return messages, tools, caller


def test_a_picture_is_kept_by_the_hub_and_shown_only_to_its_person(client: TestClient) -> None:
    messages, tools, caller = _run(client)
    offered = client.portal.call(messages.offer, caller)
    assert IMAGE in [t["capability"] for t in offered]
    out = client.portal.call(
        messages.run_tool,
        caller,
        IMAGE,
        {
            "image_url": "https://cdn.example.org/walk.png",
            "page_url": "https://example.org/post",
            "caption": "Eine Laufanimation in Pixel-Art",
        },
    )["output"]
    assert "data" not in out and "fake-png" not in str(out)  # no bytes for the model
    assert out["markdown"].startswith("![Eine Laufanimation in Pixel-Art](/api/reports/images/")
    assert tools.calls == [("web.image", {"url": "https://cdn.example.org/walk.png"})]
    path = out["markdown"].split("(", 1)[1].rstrip(")")
    r = client.get(path, headers=AUTH)
    assert r.status_code == 200 and r.content == PNG
    assert (
        r.headers["content-type"] == "image/png"
        and r.headers["x-content-type-options"] == "nosniff"
    )
    _, other = make_user(TEST_DB_URL, "lee")
    assert client.get(path, headers=other).status_code == 404


def test_a_refused_picture_and_the_limit_go_back_to_the_model(client: TestClient) -> None:
    messages, _, caller = _run(client)
    given = {"page_url": "https://example.org/p", "caption": "Ein Bild davon"}
    try:
        client.portal.call(
            messages.run_tool, caller, IMAGE, {**given, "image_url": "https://example.org/big.png"}
        )
        raise AssertionError("a refused picture was kept")
    except ToolError as exc:
        assert "180 KB" in exc.message
    for n in range(MAX_IMAGES):
        client.portal.call(
            messages.run_tool, caller, IMAGE, {**given, "image_url": f"https://example.org/{n}.png"}
        )
    try:
        client.portal.call(
            messages.run_tool, caller, IMAGE, {**given, "image_url": "https://example.org/x.png"}
        )
        raise AssertionError("more pictures than allowed")
    except ToolError as exc:
        assert exc.code == "enough_images"


def test_the_picture_fetch_is_never_offered_to_a_model() -> None:
    available = [
        {"node_id": "n", "node": "node-web", "capability": c}
        for c in ("web.search", "web.fetch", "web.images", "web.image")
    ]
    offered = [t["capability"] for t in tool_groups.apply_scope(available, None)]
    assert "web.images" in offered and "web.image" not in offered
