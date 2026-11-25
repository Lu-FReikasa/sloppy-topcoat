"""The incognito folder (docs/PLATFORM.md, section 3.9): context-free, web only, never written."""

import base64
import json
import logging
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text

from tests.conftest import AUTH, TEST_DB_URL
from tests.fakenode import FakeNode, online_node
from tests.test_chat import use_model
from tests.test_memory_applications import seed
from tests.test_web_access import web_node  # noqa: F401  (a fixture)
from tests.users import make_user
from topcoat_hub import incognito

SECRET = "XYZZY-nobody-may-keep-this-question"


def requests_to_the_model(node: FakeNode) -> list[dict[str, Any]]:
    bodies = [json.loads(base64.b64decode(r["body"]["body"])) for r in node.proxy_requests]
    return [b for b in bodies if b.get("messages") and "incognito" not in str(b.get("model"))]


def new_tab(client: TestClient, who: dict[str, str] = AUTH, **body: Any) -> str:
    r = client.post("/api/incognito", headers=who, json=body)
    assert r.status_code == 201, r.text
    return r.json()["id"]


def ask(client: TestClient, tab: str, content: str, who: dict[str, str] = AUTH) -> Any:
    return client.post(f"/api/incognito/{tab}/messages", headers=who, json={"content": content})


def row_counts() -> dict[str, int]:
    engine = create_engine(TEST_DB_URL)
    with engine.connect() as conn:
        names = [
            r[0]
            for r in conn.execute(
                text(
                    "SELECT table_name FROM information_schema.tables WHERE table_schema = 'public'"
                )
            )
        ]
        counts = {
            n: conn.execute(text(f'SELECT count(*) FROM "{n}"')).scalar_one()  # noqa: S608
            for n in names
        }
    engine.dispose()
    return counts


@pytest.fixture
def model_node(client: TestClient, connect: Any) -> FakeNode:
    node = online_node(client, connect, "node-a", loaded=["small"])
    use_model(client)
    return node


def search_then_answer(query: str) -> Any:
    """A model that asks for one web search and answers once it has the result."""

    def answer(req: dict[str, Any]) -> str | dict[str, Any] | None:
        if not req.get("messages"):
            return None
        if any(m.get("role") == "tool" for m in req["messages"]):
            return "Laut Suche: Berlin."
        if not req.get("tools"):
            return None
        return {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": "c1",
                    "type": "function",
                    "function": {"name": "web_search", "arguments": json.dumps({"query": query})},
                }
            ],
        }

    return answer


def test_a_tab_is_made_listed_read_and_deleted(client: TestClient) -> None:
    tab = new_tab(client)
    assert client.get("/api/incognito", headers=AUTH).json()["tabs"][0]["id"] == tab
    got = client.get(f"/api/incognito/{tab}", headers=AUTH).json()
    assert got["messages"] == [] and got["internet"] is True
    assert (
        client.patch(f"/api/incognito/{tab}", headers=AUTH, json={"internet": False}).json()[
            "internet"
        ]
        is False
    )
    assert client.delete(f"/api/incognito/{tab}", headers=AUTH).status_code == 204
    assert client.get(f"/api/incognito/{tab}", headers=AUTH).status_code == 404
    assert client.delete(f"/api/incognito/{tab}", headers=AUTH).status_code == 404


def test_there_is_a_limit_to_the_tabs(client: TestClient) -> None:
    for _ in range(incognito.MAX_TABS):
        new_tab(client)
    r = client.post("/api/incognito", headers=AUTH, json={})
    assert r.status_code == 409 and r.json()["detail"]["code"] == "too_many_tabs"
    assert client.delete("/api/incognito", headers=AUTH).status_code == 204
    assert client.get("/api/incognito", headers=AUTH).json()["tabs"] == []


def test_nobody_else_sees_or_touches_a_tab(client: TestClient, clean_db: str) -> None:
    _, other = make_user(clean_db, "user-b")
    tab = new_tab(client)
    assert client.get("/api/incognito", headers=other).json()["tabs"] == []
    for call in (
        client.get(f"/api/incognito/{tab}", headers=other),
        client.patch(f"/api/incognito/{tab}", headers=other, json={"internet": False}),
        ask(client, tab, "hi", other),
        client.delete(f"/api/incognito/{tab}", headers=other),
    ):
        assert call.status_code == 404
    assert len(client.get("/api/incognito", headers=AUTH).json()["tabs"]) == 1


