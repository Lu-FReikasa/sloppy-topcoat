"""The second look at recent chats: another model reads answers, an error needs evidence."""

import json
import time
import uuid
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text

from tests.conftest import AUTH, TEST_DB_URL
from tests.fakenode import online_node
from tests.test_chat import new_conv, use_model
from tests.test_core import store_call
from tests.test_memory import chat_once
from tests.users import make_user
from topcoat_hub import chat_review

A_ID = uuid.UUID("00000000-0000-4000-8000-000000000001")
LONG = (
    "Der Plex-Container läuft auf node-nvidia und nutzt Port 32400. Zum Neustart reicht "
    "docker restart plex, die Konfiguration bleibt dabei erhalten."
)


CONFIRM = "You compare ONE statement"
RELATION = ["contradicts"]  # what the fake reviewer says when asked if a note contradicts a claim


def confirm_reply(req: dict) -> str | None:
    if req["messages"][0]["content"].startswith(CONFIRM):
        return json.dumps({"relation": RELATION[0]})
    return None


def models(*entries: tuple[str, bool]) -> list[dict[str, Any]]:
    return [
        {"id": m, "topcoat": {"nodes": [{"node": "n", "loaded": loaded}]}} for m, loaded in entries
    ]


def fleet(*entries: tuple[str, list[tuple]]) -> list[dict[str, Any]]:
    """Models as ``list_models`` shows them: (model, [(node, loaded[, state])])."""
    out = []
    for name, nodes in entries:
        rows = [
            {"node": n[0], "loaded": n[1], "state": n[2] if len(n) > 2 else "available"}
            for n in nodes
        ]
        out.append({"id": name, "topcoat": {"nodes": rows}})
    return out


def test_the_reviewer_is_another_loaded_model_and_the_author_only_when_nothing_else_can() -> None:
    ms = models(("gemma", True), ("qwen-big", False), ("qwen-small", True), ("bge-m3", True))
    assert chat_review.pick_reviewer(ms, "gemma", "") == "qwen-small@n"  # not the unloaded one
    # a machine with one model: that model checks, and the pick says so (D87, D88)
    assert chat_review.pick_reviewer(models(("gemma", True)), "gemma", "") == "gemma@n"
    # a model nobody configured and nothing has loaded is never picked
    assert chat_review.pick_reviewer(models(("gemma", True), ("big", False)), "gemma", "") == (
        "gemma@n"
    )
    assert chat_review.pick_reviewer(models(("bge-m3", True)), "gemma", "") is None
    assert chat_review.pick_reviewer([], "gemma", "") is None


def test_the_places_of_a_check_go_from_another_node_to_the_authors_own_model() -> None:
    ms = fleet(
        ("gemma", [("a", True), ("b", True)]),
        ("qwen", [("a", True), ("b", True)]),
    )
    # the author ran on node a: another model on another node first
    assert chat_review.pick_reviewer(ms, "gemma", "", None, "a") == "qwen@b"
    # another model on the author's node beats the author's model elsewhere (D87, issue 2.19)
    only_a = fleet(("gemma", [("a", True), ("b", True)]), ("qwen", [("a", True)]))
    assert chat_review.pick_reviewer(only_a, "gemma", "", None, "a") == "qwen@a"
    one_node = fleet(("gemma", [("a", True)]), ("qwen", [("a", True)]))
    assert chat_review.pick_reviewer(one_node, "gemma", "", None, "a") == "qwen@a"
    assert chat_review.pick_reviewer(fleet(("gemma", [("a", True)])), "gemma", "", None, "a") == (
        "gemma@a"
    )


def test_a_check_never_uses_a_node_that_is_busy_and_never_pushes_a_model_out() -> None:
    ms = fleet(
        ("gemma", [("a", True)]),
        ("qwen", [("b", True, "busy_gaming"), ("c", True, "yielded"), ("d", True, "paused")]),
    )
    assert chat_review.pick_reviewer(ms, "gemma", "", None, "a") == "gemma@a"
    # an unloaded model of the check order loads only on a node that holds nothing else
    idle = fleet(("gemma", [("a", True)]), ("qwen", [("b", False)]))
    assert chat_review.pick_reviewer(idle, "gemma", "", [("b", "qwen")], "a") == "qwen@b"
    busy = fleet(("gemma", [("a", True)]), ("qwen", [("b", False)]), ("other", [("b", True)]))
    assert chat_review.pick_reviewer(busy, "gemma", "", [("b", "qwen")], "a") == "other@b"
    # ... and never on the author's node, where the load would move the author's model
    same = fleet(("gemma", [("a", True)]), ("qwen", [("a", False)]))
    assert chat_review.pick_reviewer(same, "gemma", "", [("a", "qwen")], "a") == "gemma@a"


