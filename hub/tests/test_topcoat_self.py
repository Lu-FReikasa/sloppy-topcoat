"""Topcoat as a read-only tool of the assistant: the guide, the user's own chats and settings, the
admin-only configuration. The rules under test: nothing leaves the user's own data, secrets are
blackened, the switches work, and the tool never writes."""

import asyncio
import pathlib
import uuid
from typing import Any

import pytest
from fastapi.testclient import TestClient

from tests.conftest import AUTH
from tests.users import make_user
from topcoat_hub import chat_tools, tool_groups, topcoat_self
from topcoat_hub.chat_store import ChatStore
from topcoat_hub.config import Settings
from topcoat_hub.models import DEV_ADMIN_ID
from topcoat_hub.tools import Caller, ToolError

# --- no database -----------------------------------------------------------------------------


def test_every_guide_topic_has_a_menu_place_and_text() -> None:
    assert topcoat_self.FEATURES
    for key, entry in topcoat_self.FEATURES.items():
        assert entry["title"] and entry["where"] and len(entry["text"]) > 60, key
    listing = topcoat_self.guide("")
    assert {t["topic"] for t in listing["topics"]} == set(topcoat_self.FEATURES)
    assert topcoat_self.guide("Chat")["topic"] == "chat"
    with pytest.raises(ToolError):
        topcoat_self.guide("nonsense")


def test_the_guide_says_how_a_node_is_renamed_and_knows_no_flows() -> None:
    # live 2026-11-09: the question "can I rename a node?" had no answer in any source, and the
    # run asked the guide again and again until it was stopped
    names = topcoat_self.guide("names")
    assert "Rename" in names["where"] and "old name still reaches" in names["text"]
    assert "flows" not in topcoat_self.FEATURES  # the flow editor was removed (r7)
    assert "Nodes" in topcoat_self.guide("machines")["where"]


def test_secrets_in_text_are_blackened() -> None:
    for text in (
        "my password: hunter2 ok",
        "token=abc123def",
        "Authorization: Bearer abcdefghij",
        "key sk-abcdefghijklmnopqrstuv here",
        "-----BEGIN PRIVATE KEY-----\nMIIE\n-----END PRIVATE KEY-----",
    ):
        out = topcoat_self.scrub(text)
        assert topcoat_self.HIDDEN in out, text
        assert "hunter2" not in out and "abc123def" not in out and "MIIE" not in out
    assert topcoat_self.scrub("nothing to hide here") == "nothing to hide here"


def test_the_topcoat_group_is_a_selectable_group_with_its_own_capabilities() -> None:
    assert "topcoat" in tool_groups.SELECTABLE
    for tool in (topcoat_self.guide_tool(), topcoat_self.state_tool()):
        assert tool_groups.group_of(tool["capability"]) == "topcoat"
    avail = [topcoat_self.guide_tool(), topcoat_self.state_tool()]
    assert tool_groups.apply_scope(avail, {"groups": ["docker"]}) == []
    assert len(tool_groups.apply_scope(avail, None)) == 2


@pytest.mark.parametrize(
    "text",
    [
        "Wie funktioniert Topcoat eigentlich?",
        "Was kann Topcoat alles?",
        "Welche Funktionen hat diese App?",
        "Wo finde ich meine Einstellungen?",
        "Welche Werkzeuge habe ich in meinen Chats an?",
        "Where do I find the settings in this app?",
    ],
)
def test_questions_about_topcoat_itself_start_a_look_when_the_tool_is_on(text: str) -> None:
    assert chat_tools.check_need(text, set(), self_tool=True) == "check"
    assert chat_tools.check_need(text, set(), self_tool=False) != "check"


@pytest.mark.parametrize(
    "text",
    [
        "Schreibe ein Gedicht über den Herbst",
        "Was ist die Hauptstadt von Peru?",
        "Erkläre Kubernetes",
    ],
)
def test_other_questions_are_not_taken_for_questions_about_topcoat(text: str) -> None:
    assert chat_tools.check_need(text, set(), self_tool=True) == ""


# --- with the hub ----------------------------------------------------------------------------


@pytest.fixture
def settings(settings: Settings) -> Settings:
    return settings.model_copy(update={"self_tool_enabled": True})


def run(client: TestClient, caller: Caller, capability: str, **tool_input: Any) -> Any:
    out = client.portal.call(client.app.state.topcoat_self.run_tool, caller, capability, tool_input)
    assert out["ok"] is True
    return out["output"]


def state(client: TestClient, caller: Caller, **tool_input: Any) -> Any:
    return run(client, caller, topcoat_self.STATE_CAPABILITY, **tool_input)