def test_the_model_knows_nothing_of_the_person_but_the_tab(
    client: TestClient, model_node: FakeNode
) -> None:
    seed("Wohnt in Hamburg", kind="person")  # a note the model must never see here
    tab = new_tab(client)
    first = ask(client, tab, "Wie heißt die Hauptstadt von Frankreich?")
    assert first.status_code == 200, first.text
    assert first.json()["message"]["content"].startswith("Antwort von")
    second = ask(client, tab, "Und die von Spanien?")
    assert second.status_code == 200
    sent = requests_to_the_model(model_node)
    assert sent[0]["messages"][0]["role"] == "system"
    assert "private conversation" in sent[0]["messages"][0]["content"]
    assert [m["role"] for m in sent[0]["messages"]] == ["system", "user"]
    assert [m["role"] for m in sent[1]["messages"]] == ["system", "user", "assistant", "user"]
    assert not sent[0].get("tools")  # no tool node offers the web here
    assert "Hamburg" not in json.dumps(sent)
    got = client.get(f"/api/incognito/{tab}", headers=AUTH).json()
    assert [m["role"] for m in got["messages"]] == ["user", "assistant", "user", "assistant"]
    assert got["title"].startswith("Wie heißt die Hauptstadt")


def test_it_may_search_the_web_and_the_search_leaves_no_trace(
    client: TestClient,
    model_node: FakeNode,
    web_node: tuple[str, FakeNode],  # noqa: F811
) -> None:
    _, tools = web_node
    model_node.answer_fn = search_then_answer("capital of germany")
    tab = new_tab(client)
    before = row_counts()
    r = ask(client, tab, "Hauptstadt von Deutschland?")
    after = row_counts()
    assert r.status_code == 200, r.text
    assert r.json()["message"]["content"] == "Laut Suche: Berlin."
    assert [c["capability"] for c in tools.capability_calls] == ["web.search"]
    assert tools.capability_calls[0]["input"] == {"query": "capital of germany"}
    assert after == before, {k: (before[k], after[k]) for k in before if before[k] != after[k]}


def test_a_search_with_private_data_is_refused_and_still_leaves_no_trace(
    client: TestClient,
    model_node: FakeNode,
    web_node: tuple[str, FakeNode],  # noqa: F811
) -> None:
    _, tools = web_node
    model_node.answer_fn = search_then_answer("login with password=hunter2hunter2")
    tab = new_tab(client)
    before = row_counts()
    r = ask(client, tab, "Kannst du das nachschlagen?")
    assert r.status_code == 200, r.text
    assert tools.capability_calls == []  # it never left the hub
    asked = requests_to_the_model(model_node)[-1]["messages"]
    assert any(m.get("role") == "tool" and "refused" in m["content"] for m in asked)
    assert row_counts() == before


def test_with_the_internet_off_the_web_is_not_even_offered(
    client: TestClient,
    model_node: FakeNode,
    web_node: tuple[str, FakeNode],  # noqa: F811
) -> None:
    _, tools = web_node
    tab = new_tab(client, internet=False)
    assert ask(client, tab, "Hallo").status_code == 200
    assert not requests_to_the_model(model_node)[-1].get("tools")
    assert tools.capability_calls == []


def test_a_whole_turn_writes_no_row_and_no_log_line_with_the_question(
    client: TestClient,
    model_node: FakeNode,
    web_node: tuple[str, FakeNode],  # noqa: F811
    caplog: pytest.LogCaptureFixture,
) -> None:
    model_node.answer_fn = search_then_answer(f"public facts about {SECRET}")
    tab = new_tab(client)
    caplog.set_level(logging.DEBUG)
    before = row_counts()
    r = ask(client, tab, f"Frage mit {SECRET}")
    assert r.status_code == 200, r.text
    assert row_counts() == before, "an incognito turn must leave no row in any table"
    assert SECRET not in caplog.text
    assert all(SECRET not in str(rec.args) for rec in caplog.records)


def test_an_empty_message_and_a_full_tab_are_refused(
    client: TestClient, model_node: FakeNode, monkeypatch: pytest.MonkeyPatch
) -> None:
    tab = new_tab(client)
    assert (
        client.post(
            f"/api/incognito/{tab}/messages", headers=AUTH, json={"content": ""}
        ).status_code
        == 422
    )
    monkeypatch.setattr(incognito, "MAX_MESSAGES", 2)
    assert ask(client, tab, "eins").status_code == 200
    r = ask(client, tab, "zwei")
    assert r.status_code == 409 and r.json()["detail"]["code"] == "tab_full"


def test_a_model_that_cannot_answer_is_an_error_not_a_stored_message(client: TestClient) -> None:
    tab = new_tab(client)  # no model node is online
    r = ask(client, tab, "Hallo")
    assert r.status_code == 502
    assert client.get(f"/api/incognito/{tab}", headers=AUTH).json()["messages"] == []