def test_a_check_only_goes_to_the_nodes_the_background_row_allows() -> None:
    # live (2026-11-06): the pick named node-amd, the run refused it (node_kept_free) and no
    # second look was stored; the choice must leave out the nodes the run would refuse
    ms = fleet(
        ("gemma", [("a", True), ("b", True)]),
        ("qwen", [("a", True), ("b", True)]),
    )
    assert chat_review.pick_reviewer(ms, "gemma", "", None, "a", {"b"}) == "qwen@b"
    assert chat_review.pick_reviewer(ms, "gemma", "", None, "a", {"a"}) == "qwen@a"
    assert chat_review.pick_reviewer(ms, "gemma", "", None, "a", {"c"}) is None
    # no restriction (None) keeps the rules as they were
    assert chat_review.pick_reviewer(ms, "gemma", "", None, "a", None) == "qwen@b"


def test_the_check_order_and_the_configured_model_decide_inside_a_place() -> None:
    ms = fleet(
        ("gemma", [("a", True)]),
        ("qwen", [("b", True)]),
        ("llama", [("c", True)]),
    )
    assert chat_review.pick_reviewer(ms, "gemma", "", None, "a") == "llama@c"  # by name
    assert chat_review.pick_reviewer(ms, "gemma", "qwen", None, "a") == "qwen@b"  # configured
    assert chat_review.pick_reviewer(ms, "gemma", "", [("b", "qwen")], "a") == "qwen@b"  # order
    # a configured model that is the author does not win a place it has not earned
    assert chat_review.pick_reviewer(ms, "gemma", "gemma", None, "a") == "llama@c"


def test_a_finding_is_an_error_only_with_a_note_that_contradicts_it() -> None:
    notes = [(uuid.uuid4(), "Plex läuft auf node-amd"), (uuid.uuid4(), "Zweite")]
    raw = json.dumps(
        {
            "findings": [
                {
                    "claim": "Plex läuft auf node-nvidia",
                    "reason": "Notiz sagt anders",
                    "note": "N1",
                    "severity": "major",
                },
                {"claim": "Port 32400", "reason": "unsicher", "note": None},
                {"claim": "x", "reason": "y", "note": "N9"},
                {"claim": "Passwort: Sommer2024xyz", "reason": "z", "note": None},
            ]
        }
    )
    findings = chat_review.parse_findings("Hier: " + raw, notes)
    assert [f["claim"] for f in findings] == ["Plex läuft auf node-nvidia", "Port 32400", "x"]
    assert findings[0]["evidence"]["text"] == "Plex läuft auf node-amd"
    assert findings[1]["evidence"] is None and findings[2]["evidence"] is None
    assert chat_review.verdict_of(findings) == "error"
    assert chat_review.verdict_of(findings[1:]) == "hint"
    assert chat_review.verdict_of([]) == "ok"
    assert chat_review.parse_findings("kein JSON", notes) == []
    # a backed finding that does not matter is orange, not red
    small = [{**findings[0], "severity": "minor"}]
    assert chat_review.verdict_of(small) == "hint"


RESULTS = ["docker.ps said: plex is running on node-amd, port 32400", "disk.usage said: 91 percent"]
CHAT = "User: Plex läuft doch auf node-amd?\nAssistant: Ja, auf Port 32400."


def _finding(**over: object) -> str:
    return json.dumps({"findings": [{"claim": "c", "reason": "r", "severity": "major", **over}]})


def test_a_tool_result_backs_a_finding_only_with_a_quote_that_stands_in_it() -> None:
    backed = chat_review.parse_findings(
        _finding(source="E1", quote="plex is running on node-amd"), [], RESULTS, CHAT
    )
    assert backed[0]["evidence"] == {"result": "E1", "text": "plex is running on node-amd"}
    assert chat_review.verdict_of(backed) == "error"
    for bad in (
        _finding(source="E1", quote="plex runs on node-nvidia"),  # not in the result
        _finding(source="E1"),  # no quote at all
        _finding(source="E3", quote="plex is running on node-amd"),  # no such result
    ):
        [f] = chat_review.parse_findings(bad, [], RESULTS, CHAT)
        assert f["evidence"] is None
        assert chat_review.verdict_of([f]) == "hint"


