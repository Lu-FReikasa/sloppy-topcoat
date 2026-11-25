"""The development assistant reads how an answer was handled (trace.read) and points the person's
file reader somewhere (repo.show): structure only, handed over by the person with a reference."""

import json
import uuid
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text

from tests.conftest import AUTH
from tests.fakenode import online_node
from tests.test_chat import new_conv, use_model
from tests.test_memory import chat_once
from topcoat_hub import tool_groups, trace_tool
from topcoat_hub.secretbox import SecretBox
from topcoat_hub.tools import Caller, ToolError
from topcoat_hub.ui.seeds import BUILTIN_TEMPLATES

A_ID = uuid.UUID("00000000-0000-4000-8000-000000000001")
QUESTION = "Welcher Dienst läuft auf dem Rechner mit dem Namen Geheim-Host?"
ANSWER = "Der Dienst Plex läuft dort, Passwort hunter2."
PRIVATE_DETAIL = "Ergebnis mit privaten Angaben: 10.9.8.7 und hunter2"

ACTIVITY = [
    {
        "id": 1,
        "kind": "check",
        "status": "ok",
        "title": "",
        "detail": None,
        "data": {"calls": 2, "evidence": 1},
        "at": "2026-10-23T18:36:20.000+00:00",
        "ms": 1800,
    },
    {
        "id": 2,
        "kind": "tool",
        "status": "ok",
        "title": "docker.ps @ node-geheim",
        "detail": PRIVATE_DETAIL,
        "data": {"seq": 1, "input": {"command": "ps"}, "ref": "E1", "code": None},
        "at": "2026-10-23T18:36:20.500+00:00",
        "ms": 700,
    },
    {
        "id": 3,
        "kind": "check",
        "status": "ok",
        "title": "geheimes-modell",
        "detail": None,
        "data": {"second_look": True},
        "at": "2026-10-23T18:36:25.000+00:00",
        "ms": 900,
    },
]


# --- unit ------------------------------------------------------------------------------------


def test_a_signature_needs_the_key_and_binds_the_text(tmp_path) -> None:
    box = SecretBox(tmp_path)
    tag = box.sign("trace|a|b|1")
    assert len(tag) == 10 and tag == SecretBox(tmp_path).sign("trace|a|b|1")  # same key file
    assert tag != box.sign("trace|a|b|2")
    assert tag != SecretBox(tmp_path / "other").sign("trace|a|b|1")  # another hub, another tag


def test_paths_stay_inside_the_repository() -> None:
    assert trace_tool.clean_path("hub\\src//topcoat_hub/./api/chat.py") == (
        "hub/src/topcoat_hub/api/chat.py"
    )
    for bad in ("", "/", "../etc/passwd", "a/../../b", "C:/Windows", ".git/config", "a/.git/x"):
        with pytest.raises(ToolError):
            trace_tool.clean_path(bad)


def test_a_title_loses_the_machine_and_a_step_knows_where_it_lives() -> None:
    assert trace_tool.mask_title("docker.ps @ node-a") == "docker.ps"
    assert trace_tool.mask_title("repo.read@dev-box") == "repo.read"
    second = trace_tool.pointers_for({"kind": "check", "data": {"second_look": True}})
    assert second[0]["find"] == "async def _verify_answer"
    assert trace_tool.pointers_for({"kind": "no such kind"}) == []
    assert all(
        p["path"].startswith("hub/")
        for kind in ("tool", "flow")
        for p in trace_tool.pointers_for({"kind": kind})
    )


def test_the_trace_group_belongs_to_development_and_to_nobody_else() -> None:
    assert "trace" in tool_groups.KEYS and "trace" in tool_groups.HUB_VIEWS
    groups = {
        t["key"]: (t["agent"].get("tool_scope") or {}).get("groups") for t in BUILTIN_TEMPLATES
    }
    assert "trace" in groups["dev"]
    assert all("trace" not in (g or []) for key, g in groups.items() if key != "dev")