def add_chat(client: TestClient, headers: dict[str, str], title: str, text: str) -> str:
    r = client.post("/api/conversations", headers=headers, json={"title": title})
    assert r.status_code == 201, r.text
    conv = uuid.UUID(r.json()["id"])
    user = client.get("/api/me", headers=headers).json()["id"]

    async def put() -> None:
        async with client.app.state.sessionmaker() as db:
            await ChatStore(db, uuid.UUID(user)).add_message(conv, "user", text)
            await db.commit()

    client.portal.call(put)
    return str(conv)


def admin() -> Caller:
    return Caller(DEV_ADMIN_ID, True, "agent")


def test_the_tools_are_offered_by_the_service(client: TestClient) -> None:
    offered = client.portal.call(client.app.state.topcoat_self.offer, admin())
    # next to the two of Topcoat itself, the two of the development program (trace_tool.py)
    assert [t["capability"] for t in offered] == [
        "topcoat.guide",
        "topcoat.state",
        "trace.read",
        "repo.show",
    ]
    assert {t["node"] for t in offered} == {"topcoat"}


def test_the_hub_can_switch_the_tool_off(client: TestClient) -> None:
    svc = client.app.state.topcoat_self
    svc.settings = svc.settings.model_copy(update={"self_tool_enabled": False})
    assert client.portal.call(svc.offer, admin()) == []
    with pytest.raises(ToolError) as err:
        client.portal.call(svc.run_tool, admin(), "topcoat.guide", {})
    assert err.value.code == "disabled"


def test_a_user_reads_only_their_own_chats_and_an_admin_is_no_exception(
    client: TestClient, migrated_db: str
) -> None:
    bob_id, bob = make_user(migrated_db, "bob")
    mine = add_chat(client, AUTH, "Admin chat", "hello from the admin")
    theirs = add_chat(client, bob, "Bob chat", "hello from bob")
    seen = state(client, admin(), what="chats")["chats"]
    assert [c["title"] for c in seen] == ["Admin chat"]
    assert [
        c["title"] for c in state(client, Caller(bob_id, False, "agent"), what="chats")["chats"]
    ] == ["Bob chat"]
    assert (
        state(client, admin(), what="chat", id=mine)["messages"][0]["text"]
        == "hello from the admin"
    )
    with pytest.raises(
        ToolError
    ) as err:  # somebody else's chat looks like a chat that does not exist
        client.portal.call(
            client.app.state.topcoat_self.run_tool,
            admin(),
            "topcoat.state",
            {"what": "chat", "id": theirs},
        )
    assert err.value.code == "not_found"


def test_a_secret_in_a_chat_is_not_handed_to_the_assistant(client: TestClient) -> None:
    conv = add_chat(client, AUTH, "NAS", "the NAS password: hunter2 and the rest")
    text = state(client, admin(), what="chat", id=conv)["messages"][0]["text"]
    assert "hunter2" not in text and topcoat_self.HIDDEN in text


def test_the_config_is_for_admins_and_has_no_secrets(client: TestClient, migrated_db: str) -> None:
    bob_id, _ = make_user(migrated_db, "bob")
    with pytest.raises(ToolError) as err:
        client.portal.call(
            client.app.state.topcoat_self.run_tool,
            Caller(bob_id, False, "agent"),
            "topcoat.state",
            {"what": "config"},
        )
    assert err.value.code == "forbidden"
    cfg = state(client, admin(), what="config")["config"]
    assert "agent_max_turns" in cfg
    assert not {"database_url", "dev_admin_token", "tls_key_file", "public_url"} & set(cfg)
    assert "test-dev-token" not in str(cfg)


def test_settings_and_overview_are_the_callers_own(client: TestClient, migrated_db: str) -> None:
    bob_id, _ = make_user(migrated_db, "bob")
    bob = Caller(bob_id, False, "agent")
    assert state(client, bob, what="settings")["role"] == "user"
    assert state(client, admin(), what="settings")["role"] == "admin"
    ov = state(client, bob, what="overview")
    assert ov["you_are_admin"] is False and ov["chats"] == 0
    assert state(client, bob, what="nodes")["connected_servers_with_tools_you_may_run"] == []


def test_unknown_input_and_unknown_what_are_refused(client: TestClient) -> None:
    svc = client.app.state.topcoat_self
    for cap, bad in (
        ("topcoat.state", {"what": "chats", "sql": "x"}),
        ("topcoat.state", {"what": "everything"}),
        ("topcoat.state", {"what": "chat", "id": "not-a-uuid"}),
        ("topcoat.guide", {"topic": "nonsense"}),
    ):
        with pytest.raises(ToolError):
            client.portal.call(svc.run_tool, admin(), cap, bad)