def test_the_conversation_backs_a_finding_only_with_a_quote_that_stands_in_it() -> None:
    [f] = chat_review.parse_findings(
        _finding(source="C", quote="Plex läuft doch auf node-amd?"), [], [], CHAT
    )
    assert f["evidence"] == {"chat": True, "text": "Plex läuft doch auf node-amd?"}
    [g] = chat_review.parse_findings(
        _finding(source="C", quote="Plex läuft auf dem Mond"), [], [], CHAT
    )
    assert g["evidence"] is None
    [h] = chat_review.parse_findings(_finding(source="C", quote="kurz"), [], [], CHAT)
    assert h["evidence"] is None  # a few letters prove nothing


@pytest.fixture
def world(client: TestClient, connect):
    author = online_node(client, connect, "node-a", loaded=["small"])
    reviewer = online_node(client, connect, "node-b", loaded=["other"])
    use_model(client, "small")
    seen: list[str] = []

    RELATION[0] = "contradicts"

    def answer(req: dict) -> str | None:
        system = req["messages"][0]["content"]
        if system.startswith(CONFIRM):
            return confirm_reply(req)
        if system.startswith("You review an answer"):
            seen.append(req["messages"][-1]["content"])
            return json.dumps(
                {
                    "findings": [
                        {
                            "claim": "Plex läuft auf node-nvidia",
                            "reason": "laut Notiz auf node-amd",
                            "note": "N1",
                            "severity": "major",
                        }
                    ]
                }
            )
        return None

    author.stream_text_fn = lambda req: LONG
    reviewer.stream_text_fn = lambda req: LONG
    author.answer_fn = answer
    reviewer.answer_fn = answer
    rv = client.app.state.reviewer
    rv.settings.memory_idle_seconds = 0
    # the tests below are about the model's look; since 2026-11-14 it reads on its own only when
    # the scheduled look is switched on (or when the person asks: review_now)
    rv.settings.review_scheduled = True
    return author, reviewer, rv, seen


def add_note(client: TestClient, note: str) -> None:
    async def go(store: Any) -> None:
        await store.add(
            note,
            embedding=None,
            embedder=None,
            evidence="live",
            confidence=0.9,
            source_type="manual",
            source_ref=None,
            reason="test",
            actor="user",
            kind="operational",
        )

    store_call(client, A_ID, go)


def test_a_review_by_another_model_marks_the_answer_and_waits_on_the_start_page(
    client: TestClient, world
) -> None:
    _, _, rv, seen = world
    add_note(client, "Plex läuft auf node-amd")
    conv = new_conv(client)
    chat_once(client, "Wo läuft Plex?", conv)
    assert client.portal.call(rv.tick) == 1
    # the same answer is not reviewed twice
    assert client.portal.call(rv.tick) == 0
    assert "Notes:" in seen[0] and "N1: Plex läuft auf node-amd" in seen[0]
    mine = client.get(f"/api/conversations/{conv}/reviews", headers=AUTH).json()
    assert len(mine) == 1 and mine[0]["verdict"] == "error"
    assert mine[0]["findings"][0]["evidence"]["text"] == "Plex läuft auf node-amd"
    assert mine[0]["reviewer_model"] != mine[0]["author_model"]
    waiting = client.get("/api/reviews", headers=AUTH).json()
    assert [r["id"] for r in waiting["errors"]] == [mine[0]["id"]] and waiting["hints"] == []
    assert client.post(f"/api/reviews/{mine[0]['id']}/seen", headers=AUTH).status_code == 204
    assert client.get("/api/reviews", headers=AUTH).json() == {"errors": [], "hints": []}
    # every run leaves an entry in the memory tab
    runs = client.get("/api/memory", headers=AUTH).json()["reviews"]["runs"]
    assert runs[0]["status"] == "ok" and runs[0]["stats"]["errors"] == 1


def test_without_evidence_it_is_only_a_hint(client: TestClient, world) -> None:
    conv = new_conv(client)
    chat_once(client, "Wo läuft Plex?", conv)
    assert client.portal.call(world[2].tick) == 1
    [r] = client.get(f"/api/conversations/{conv}/reviews", headers=AUTH).json()
    assert r["verdict"] == "hint"
    assert client.get("/api/reviews", headers=AUTH).json()["errors"] == []


