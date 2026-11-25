"""The tools of an application are a ceiling (app_scope.py): a conversation may switch tools off,
never on beyond what its application allows; the picker offers only those."""

from typing import Any

from fastapi.testclient import TestClient

from tests.conftest import AUTH
from tests.test_tools import tools_node
from topcoat_hub import app_scope, tool_groups
from topcoat_hub.auth import CurrentUser
from topcoat_hub.models import DEV_ADMIN_ID
from topcoat_hub.tools import Caller

ADMIN = CurrentUser(id=str(DEV_ADMIN_ID), name="admin", role="admin")


def settled(groups: list[str] | None = None, internet: bool = True, nodes=None) -> dict[str, Any]:
    return {"nodes": nodes, "groups": groups, "internet": internet}


def test_a_scope_is_narrowed_to_the_ceiling_and_nothing_comes_back() -> None:
    limit = {"nodes": None, "groups": ["notes"], "internet": True}
    assert app_scope.cap(settled(None), limit)["groups"] == ["notes"]
    assert app_scope.cap(settled(["docker", "notes"]), limit)["groups"] == ["notes"]
    assert app_scope.cap(settled(["docker"]), limit)["groups"] == []
    # a switch the conversation turned off stays off
    assert app_scope.cap(settled(None, internet=False), limit)["internet"] is False
    assert app_scope.cap(settled(None), {**limit, "internet": False})["internet"] is False
    # no ceiling: the scope as it was
    free = {"nodes": None, "groups": None, "internet": None}
    assert app_scope.cap(settled(["docker"]), free) == settled(["docker"])


def test_nodes_are_narrowed_too() -> None:
    limit = {"nodes": ["vm-a"], "groups": None, "internet": None}
    assert app_scope.cap(settled(None, nodes=["vm-a", "vm-b"]), limit)["nodes"] == ["vm-a"]
    assert app_scope.cap(settled(None), limit)["nodes"] == ["vm-a"]


def ceiling(client: TestClient, application: str | None) -> dict[str, Any]:
    async def read() -> dict[str, Any]:
        async with client.app.state.sessionmaker() as db:
            return await app_scope.ceiling(db, ADMIN, application)

    return client.portal.call(read)


def test_the_built_in_applications_keep_to_what_they_need(client: TestClient) -> None:
    chat = ceiling(client, "chat")
    assert chat["groups"] == ["notes", "wiki"] and chat["internet"] is True
    assert ceiling(client, "research")["groups"] == ["notes", "wiki"]
    dev = ceiling(client, "dev")
    assert sorted(dev["groups"]) == ["github", "notes", "trace", "wiki", "workshop"]
    assert dev["internet"] is False
    admin = ceiling(client, "admin")
    assert "docker" in admin["groups"] and "workshop" not in admin["groups"]
    # an application that does not exist allows nothing; no application: only the grants
    assert ceiling(client, "nowhere") == app_scope.NOTHING
    assert ceiling(client, None)["groups"] is None


def test_the_picker_offers_only_what_the_application_allows(client: TestClient, connect) -> None:
    tools_node(client, connect, "vm-docker")
    everything = client.get("/api/chat/tools", headers=AUTH).json()
    assert "docker" in [g["key"] for g in everything["groups"]]
    chat = client.get("/api/chat/tools?application=chat", headers=AUTH).json()
    assert "docker" not in [g["key"] for g in chat["groups"]]
    assert all("docker" not in n["groups"] for n in chat["nodes"])
    admin = client.get("/api/chat/tools?application=admin", headers=AUTH).json()
    assert "docker" in [g["key"] for g in admin["groups"]]
    dev = client.get("/api/chat/tools?application=dev", headers=AUTH).json()
    assert dev["internet"]["node"] is None  # Development has no web


def test_a_conversation_of_chat_cannot_reach_the_servers(client: TestClient, connect) -> None:
    """The ceiling holds whatever scope the conversation stores: asked about containers, Chat has
    no docker tool to offer the model."""
    tools_node(client, connect, "vm-docker")
    conv = client.post("/api/conversations", headers=AUTH, json={"application": "chat"}).json()
    client.patch(
        f"/api/conversations/{conv['id']}",
        headers=AUTH,
        json={"tool_scope": {"nodes": None, "groups": ["docker"], "internet": True}},
    )

    async def turn_scope() -> dict[str, Any]:
        async with client.app.state.sessionmaker() as db:
            limit = await app_scope.ceiling(db, ADMIN, "chat")
        stored = {"nodes": None, "groups": ["docker"], "internet": True}
        return app_scope.cap(tool_groups.resolve_scope(stored, True), limit)

    scope = client.portal.call(turn_scope)
    assert scope["groups"] == []
    available = client.portal.call(
        client.app.state.agents.tools.available,
        Caller.of(ADMIN, "agent"),
    )
    left = tool_groups.apply_scope(available, scope)
    assert not [t for t in left if tool_groups.group_of(t["capability"]) == "docker"]


def test_a_conversation_without_an_application_belongs_to_chat_unless_a_key_made_it(
    client: TestClient,
) -> None:
    """A browser session that names no workspace gets Chat's ceiling; a program with an API key
    that names none keeps no ceiling beyond the person's grants (D20)."""
    made = client.post("/api/conversations", headers=AUTH, json={"title": "no app"})
    assert made.status_code == 200 or made.status_code == 201, made.text
    assert made.json()["application"] == "chat"
    named = client.post("/api/conversations", headers=AUTH, json={"application": "dev"})
    assert named.json()["application"] == "dev"

    key = client.post("/api/keys", headers=AUTH, json={"name": "script"}).json()["key"]
    bearer = {"Authorization": f"Bearer {key}"}
    by_key = client.post("/api/conversations", headers=bearer, json={"title": "from a script"})
    assert by_key.status_code in (200, 201), by_key.text
    assert by_key.json()["application"] is None