def test_memory_tasks_and_knowledge_are_the_callers_own(
    client: TestClient, migrated_db: str
) -> None:
    from sqlalchemy import create_engine, text

    bob_id, _ = make_user(migrated_db, "bob")
    engine = create_engine(migrated_db)
    with engine.begin() as conn:
        for uid, note in (
            (str(DEV_ADMIN_ID), "admin likes tea"),
            (str(bob_id), "bob likes coffee"),
        ):
            conn.execute(
                text(
                    "INSERT INTO memory_items (id, user_id, text, status) "
                    "VALUES (gen_random_uuid(), :u, :t, 'active')"
                ),
                {"u": uid, "t": note},
            )
        conn.execute(
            text(
                "INSERT INTO agent_tasks (id, user_id, prompt, title, status, origin, via, kind) "
                "VALUES (gen_random_uuid(), :u, 'check x password: hunter2', 'bob task', "
                "'done', 'tasks', 'gui', 'single')"
            ),
            {"u": str(bob_id)},
        )
        conn.execute(
            text(
                "INSERT INTO knowledge_sources (id, user_id, name, visibility, node_name, root, "
                "status, kind, file_count, chunk_count, excluded_count, blocked_count) "
                "VALUES (gen_random_uuid(), :u, :n, :v, 'tools-a', '/srv/secret/path', "
                "'ready', 'node', 0, 0, 0, 0)"
            ),
            {"u": str(bob_id), "n": "bobs-private", "v": "private"},
        )
        conn.execute(
            text(
                "INSERT INTO knowledge_sources (id, user_id, name, visibility, node_name, root, "
                "status, kind, file_count, chunk_count, excluded_count, blocked_count) "
                "VALUES (gen_random_uuid(), :u, 'lab-notes', 'shared', 'tools-a', '/srv/notes', "
                "'ready', 'node', 0, 0, 0, 0)"
            ),
            {"u": str(bob_id)},
        )
    engine.dispose()
    bob = Caller(bob_id, False, "agent")
    assert [n["text"] for n in state(client, admin(), what="memory")["notes"]] == [
        "admin likes tea"
    ]
    assert [n["text"] for n in state(client, bob, what="memory")["notes"]] == ["bob likes coffee"]
    assert state(client, admin(), what="tasks")["tasks"] == []
    mine = state(client, bob, what="tasks")["tasks"]
    assert [t["title"] for t in mine] == ["bob task"]
    detail = state(client, bob, what="task", id=mine[0]["id"])
    assert "hunter2" not in detail["question"]
    with pytest.raises(ToolError) as err:  # somebody else's task is not found, admin or not
        client.portal.call(
            client.app.state.topcoat_self.run_tool,
            admin(),
            "topcoat.state",
            {"what": "task", "id": mine[0]["id"]},
        )
    assert err.value.code == "not_found"
    names = [k["name"] for k in state(client, admin(), what="knowledge")["sources"]]
    assert names == ["lab-notes"]  # a shared source, not Bob's private one
    assert "/srv" not in str(state(client, bob, what="knowledge"))


def test_an_admin_sees_a_model_node_without_tools_and_is_told_it_works(
    client: TestClient, connect, migrated_db: str
) -> None:
    from tests.fakenode import online_node

    online_node(client, connect, "node-laptop", loaded=["small-model"])
    out = state(client, admin(), what="nodes")
    assert out["connected_servers_with_tools_you_may_run"] == []  # it offers no tools
    machine = next(m for m in out["all_machines"] if m["node"] == "node-laptop")
    assert machine["state"] == "available"
    assert machine["loaded"] == ["small-model"]
    assert "tools" not in machine  # it offers none
    # what the hub knows of the machine itself, for a question about its memory and load (AGT-03)
    assert {"cpu_percent", "ram_mb"} <= set(machine)
    full = state(client, admin(), what="nodes", id="node-laptop")["all_machines"]
    assert [m["node"] for m in full] == ["node-laptop"]  # one machine, in full
    assert [m["name"] for m in full[0]["loaded_models"]] == ["small-model"]
    assert full[0]["tools_you_may_run"] == 0
    assert {"cpu", "cpu_util_percent", "ram_total_mb", "ram_used_mb", "gpus", "loading"} <= set(
        full[0]
    )
    assert "last_seen" in full[0]
    assert "cannot be asked for logs or containers" in out["note"]
    # someone who is no admin does not get the list of the hub's machines
    bob_id, _ = make_user(migrated_db, "bob2")
    assert "all_machines" not in state(client, Caller(bob_id, False, "agent"), what="nodes")