def test_the_traffic_light_of_an_answer_is_its_verdict_and_yellow_while_it_waits(
    client: TestClient, world
) -> None:
    rv = world[2]
    conv = new_conv(client)
    chat_once(client, "Wo läuft Plex?", conv)
    msgs = client.get(f"/api/conversations/{conv}", headers=AUTH).json()["messages"]
    answer_id = msgs[-1]["id"]
    # queued but not yet reviewed: yellow
    rv._waiting[answer_id] = uuid.UUID(conv)
    lights = client.get(f"/api/conversations/{conv}/review-lights", headers=AUTH).json()
    assert lights == {"lights": {}, "waiting": [answer_id]}
    rv._waiting.clear()
    assert client.portal.call(rv.tick) == 1
    lights = client.get(f"/api/conversations/{conv}/review-lights", headers=AUTH).json()
    assert lights == {"lights": {str(answer_id): "hint"}, "waiting": []}  # orange
    # somebody else's conversation shows nothing
    _, other = make_user(TEST_DB_URL, "mallory")
    nothing = client.get(f"/api/conversations/{conv}/review-lights", headers=other).json()
    assert nothing == {"lights": {}, "waiting": []}


def test_old_chats_are_left_alone(client: TestClient, world) -> None:
    rv = world[2]
    conv = new_conv(client)
    chat_once(client, "Wo läuft Plex?", conv)
    engine = create_engine(client.app.state.settings.database_url)
    with engine.begin() as conn:
        conn.execute(text("UPDATE messages SET created_at = now() - interval '30 hours'"))
    engine.dispose()
    assert client.portal.call(rv.tick) == 0


def test_with_only_the_authors_model_there_is_no_review_and_the_run_says_so(
    client: TestClient, connect
) -> None:
    node = online_node(client, connect, "node-a", loaded=["small"])
    use_model(client, "small")
    node.stream_text_fn = lambda req: LONG
    rv = client.app.state.reviewer
    rv.settings.memory_idle_seconds = 0
    rv.settings.review_scheduled = True
    conv = new_conv(client)
    chat_once(client, "Wo läuft Plex?", conv)

    async def only_small() -> list[dict[str, Any]]:
        return models(("small", True))

    rv.gateway.list_models = only_small  # the nodes serve nothing but the author's model
    assert client.portal.call(rv.tick) == 0
    runs = client.get("/api/memory", headers=AUTH).json()["reviews"]["runs"]
    assert runs[0]["status"] == "no_model"
    assert client.get(f"/api/conversations/{conv}/reviews", headers=AUTH).json() == []


def test_a_review_of_another_user_is_not_visible(client: TestClient, world) -> None:
    from tests.users import make_user

    conv = new_conv(client)
    chat_once(client, "Wo läuft Plex?", conv)
    client.portal.call(world[2].tick)
    [r] = client.get(f"/api/conversations/{conv}/reviews", headers=AUTH).json()
    _, other = make_user(client.app.state.settings.database_url, "mallory")
    assert client.get(f"/api/conversations/{conv}/reviews", headers=other).json() == []
    assert client.post(f"/api/reviews/{r['id']}/seen", headers=other).status_code == 404
    assert client.get("/api/reviews", headers=other).json() == {"errors": [], "hints": []}


def wait_for_reviews(client: TestClient, conv: str, count: int) -> list[dict[str, Any]]:
    deadline = time.monotonic() + 5
    while True:
        mine = client.get(f"/api/conversations/{conv}/reviews", headers=AUTH).json()
        if len(mine) >= count or time.monotonic() > deadline:
            return mine
        time.sleep(0.05)


def test_a_finding_reaches_the_agent_with_the_next_question_once(client: TestClient, world) -> None:
    author, _, rv, _ = world
    add_note(client, "Plex läuft auf node-amd")
    prompts: list[str] = []

    def answer(req: dict) -> str:
        prompts.append(req["messages"][-1]["content"])
        return LONG

    author.stream_text_fn = answer
    conv = new_conv(client)
    chat_once(client, "Wo läuft Plex?", conv)
    assert client.portal.call(rv.tick) == 1
    [review] = wait_for_reviews(client, conv, 1)
    assert review["verdict"] == "error"
    # the chat goes on: the finding stands in front of the next question, once
    rv.settings.review_scheduled = False  # nothing new is reviewed
    chat_once(client, "Und der Port?", conv)
    assert "Plex läuft auf node-nvidia" in prompts[-1]
    assert "Plex läuft auf node-amd" in prompts[-1]  # the note that contradicts the claim
    assert prompts[-1].rstrip().endswith("Und der Port?")
    chat_once(client, "Danke, und der Container?", conv)
    assert "second model" not in prompts[-1]
    # the user's own list is not touched by the agent having been told
    assert client.get("/api/reviews", headers=AUTH).json()["errors"][0]["id"] == review["id"]