# --- with a hub ------------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def self_tool_on(request: pytest.FixtureRequest) -> None:
    """The tests run with Topcoat-as-a-tool off (conftest); these need it on."""
    if "client" in request.fixturenames:
        client = request.getfixturevalue("client")
        svc = client.app.state.topcoat_self
        svc.settings = svc.settings.model_copy(update={"self_tool_enabled": True})


@pytest.fixture
def answered(client: TestClient, connect):
    """A chat with one answer whose activity is the one above: (conv, question id, answer id)."""
    node = online_node(client, connect, "node-geheim", loaded=["geheimes-modell"])
    use_model(client, "geheimes-modell")
    node.stream_text_fn = lambda req: ANSWER
    conv = new_conv(client)
    chat_once(client, QUESTION, conv)
    messages = client.get(f"/api/conversations/{conv}", headers=AUTH).json()["messages"]
    user_msg = next(m for m in messages if m["role"] == "user")
    answer = next(m for m in messages if m["role"] == "assistant")
    engine = create_engine(client.app.state.settings.database_url)
    with engine.begin() as conn:
        conn.execute(
            text("UPDATE messages SET activity = CAST(:a AS jsonb) WHERE id = :i"),
            {"a": json.dumps(ACTIVITY), "i": answer["id"]},
        )
    engine.dispose()
    return conv, user_msg["id"], answer["id"]


def reference(client: TestClient, conv: str, message_id: int) -> str:
    r = client.post(f"/api/conversations/{conv}/messages/{message_id}/reference", headers=AUTH)
    assert r.status_code == 200, r.text
    return r.json()["reference"]


def run(client: TestClient, capability: str, tool_input: dict[str, Any]) -> dict[str, Any]:
    caller = Caller(A_ID, True, "agent")
    return client.portal.call(
        lambda: client.app.state.topcoat_self.run_tool(caller, capability, tool_input)
    )


def test_the_person_gets_a_reference_for_a_message_of_theirs(client, answered) -> None:
    conv, _question_id, answer_id = answered
    ref = reference(client, conv, answer_id)
    assert ref.startswith(f"trace:{conv[:8]}:{answer_id}:") and trace_tool.REF.match(ref)
    assert (
        client.post(
            f"/api/conversations/{uuid.uuid4()}/messages/{answer_id}/reference", headers=AUTH
        ).status_code
        == 404
    )
    assert (
        client.post(
            f"/api/conversations/{conv}/messages/999999/reference", headers=AUTH
        ).status_code
        == 404
    )


def test_the_trace_is_the_structure_and_never_what_was_said(client, answered) -> None:
    conv, _question_id, answer_id = answered
    out = run(client, "trace.read", {"reference": reference(client, conv, answer_id)})
    assert out["ok"] is True
    trace = out["output"]
    assert [s["kind"] for s in trace["steps"]] == ["check", "tool", "check"]
    assert [s["what"] for s in trace["steps"]] == ["", "docker.ps", ""]  # a model is no name
    tool = trace["steps"][1]
    assert tool["what"] == "docker.ps" and tool["ms"] == 700 and tool["status"] == "ok"
    assert tool["detail_chars"] == len(PRIVATE_DETAIL)
    assert tool["facts"] == {"seq": 1}  # not the input, not the reference of the evidence
    assert any(p["find"] == '"tools_start"' for p in tool["code"])
    assert trace["steps"][2]["code"][0]["find"] == "async def _verify_answer"  # the second look
    messages = client.get(f"/api/conversations/{conv}", headers=AUTH).json()["messages"]
    said = {"assistant": next(m["content"] for m in messages if m["id"] == answer_id)}
    assert trace["question_chars"] == len(QUESTION)
    assert trace["answer_chars"] == len(said["assistant"])
    assert trace["pipeline"][0]["find"] == "async def send_message"
    # nothing that was said, and no machine or model, in anything the assistant gets
    everything = json.dumps(trace, ensure_ascii=False)
    for secret in (
        QUESTION,
        said["assistant"],
        PRIVATE_DETAIL,
        "hunter2",
        "Geheim",
        "node-geheim",
        "10.9.8.7",
    ):
        assert secret not in everything, secret
    assert "geheimes-modell" not in json.dumps({k: v for k, v in trace.items() if k != "steps"})