def test_a_model_node_that_is_gone_shows_as_offline_not_as_working(
    client: TestClient, connect, migrated_db: str
) -> None:
    from tests.fakenode import online_node

    node = online_node(client, connect, "node-gone", loaded=["small-model"])
    node.close()
    client.portal.call(asyncio.sleep, 0.3)
    out = state(client, admin(), what="nodes")
    machine = next(m for m in out["all_machines"] if m["node"] == "node-gone")
    assert machine["state"] == "offline"


def test_the_agent_is_not_told_that_a_machine_without_a_tool_node_cannot_be_seen() -> None:
    from topcoat_hub import agent_prompts

    source = pathlib.Path(agent_prompts.__file__).read_text()
    assert "cannot be looked at at all" not in source
    assert source.count("ask topcoat.state with what=nodes") == 2


def test_all_machines_fit_into_a_tool_excerpt_and_an_unknown_machine_is_not_found(
    client: TestClient, connect, migrated_db: str
) -> None:
    """AGT-03 live: ten machines at once were cut in the excerpt, the agent asked again and then
    stated numbers it never read. The short rows stay under the chat's observation limit."""
    from tests.fakenode import online_node

    for i in range(10):
        online_node(client, connect, f"node-{i}", loaded=["a-model"])
    out = state(client, admin(), what="nodes")
    assert len(out["all_machines"]) == 10
    assert len(str(out)) < 3000
    assert all("last_seen" not in m for m in out["all_machines"] if m["state"] == "available")
    with pytest.raises(ToolError) as err:
        client.portal.call(
            client.app.state.topcoat_self.run_tool,
            admin(),
            "topcoat.state",
            {"what": "nodes", "id": "node-nope"},
        )
    assert err.value.code == "not_found"


def test_the_whole_guide_comes_in_one_read() -> None:
    whole = topcoat_self.guide("all")
    assert [t["topic"] for t in whole["topics"]] == list(topcoat_self.FEATURES)
    assert all(t["text"] and t["where"] for t in whole["topics"])


def test_every_machine_says_whether_its_kind_can_be_switched_in_the_gui(
    client: TestClient, connect, migrated_db: str
) -> None:
    """#222: asked whether a node's compute role can change after the install, the assistant
    guessed "remove and re-add the node". The node page has the switch; topcoat.state says so."""
    from sqlalchemy import select, update

    from tests.fakenode import online_node
    from tests.test_tools import tools_node
    from topcoat_hub.models import Node

    online_node(client, connect, "node-gpu", loaded=["a-model"])
    tools_node(client, connect, "vm-tools")
    online_node(client, connect, "node-old")

    async def without_roles() -> None:  # a config the switch cannot work on (no roles list)
        async with client.app.state.sessionmaker() as db:
            node = (await db.execute(select(Node).where(Node.name == "node-old"))).scalar_one()
            config = {k: v for k, v in node.config.items() if k != "roles"}
            await db.execute(update(Node).where(Node.id == node.id).values(config=config))
            await db.commit()

    client.portal.call(without_roles)
    out = state(client, admin(), what="nodes")
    # the short rows name only the exception; the full row says it either way
    kinds = {m["node"]: (m["kind"], m.get("kind_switchable", True)) for m in out["all_machines"]}
    assert kinds == {
        "node-gpu": ("compute", True),
        "vm-tools": ("tools", True),
        "node-old": ("tools", False),
    }
    assert "no reinstall or re-adding" in out["note"]
    assert "kind_switchable" not in next(m for m in out["all_machines"] if m["node"] == "node-gpu")
    full = state(client, admin(), what="nodes", id="node-gpu")["all_machines"][0]
    assert full["kind_switchable"] is True and full["kind"] == "compute"
    assert full["kind_switch"] == topcoat_self.KIND_SWITCH_WHERE
    assert "Rights & capabilities" in full["kind_switch"]
    assert (
        state(client, admin(), what="nodes", id="node-old")["all_machines"][0]["kind_switch"]
        is None
    )
    # someone who is no admin is told who changes it, not handed the machines
    bob_id, _ = make_user(migrated_db, "bob3")
    plain = state(client, Caller(bob_id, False, "agent"), what="nodes")
    assert "all_machines" not in plain and "changed by an admin" in plain["note"]
    # the guide says the same: a switch, not a choice at install time
    assert "kind_switchable" in topcoat_self.guide("machines")["text"]