def test_no_model_reads_a_fresh_answer_on_its_own(client: TestClient, world) -> None:
    _, _, rv, seen = world
    conv = new_conv(client)
    chat_once(client, "Wo läuft Plex?", conv)
    time.sleep(0.3)
    # an answer without a check has nothing to compare its citations with: no light at all
    assert client.get(f"/api/conversations/{conv}/reviews", headers=AUTH).json() == []
    assert seen == []
    rv.settings.review_scheduled = False
    assert client.portal.call(rv.tick) == 0  # and the scheduled look is off by default
    rv.settings.review_scheduled = True
    assert client.portal.call(rv.tick) == 1  # the old way still finds it when switched on


def wait_for_messages(client: TestClient, conv: str, count: int) -> list[dict[str, Any]]:
    deadline = time.monotonic() + 8
    while True:
        msgs = client.get(f"/api/conversations/{conv}", headers=AUTH).json()["messages"]
        done = [m for m in msgs if m["status"] != "running"]
        if len(done) >= count or time.monotonic() > deadline:
            return msgs
        time.sleep(0.05)


def test_the_agent_no_longer_speaks_up_on_its_own(client: TestClient, world) -> None:
    """Live 2026-11-14: a message of its own about a finding ran next to the person's next
    question in the same conversation. A finding now waits for the next question."""
    _, _, rv, _ = world
    add_note(client, "Plex läuft auf node-amd")
    conv = new_conv(client)
    chat_once(client, "Wo läuft Plex?", conv)
    assert client.portal.call(rv.tick) == 1
    [review] = wait_for_reviews(client, conv, 1)
    assert review["verdict"] == "error"
    time.sleep(0.5)
    msgs = client.get(f"/api/conversations/{conv}", headers=AUTH).json()["messages"]
    assert [m["role"] for m in msgs] == ["user", "assistant"]


def test_a_hint_without_evidence_waits_for_the_next_question(client: TestClient, world) -> None:
    _, _, rv, _ = world
    # no note in the memory: the reviewer's claim has no evidence
    conv = new_conv(client)
    chat_once(client, "Wo läuft Plex?", conv)
    client.portal.call(rv.tick)
    [review] = wait_for_reviews(client, conv, 1)
    assert review["verdict"] == "hint"
    time.sleep(0.3)
    msgs = client.get(f"/api/conversations/{conv}", headers=AUTH).json()["messages"]
    assert [m["role"] for m in msgs] == ["user", "assistant"]


def test_a_minor_finding_with_evidence_waits_for_the_next_question(
    client: TestClient, world
) -> None:
    author, reviewer, rv, _ = world
    rv.settings.review_after_answer = True
    add_note(client, "Plex läuft auf node-amd")

    def minor(req: dict) -> str | None:
        if req["messages"][0]["content"].startswith(CONFIRM):
            return confirm_reply(req)
        if req["messages"][0]["content"].startswith("You review an answer"):
            return json.dumps(
                {"findings": [{"claim": "Port 32400", "reason": "Notiz sagt 32401", "note": "N1"}]}
            )
        return None

    author.answer_fn = minor
    reviewer.answer_fn = minor
    prompts: list[str] = []

    def answer(req: dict) -> str:
        prompts.append(req["messages"][-1]["content"])
        return LONG

    author.stream_text_fn = answer
    conv = new_conv(client)
    chat_once(client, "Wo läuft Plex?", conv)
    client.portal.call(rv.tick)
    [review] = wait_for_reviews(client, conv, 1)
    assert review["verdict"] == "hint" and review["findings"][0]["severity"] == "minor"
    time.sleep(0.3)
    msgs = client.get(f"/api/conversations/{conv}", headers=AUTH).json()["messages"]
    assert [m["role"] for m in msgs] == ["user", "assistant"]  # no message of its own
    chat_once(client, "Und der Port?", conv)
    assert "Port 32400" in prompts[-1]  # but the agent hears of it with the next question