def test_a_reference_to_a_question_reads_its_answer(client, answered) -> None:
    conv, question_id, _answer_id = answered
    out = run(client, "trace.read", {"reference": reference(client, conv, question_id)})
    assert len(out["output"]["steps"]) == 3


def test_nothing_is_read_without_a_reference_the_person_gave(client, answered) -> None:
    conv, question_id, answer_id = answered
    good = reference(client, conv, answer_id)
    forged = good[:-10] + ("0" * 10 if not good.endswith("0" * 10) else "1" * 10)
    with pytest.raises(ToolError) as bad:
        run(client, "trace.read", {"reference": forged})
    assert bad.value.status == 403 and bad.value.code == "not_handed_over"
    # another message of the same chat is not covered by the tag of this one
    other = good.replace(f":{answer_id}:", f":{question_id}:")
    with pytest.raises(ToolError) as bad:
        run(client, "trace.read", {"reference": other})
    assert bad.value.status == 403
    for junk in ("", "nonsense", f"trace:{uuid.uuid4().hex[:8]}:1:{'a' * 10}"):
        with pytest.raises(ToolError) as bad:
            run(client, "trace.read", {"reference": junk})
        assert bad.value.status in (404, 422)
    with pytest.raises(ToolError):
        run(client, "trace.read", {})


def test_repo_show_checks_the_path_and_the_repository(client) -> None:
    async def names(user_id):
        return [SimpleNamespace(name="topcoat-localdev")]

    client.app.state.trace.repos = SimpleNamespace(list=names)
    out = run(
        client,
        "repo.show",
        {"repo": "Topcoat-LocalDev", "path": "hub\\src/x.py", "from": 3, "to": 9},
    )["output"]
    assert out["shown"] is True and out["path"] == "hub/src/x.py" and out["from"] == 3
    jump = run(client, "repo.show", {"repo": "topcoat-localdev", "path": "a.py", "find": "def f"})
    assert jump["output"]["find"] == "def f"
    for bad_input, status in (
        ({"repo": "topcoat-localdev", "path": "../x"}, 422),
        ({"repo": "topcoat-localdev", "path": "x", "from": 9, "to": 3}, 422),
        ({"repo": "topcoat-localdev", "path": "x", "from": 0}, 422),
        ({"repo": "topcoat-localdev", "path": "x", "extra": 1}, 422),
        ({"repo": "someone-elses", "path": "x"}, 404),
    ):
        with pytest.raises(ToolError) as bad:
            run(client, "repo.show", bad_input)
        assert bad.value.status == status, bad_input


def test_the_self_tool_offers_the_two_new_tools_next_to_its_own(client) -> None:
    caller = Caller(A_ID, True, "agent")
    offered = client.portal.call(lambda: client.app.state.topcoat_self.offer(caller))
    assert {t["capability"] for t in offered} == {
        "topcoat.guide",
        "topcoat.state",
        "trace.read",
        "repo.show",
    }


def test_repo_show_tells_the_agent_that_it_returns_no_text_and_where_to_read(client) -> None:
    # seen live (2026-11-04): an agent took "shown" for a read and called it again and again
    from topcoat_hub.trace_tool import SHOW_DESCRIPTION

    assert "does NOT return" in SHOW_DESCRIPTION and "repo.read" in SHOW_DESCRIPTION

    async def names(user_id):
        return [SimpleNamespace(name="topcoat-localdev")]

    client.app.state.trace.repos = SimpleNamespace(list=names)
    out = run(client, "repo.show", {"repo": "topcoat-localdev", "path": "VERSION"})["output"]
    assert "no text" in out["note"] and "repo.read" in out["note"]