def test_a_note_that_only_does_not_mention_the_claim_is_no_contradiction(
    client: TestClient, world
) -> None:
    _, _, rv, _ = world
    rv.settings.review_after_answer = True
    RELATION[0] = "unrelated"  # the second question: the note says nothing about the claim
    add_note(client, "Plex läuft auf node-amd")
    conv = new_conv(client)
    chat_once(client, "Wo läuft Plex?", conv)
    client.portal.call(rv.tick)
    [review] = wait_for_reviews(client, conv, 1)
    assert review["verdict"] == "hint" and review["findings"][0]["evidence"] is None
    time.sleep(0.3)
    msgs = client.get(f"/api/conversations/{conv}", headers=AUTH).json()["messages"]
    assert [m["role"] for m in msgs] == ["user", "assistant"]  # the agent does not speak up


def test_the_scheduled_look_does_not_dig_up_answers_from_before_the_last_half_hour(
    client: TestClient, world
) -> None:
    _, _, rv, _ = world
    conv = new_conv(client)
    chat_once(client, "Wo läuft Plex?", conv)  # reviewed by no one yet: after_answer is off
    rv.settings.review_after_answer = True
    engine = create_engine(client.app.state.settings.database_url)
    with engine.begin() as c:
        c.execute(text("UPDATE messages SET created_at = now() - interval '2 hours'"))
    assert client.portal.call(rv.tick) == 0  # an old answer: left alone
    rv.settings.review_after_answer = False
    assert client.portal.call(rv.tick) == 1  # without the look at once the old rule holds


def test_an_answer_that_is_already_reviewed_is_not_stored_twice(client: TestClient, world) -> None:
    _, _, rv, _ = world
    conv = new_conv(client)
    chat_once(client, "Wo läuft Plex?", conv)
    [row] = client.portal.call(lambda: rv.candidates(5))
    assert client.portal.call(lambda: rv._store(row, "other", "ok", [])) is True
    # the scheduled look and the look right after the answer can meet on one answer
    assert client.portal.call(lambda: rv._store(row, "other", "ok", [])) is False
    assert len(client.get(f"/api/conversations/{conv}/reviews", headers=AUTH).json()) <= 1


def test_only_an_important_finding_with_evidence_is_worth_a_message() -> None:
    evidence = {"note_id": "x", "text": "t"}
    assert chat_review.worth_a_message(
        [{"claim": "a", "reason": "b", "evidence": evidence, "severity": "major"}]
    )
    assert not chat_review.worth_a_message(
        [{"claim": "a", "reason": "b", "evidence": evidence, "severity": "minor"}]
    )
    assert not chat_review.worth_a_message(
        [{"claim": "a", "reason": "b", "evidence": None, "severity": "major"}]
    )
    assert not chat_review.worth_a_message([])
    # a reviewer that names no severity counts as minor
    notes = [(uuid.uuid4(), "n")]
    [f] = chat_review.parse_findings(
        json.dumps({"findings": [{"claim": "c", "reason": "r", "note": "N1"}]}), notes
    )
    assert f["severity"] == "minor"


def test_the_reviewer_reads_the_tool_results_behind_an_answer(client: TestClient, world) -> None:
    """What the check really saw (a tool message that looks odd) is shown to the reviewer next to
    the answer, so that a true statement is not called a mistake."""
    _, _, rv, seen = world
    conv = new_conv(client)
    chat_once(client, "Was steht in der README?", conv)
    task = uuid.uuid4()
    engine = create_engine(client.app.state.settings.database_url)
    with engine.begin() as c:
        c.execute(
            text(
                "INSERT INTO agent_tasks (id, user_id, prompt, title, status, created_at, "
                "updated_at) VALUES (:t, :u, 'p', 't', 'done', now(), now())"
            ),
            {"t": task, "u": A_ID},
        )
        for ref, tag, cap, excerpt in (
            ("E1", "live", "repo.read", "error: file is not valid UTF-8"),
            ("E2", "doc", "knowledge.search", "a document page, no live result"),
        ):
            c.execute(
                text(
                    "INSERT INTO agent_evidence (task_id, user_id, ref, tag, source, excerpt, "
                    "bytes) VALUES (:t, :u, :r, :g, CAST(:s AS jsonb), :e, 10)"
                ),
                {
                    "t": task,
                    "u": A_ID,
                    "r": ref,
                    "g": tag,
                    "s": json.dumps({"capability": cap}),
                    "e": excerpt,
                },
            )
        c.execute(
            text("UPDATE messages SET agent_task_id = :t WHERE role = 'assistant'"), {"t": task}
        )
    assert client.portal.call(rv.tick) == 1
    block = seen[0].split("Tool results:")[1].split("Answer to review:")[0]
    assert "E1 (repo.read): error: file is not valid UTF-8" in block
    assert "E2" not in block  # only what was seen live on a machine
    assert "never report it" in chat_review.REVIEW_TOOLS_NOTE
    assert "never report it" not in chat_review.REVIEW_SYSTEM


def test_an_answer_without_a_check_has_no_tool_results_block(client: TestClient, world) -> None:
    _, _, rv, seen = world
    conv = new_conv(client)
    chat_once(client, "Wo läuft Plex?", conv)
    assert client.portal.call(rv.tick) == 1
    assert "Tool results:" not in seen[0]


def test_the_reviewer_reads_the_conversation_not_the_hubs_own_lines() -> None:
    """2026-11-13: it read the last 6 messages of any kind; a check's lines filled them."""
    from types import SimpleNamespace as M

    rows = [
        M(id=1, role="user", content="Mach eine Übergabe für die Recherche fertig.", kind="say"),
        M(id=2, role="assistant", content="Soll ich die Übergabe vorbereiten?", kind="say"),
        M(id=3, role="user", content="ja bitte", kind="say"),
        M(id=4, role="assistant", content="Schau ich mir an.", kind="say"),
        M(id=5, role="assistant", content="läuft: web.search", kind="progress"),
        M(id=6, role="assistant", content="**Summary:** nichts gefunden", kind="report"),
        M(id=7, role="assistant", content="Die Antwort", kind="say"),
    ]
    lines = chat_review.earlier_lines(rows, 7)
    assert lines[0].startswith("User: Mach eine Übergabe")
    assert "Check: **Summary:** nichts gefunden" in lines
    assert not any("Schau ich mir an" in x or "web.search" in x for x in lines)
    # newest first until the budget: the oldest part goes when it is long
    many = [M(id=i, role="user", content="x" * 1000, kind="say") for i in range(1, 20)]
    kept = chat_review.earlier_lines(many, 20)
    assert 1 < len(kept) < 19 and sum(map(len, kept)) <= chat_review.EARLIER_BUDGET + 1200


def _answer_with_evidence(client: TestClient, conv: str, excerpts: dict[str, str]) -> int:
    """The last answer of ``conv`` as if a check with these results stood behind it."""
    task = uuid.uuid4()
    engine = create_engine(client.app.state.settings.database_url)
    with engine.begin() as c:
        c.execute(
            text(
                "INSERT INTO agent_tasks (id, user_id, prompt, title, status, created_at, "
                "updated_at) VALUES (:t, :u, 'p', 't', 'done', now(), now())"
            ),
            {"t": task, "u": A_ID},
        )
        for ref, excerpt in excerpts.items():
            c.execute(
                text(
                    "INSERT INTO agent_evidence (task_id, user_id, ref, tag, source, excerpt, "
                    "bytes) VALUES (:t, :u, :r, 'live', CAST('{}' AS jsonb), :e, 10)"
                ),
                {"t": task, "u": A_ID, "r": ref, "e": excerpt},
            )
        answer_id = c.execute(
            text(
                "UPDATE messages SET agent_task_id = :t, content = :c WHERE role = 'assistant' "
                "AND conversation_id = :v RETURNING id"
            ),
            {
                "t": task,
                "v": conv,
                "c": "Das Menü findest du unter 'Settings > Me' [E1]. Plex läuft [E1]. " + LONG,
            },
        ).scalar_one()
        c.execute(text("DELETE FROM chat_reviews WHERE message_id = :m"), {"m": answer_id})
    engine.dispose()
    return int(answer_id)


def test_right_after_an_answer_the_hub_checks_its_citations_without_a_model(
    client: TestClient, world
) -> None:
    _, _, rv, seen = world
    conv = new_conv(client)
    chat_once(client, "Wie ändere ich das Theme?", conv)
    answer_id = _answer_with_evidence(client, conv, {"E1": '{"node": "topcoat", "plex": "up"}'})
    row = SimpleNamespace(id=answer_id, cid=uuid.UUID(conv), uid=A_ID, model="small", node="node-a")
    client.portal.call(lambda: rv._check_citations(row))
    [review] = client.get(f"/api/conversations/{conv}/reviews", headers=AUTH).json()
    assert review["reviewer_model"] == chat_review.HUB_CHECK and review["verdict"] == "hint"
    assert [f["rule"] for f in review["findings"]] == ["not_in_result"]
    assert "Settings > Me" in review["findings"][0]["reason"]
    assert seen == []  # no model was asked


def test_a_model_reads_an_answer_when_the_person_asks_without_notes_as_truth(
    client: TestClient, world
) -> None:
    _, _, rv, seen = world
    add_note(client, "Plex läuft auf node-amd")
    conv = new_conv(client)
    chat_once(client, "Wo läuft Plex?", conv)
    answer_id = _answer_with_evidence(client, conv, {"E1": "plex: up"})
    rv.settings.review_scheduled = False
    started = client.post(f"/api/messages/{answer_id}/review", headers=AUTH)
    assert started.status_code == 202
    [review] = wait_for_reviews(client, conv, 1)
    assert review["reviewer_model"] != chat_review.HUB_CHECK
    assert "Notes:" not in seen[-1] and "Plex läuft auf node-amd" not in seen[-1]
    assert "Tool results:" in seen[-1]
    # a note cannot back a finding any more: the claim the model names stays a hint
    assert review["verdict"] == "hint"
    msgs = client.get(f"/api/conversations/{conv}", headers=AUTH).json()["messages"]
    assert [m["role"] for m in msgs] == ["user", "assistant"]  # nothing speaks up


def test_the_hub_check_flags_a_report_that_repeats_the_one_before(
    client: TestClient, world
) -> None:
    # live 2026-11-17: "ja bitte" to an offer to explain ran the same diagnosis again
    _, _, rv, seen = world
    conv = new_conv(client)
    chat_once(client, "Was ist mit dem WLAN?", conv)
    answer_id = _answer_with_evidence(client, conv, {"E1": "Plex läuft"})
    engine = create_engine(client.app.state.settings.database_url)
    summary = "**Summary:** Keine WLAN-Abbrüche in den Logs von gestern, das Pixel 7 ist am Lu AP."
    with engine.begin() as c:
        for _ in range(2):  # two report cards before the answer, both saying the same
            c.execute(
                text(
                    "INSERT INTO messages (conversation_id, user_id, role, content, kind, status, "
                    "created_at) VALUES (:v, :u, 'assistant', :c, 'report', 'complete', now())"
                ),
                {"v": conv, "u": A_ID, "c": summary},
            )
        # the answer comes after its report card
        answer_id = c.execute(
            text(
                "INSERT INTO messages (conversation_id, user_id, role, content, kind, status, "
                "agent_task_id, created_at) SELECT conversation_id, user_id, role, content, kind, "
                "status, agent_task_id, now() FROM messages WHERE id = :m RETURNING id"
            ),
            {"m": answer_id},
        ).scalar_one()
    engine.dispose()
    row = SimpleNamespace(id=answer_id, cid=uuid.UUID(conv), uid=A_ID, model="small", node="node-a")
    client.portal.call(lambda: rv._check_citations(row))
    reviews = client.get(f"/api/conversations/{conv}/reviews", headers=AUTH).json()
    review = next(r for r in reviews if r["message_id"] == answer_id)
    assert review["verdict"] == "hint"
    assert "repeated_result" in [f["rule"] for f in review["findings"]]
    assert seen == []  # no model was asked


def test_a_model_of_the_authors_family_checks_last() -> None:
    """Issue 2.19: gpt-oss checked gpt-oss. Another family on any node comes first; the author's
    family only when nothing else is up (the result then says same_family)."""
    from topcoat_hub.model_profiles import same_family

    ms = fleet(
        ("openai_gpt-oss-20b-MXFP4", [("node-desk", True), ("other-pc", True)]),
        ("Qwen3-Coder-30B-A3B-IQ4_NL", [("node-desk", True)]),
    )
    # the coder on the author's own node beats gpt-oss on another node
    assert (
        chat_review.pick_reviewer(
            ms, "openai_gpt-oss-20b-MXFP4", "", author_node="node-desk", same_model_elsewhere=True
        )
        == "Qwen3-Coder-30B-A3B-IQ4_NL@node-desk"
    )
    assert same_family("openai_gpt-oss-20b-MXFP4@node-desk", "gpt-oss-20b-Q4@x")
    assert not same_family("openai_gpt-oss-20b-MXFP4", "Qwen3-Coder-30B-A3B-IQ4_NL")
    assert not same_family(None, "gpt-oss")
