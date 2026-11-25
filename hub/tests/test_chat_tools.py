"""The chat looks at servers (E3-01d): a question about the user's machines runs the agent loop
first (visible steps, evidence) and is answered from what it found; everything else is as before."""

import json
from typing import Any

import pytest
from fastapi.testclient import TestClient

from tests.conftest import AUTH, TEST_DB_URL
from tests.fakenode import eventually, online_node
from tests.test_agent import ScriptedModel, finish, use
from tests.test_chat import (
    answer_text,
    new_conv,
    sent_messages,
    sse_events,
    use_model,
)
from tests.test_tools import tools_node
from tests.users import make_user
from topcoat_hub import chat, chat_tools
from topcoat_hub.agent_loop import ModelFailure


def ask_with(
    client: TestClient, conv: str, content: str, tools: str = "auto", who: dict[str, str] = AUTH
) -> tuple[int, list[tuple[str, dict[str, Any]]], Any]:
    r = client.post(
        f"/api/conversations/{conv}/messages",
        headers=who,
        json={"content": content, "tools": tools},
    )
    if r.headers.get("content-type", "").startswith("text/event-stream"):
        return r.status_code, sse_events(r.text), r
    return r.status_code, [], r


@pytest.fixture
def world(client: TestClient, connect):
    """A tool node with canned answers and a model node that answers the chat."""
    node_id, tnode = tools_node(client, connect, "vm-docker")
    tnode.capability_answers["docker.logs"] = {
        "container": "web",
        "lines": 3,
        "log": "2026-09-30T07:59:00Z starting\n2026-09-30T07:59:05Z fatal: out of memory\n",
        "truncated": False,
        "masked": 0,
    }
    tnode.capability_answers["system.info"] = {"hostname": "vm", "uptime_seconds": 1000}
    model = online_node(client, connect, "node-a", loaded=["small"])
    use_model(client)
    return node_id, tnode, model


FINDINGS = [
    {"claim": "web was killed for memory", "tag": "live", "evidence": ["E1"]},
    {"claim": "the limit may be too low", "tag": "assumption", "confirm_with": "docker.inspect"},
]


def script_check(client: TestClient) -> ScriptedModel:
    look = json.dumps(
        {
            "action": "tool",
            "node": "vm-docker",
            "capability": "docker.logs",
            "input": {"container": "web", "lines": 50},
            "reason": "see why web restarts",
        }
    )
    return use(client, look, finish("web ran out of memory.", FINDINGS, "raise the memory limit"))


# --- the pieces -------------------------------------------------------------------------------

NODES = {"tools-alpha", "tools-bravo"}


@pytest.mark.parametrize(
    "text",
    [
        "Warum startet mein Docker Container web ständig neu?",
        "Schau mal nach den Logs auf alpha",
        "Wie viel Speicherplatz ist auf dem Server noch frei?",
        "Why is the proxmox host slow right now?",
        "Prüf mal meine Container",
        "Wie ist der Status von tools-bravo?",
        "Check my server please",
        "why does my service keep restarting? check the logs",
        "Welche Container laufen bei mir?",
        "Läuft Docker gerade?",
        "Suche im Web nach dem Fehler cuda out of memory",
        "Kannst du das online recherchieren?",
        "Lies bitte https://example.org/release-notes",
        # live 2026-11-12: the answer to "do you know?" was "look online", and nothing was searched
        "Schaue gern Online",
        "guck mal im Internet nach",
        "Kannst du das online nachsehen?",
        "please look it up online",
    ],
)
def test_questions_about_the_users_machines_are_detected(text: str) -> None:
    assert chat_tools.check_need(text, NODES) == "check"


@pytest.mark.parametrize(
    "text",
    [
        "Wie installiere ich Docker unter Ubuntu?",
        "Was ist der Unterschied zwischen Docker und VMs?",
        "Erkläre mir Kubernetes",
        "Schreibe ein Gedicht über den Herbst",
        "Wie funktioniert ein Minecraft Server?",
        "What is systemd?",
        "Hallo",
        "Mein Code hat einen Fehler",
        "Schreibe mir bitte in einem Satz, was ein Docker-Container ist.",
        "Was ist ein Docker Container?",
        "Wie viel RAM braucht ein Minecraft Server?",
        "Und der andere Container?",
    ],
)
def test_general_questions_do_not_start_a_check(text: str) -> None:
    assert chat_tools.check_need(text, NODES) != "check"
    assert chat_tools.check_need(text, NODES) == ""  # and they are not offered one either


@pytest.mark.parametrize(
    "text",
    [
        "Warum startet der Container neu?",  # whose container? probably the user's: ask first
        "Kennst du gute Server-Hardware für mein Budget?",
    ],
)
def test_in_doubt_the_answer_comes_first_and_the_check_is_offered(text: str) -> None:
    assert chat_tools.check_need(text, NODES) == "offer"
    assert chat_tools.check_need(text, NODES) != "check"


def test_the_check_block_is_honest_about_what_was_and_was_not_seen() -> None:
    none = chat_tools.check_block(chat_tools.CheckResult(error="no model could answer"), 4000)
    assert "could NOT be made" in none and "no model could answer" in none
    assert "do not guess" in none


def _evidence(ref: str, capability: str, node: str, excerpt: str) -> Any:
    from topcoat_hub.models import AgentEvidence

    return AgentEvidence(
        ref=ref,
        tag="live",
        source={"capability": capability, "node": node, "input": {}},
        excerpt=excerpt,
        bytes=len(excerpt),
    )


def test_a_check_that_failed_after_looking_hands_over_what_it_saw() -> None:
    """First hardware test: three tool calls with good evidence E1-E3, then the model's finish was
    cut off and the task failed — the chat answered "I cannot check anything"."""
    from topcoat_hub.models import AgentTask

    evidence = [
        _evidence("E1", "disk.usage", "tools-alpha", "/srv/data 86.2% used"),
        _evidence("E2", "docker.ps", "tools-alpha", "web running, db running"),
        _evidence("E3", "system.info", "tools-alpha", "uptime 12 days"),
    ]
    task = AgentTask(status="failed", stop_reason="bad_format", error="cut off", diagnosis=None)
    result = chat_tools.CheckResult(task, evidence, error="the model's answer was cut off")
    block = chat_tools.check_block(result, 6000)
    assert "INCOMPLETE" in block and "the model's answer was cut off" in block
    assert "could NOT be made" not in block and "no results about the servers" not in block
    assert "DO have results" in block and "Never say that you cannot look" in block
    for needle in (
        "[E1] disk.usage on tools-alpha",
        "86.2% used",
        "[E3] system.info",
        "<<<DATA",
    ):
        assert needle in block
    # without any evidence it stays the plain refusal
    nothing = chat_tools.check_block(chat_tools.CheckResult(task, [], error="boom"), 6000)
    assert "could NOT be made" in nothing and "INCOMPLETE" not in nothing
    # a check that took too long (no task row) with evidence is incomplete too
    slow = chat_tools.check_block(
        chat_tools.CheckResult(None, evidence[:1], error="the check took too long"), 6000
    )
    assert "INCOMPLETE" in slow and "86.2% used" in slow


# --- a chat answer after a check --------------------------------------------------------------


def test_a_forced_check_shows_its_steps_and_the_answer_rests_on_the_evidence(
    client: TestClient, world
) -> None:
    _, tnode, model = world
    script_check(client)
    conv = new_conv(client)
    status, events, _ = ask_with(client, conv, "Was ist mit web los?", "on")
    assert status == 200
    kinds = [e for e, _ in events]
    assert kinds[0] == "meta" and events[0][1]["checking"] == "forced"
    assert kinds.index("tools_start") < kinds.index("tool_step") < kinds.index("tools_done")
    assert kinds.index("tools_done") < kinds.index("delta") and kinds[-1] == "done"
    assert "memory" not in kinds  # nothing is learned about the user from server facts
    steps = [d for e, d in events if e == "tool_step"]
    assert [s["kind"] for s in steps] == ["plan", "call"]
    assert steps[0]["text"] == "see why web restarts"
    call = steps[1]
    assert (call["capability"], call["node"], call["ok"], call["ref"]) == (
        "docker.logs",
        "vm-docker",
        True,
        "E1",
    )
    done_tools = next(d for e, d in events if e == "tools_done")
    assert done_tools["status"] == "done" and done_tools["calls"] == 1
    assert done_tools["evidence"][0]["ref"] == "E1" and done_tools["evidence"][0]["tag"] == "live"
    assert [c["capability"] for c in tnode.capability_calls] == ["docker.logs"]

    # the model that writes the answer got the results before the question, and the rules
    sent = sent_messages(model)
    assert "Server check for this message:" in sent[-1]["content"]
    assert "[live E1] web was killed for memory" in sent[-1]["content"]
    assert "[assumption] the limit may be too low (to check: docker.inspect)" in sent[-1]["content"]
    assert "fatal: out of memory" in sent[-1]["content"] and "<<<DATA " in sent[-1]["content"]
    assert sent[-1]["content"].rstrip().endswith("Was ist mit web los?")
    assert "Result of the server check" in sent[0]["content"]
    # the report card says it all (E3-07 D3): the model is told not to copy it, and answers
    assert "do NOT copy it" in sent[0]["content"] and "only look" in sent[0]["content"]
    assert "[assumption]" in sent[-1]["content"]  # but it has the findings to say what matters

    message = events[-1][1]["message"]
    assert message["agent_task_id"] == done_tools["task_id"] and message["status"] == "complete"
    stored = client.get(f"/api/conversations/{conv}", headers=AUTH).json()["messages"]
    # user, the hub's confirmation, the report card and the closing bubble (E3-07 D3)
    assert [m["kind"] for m in stored] == ["say", "say", "report", "say"]
    assert stored[1]["agent_task_id"] is None
    assert stored[2]["agent_task_id"] == done_tools["task_id"]
    assert stored[3]["agent_task_id"] == done_tools["task_id"]
    # the check is a task of its own, kept out of the list of tasks
    task = client.get(f"/api/agent/tasks/{done_tools['task_id']}", headers=AUTH).json()
    assert task["origin"] == "chat" and task["status"] == "done" and task["tool_calls"] == 1
    assert client.get("/api/agent/tasks", headers=AUTH).json() == []
    everything = client.get("/api/agent/tasks", headers=AUTH, params={"include_chat": True}).json()
    assert [t["id"] for t in everything] == [done_tools["task_id"]]
    # the call is audited as the agent's, like any check
    audit = client.get("/api/tool-calls", headers=AUTH).json()
    assert audit[0]["capability"] == "docker.logs"


def test_a_question_about_the_servers_is_detected_a_general_one_is_not(
    client: TestClient, world
) -> None:
    _, _, model = world
    script_check(client)
    conv = new_conv(client)
    _, events, _ = ask_with(client, conv, "Warum startet mein Docker Container web ständig neu?")
    assert events[0][1]["checking"] == "detected" and "tools_start" in [e for e, _ in events]

    _, events, _ = ask_with(client, conv, "Wie funktioniert ein Minecraft Server?")
    kinds = [e for e, _ in events]
    assert "tools_start" not in kinds and "checking" not in events[0][1]
    # the assistant knows which systems it can look at (not only containers: every group offered)
    system = sent_messages(model)[0]["content"]
    assert "Docker containers" in system and "host health" in system
    assert "Result of the server check" not in system  # the rules belong to checked answers only


def test_the_user_can_switch_the_check_off(client: TestClient, world) -> None:
    script_check(client)
    conv = new_conv(client)
    _, events, _ = ask_with(
        client, conv, "Warum startet mein Docker Container web ständig neu?", "off"
    )
    assert "tools_start" not in [e for e, _ in events] and events[-1][0] == "done"


def test_without_tools_a_forced_check_is_refused_and_auto_answers_plainly(
    client: TestClient, connect
) -> None:
    online_node(client, connect, "node-a", loaded=["small"])
    use_model(client)
    conv = new_conv(client)
    status, _, r = ask_with(client, conv, "Check my server", "on")
    assert status == 409 and r.json()["detail"]["code"] == "no_capabilities"
    status, events, _ = ask_with(client, conv, "Warum startet mein Docker Container neu?")
    assert status == 200 and events[-1][0] == "done"
    assert "tools_start" not in [e for e, _ in events]


def test_a_check_that_fails_says_why_and_the_answer_does_not_guess(
    client: TestClient, world
) -> None:
    _, _, model = world
    reason = "no node could serve 'big': node-laptop: not enough free RAM (needs ~21000 MB)"
    use(client, ModelFailure(reason, reason="insufficient_memory"))
    conv = new_conv(client)
    status, events, _ = ask_with(client, conv, "Was ist mit web los?", "on")
    assert status == 200
    done_tools = next(d for e, d in events if e == "tools_done")
    assert done_tools["status"] == "failed" and done_tools["reason"] == "insufficient_memory"
    assert "not enough free RAM" in done_tools["error"]
    last = sent_messages(model)[-1]["content"]
    assert "could NOT be made" in last and "not enough free RAM" in last
    assert events[-1][0] == "done"  # still answered, honestly


def test_the_steps_of_a_stored_check_belong_to_its_owner_only(client: TestClient, world) -> None:
    script_check(client)
    conv = new_conv(client)
    _, events, _ = ask_with(client, conv, "Was ist mit web los?", "on")
    task_id = next(d for e, d in events if e == "tools_done")["task_id"]
    _, other = make_user(TEST_DB_URL, "anna")
    assert client.get(f"/api/agent/tasks/{task_id}", headers=other).status_code == 404
    # and nobody can ask the chat to check with rights they do not have: anna has no grants
    other_conv = new_conv(client, headers=other)
    status, _, r = ask_with(client, other_conv, "Check my server", "on", other)
    assert status == 409 and r.json()["detail"]["code"] == "no_capabilities"


def test_a_model_that_writes_json_in_the_answer_still_gets_the_tags_through() -> None:
    f = chat.PassthroughFilter()
    assert (
        f.feed("a [live] b [assumption") + f.feed("] c") + f.flush() == "a [live] b [assumption] c"
    )
    assert json.dumps(chat.strip_markers("a [live] b")) == '"a b"'  # the normal filter strips them


# --- doubt, a slow stand-in machine ----------------------------------------------------------


def test_a_general_docker_question_gets_no_check_and_no_offer(client: TestClient, world) -> None:
    script_check(client)
    conv = new_conv(client)
    _, events, _ = ask_with(
        client, conv, "Schreibe mir bitte in einem Satz, was ein Docker-Container ist."
    )
    assert "tools_start" not in [e for e, _ in events]
    assert "checking" not in events[0][1] and "check_offer" not in events[0][1]


def test_when_in_doubt_the_answer_is_plain_and_the_check_is_offered(
    client: TestClient, world
) -> None:
    script_check(client)
    conv = new_conv(client)
    _, events, _ = ask_with(client, conv, "Warum startet der Container neu?")
    assert "tools_start" not in [e for e, _ in events]
    assert events[0][1]["check_offer"] is True and "checking" not in events[0][1]
    assert events[-1][0] == "done"
    # a forced check carries no offer (it runs), and "off" none either
    _, events, _ = ask_with(client, conv, "Warum startet der Container neu?", "on")
    assert "check_offer" not in events[0][1] and events[0][1]["checking"] == "forced"
    _, events, _ = ask_with(client, conv, "Warum startet der Container neu?", "off")
    assert "check_offer" not in events[0][1]


def test_the_check_rules_keep_general_knowledge_untagged() -> None:
    for compact in (False, True):
        system = chat.system_prompt(compact=compact, check=True)
        assert "never [live]" in system or "never call it [live]" in system


def test_the_slow_check_is_one_or_two_calls_with_a_time_frame_of_its_own() -> None:
    slow, normal = chat_tools.SLOW_CHECK_LIMITS, chat_tools.CHECK_LIMITS
    assert slow.max_tool_calls == 2 and slow.max_tool_calls < normal.max_tool_calls
    assert slow.max_turns < normal.max_turns
    assert slow.time_budget_seconds > normal.time_budget_seconds  # a laptop needs longer a turn
    # no clock on a model answer, a slow machine included: it ends when the model stands still
    assert slow.model_timeout_seconds == normal.model_timeout_seconds == 0
    assert slow.observation_chars < normal.observation_chars  # and gets less to read


def test_a_check_on_the_fallback_machine_is_warned_about_and_held_to_two_calls(
    client: TestClient, world, connect
) -> None:
    _, _, fast = world
    online_node(client, connect, "node-b", loaded=["small"])
    fast.state = "busy_gaming"
    fast.heartbeat()
    eventually(lambda: _node_state(client, "node-a") == "busy_gaming" or _fail())
    looks = [
        json.dumps(
            {
                "action": "tool",
                "node": "vm-docker",
                "capability": "docker.logs",
                "input": {"container": name, "lines": 5},
                "reason": "look",
            }
        )
        for name in ("web", "db", "cache")
    ]
    use(client, *looks, finish("web ran out of memory.", FINDINGS, "raise the memory limit"))
    conv = new_conv(client)
    _, events, _ = ask_with(client, conv, "Prüf mal meine Container")
    meta = events[0][1]
    assert meta["node"] == "node-b" and meta["fallback_from"] == "node-a"
    assert meta["check_max_calls"] == 2  # the browser warns before the check starts
    done = next(d for e, d in events if e == "tools_done")
    assert done["calls"] == 2  # the third look was never made


def _node_state(client: TestClient, name: str) -> str:
    return {n["name"]: n["state"] for n in client.get("/api/nodes", headers=AUTH).json()}[name]


def _fail() -> None:
    raise AssertionError("not yet")


# --- the model decides (E3-04) ----------------------------------------------------------------


class NativeScripted(ScriptedModel):
    """The agent's scripted model, for a model that makes native tool calls."""

    async def tool_mode(self, model: str | None) -> str:
        return "native"

    async def complete(self, messages, model, max_tokens, tools=None):  # type: ignore[no-untyped-def]
        return await super().complete(messages, model, max_tokens)


def use_native_model(client: TestClient, *replies: Any) -> NativeScripted:
    scripted = use(client, *replies)
    scripted.__class__ = NativeScripted
    return scripted  # type: ignore[return-value]


def test_the_model_is_offered_the_function_and_answers_directly_when_it_needs_no_look(
    client: TestClient, world
) -> None:
    _, _, node = world
    use_native_model(client)
    conv = new_conv(client)
    _, events, _ = ask_with(
        client, conv, "Schreibe mir bitte in einem Satz, was ein Docker-Container ist."
    )
    assert "tools_start" not in [e for e, _ in events] and events[-1][0] == "done"
    req = node.tool_requests[0]
    fn = req["tools"][0]["function"]
    assert fn["name"] == "look_at_my_systems" and req["tool_choice"] == "auto"
    # it is told what the function reaches: every group the user may use, not only containers
    assert "Docker containers" in fn["description"] and "host health" in fn["description"]
    system = req["messages"][0]["content"]
    assert "look_at_my_systems" in system and "Never say you have no access" in system


def test_when_the_model_calls_the_function_the_answer_becomes_a_check(
    client: TestClient, world
) -> None:
    _, tnode, node = world
    node.tool_call_fn = lambda req: {"what": "why web restarts"}
    scripted = use_native_model(
        client,
        json.dumps(
            {
                "action": "tool",
                "node": "vm-docker",
                "capability": "docker.logs",
                "input": {"container": "web", "lines": 50},
                "reason": "see why web restarts",
            }
        ),
        finish("web ran out of memory.", FINDINGS, "raise the memory limit"),
    )
    conv = new_conv(client)
    # no keyword of the old heuristic in it: the model decides
    _, events, _ = ask_with(client, conv, "Hast du eine Idee, warum bei mir alles so hakt?")
    kinds = [e for e, _ in events]
    assert kinds[0] == "meta" and "discard" in kinds  # the lead-in "Ich schaue nach." is dropped
    assert kinds.index("discard") < kinds.index("tools_start") < kinds.index("tools_done")
    checking = [d for e, d in events if e == "meta"][-1]
    assert checking["checking"] == "decided"
    after = events[kinds.index("discard") :]  # what the browser keeps: from the discard on
    assert "Ich schaue nach" not in answer_text(after)
    done = events[-1][1]["message"]
    assert done["agent_task_id"] and "Ich schaue nach" not in done["content"]
    # the model's note reaches the check's task
    task_text = scripted.calls[0][1]["content"]
    assert (
        "Hast du eine Idee" in task_text
        and "note on what to find out: why web restarts" in task_text
    )
    assert [c["capability"] for c in tnode.capability_calls] == ["docker.logs"]


def test_a_server_that_refuses_tools_is_asked_again_without_them_and_remembered(
    client: TestClient, world
) -> None:
    _, _, node = world
    node.refuse_tools = True
    use_native_model(client)
    conv = new_conv(client)
    status, events, _ = ask_with(client, conv, "Erzähl mir etwas über Pinguine.")
    assert status == 200 and events[-1][0] == "done"
    assert len(node.tool_requests) == 1  # asked with the function once …
    _, events, _ = ask_with(client, conv, "Und über Robben?")
    assert (
        events[-1][0] == "done" and len(node.tool_requests) == 1
    )  # … and not again for this model


def test_a_model_that_cannot_call_functions_keeps_the_heuristic_and_the_offer(
    client: TestClient, world
) -> None:
    _, _, node = world
    script_check(client)  # the plain scripted model has no tool mode: JSON protocol only
    conv = new_conv(client)
    _, events, _ = ask_with(client, conv, "Warum startet der Container neu?")
    assert events[0][1]["check_offer"] is True and node.tool_requests == []


def test_forced_and_switched_off_checks_do_not_offer_the_function(
    client: TestClient, world
) -> None:
    _, _, node = world
    use_native_model(client)
    conv = new_conv(client)
    ask_with(client, conv, "Erzähl mir etwas über Pinguine.", "off")
    assert node.tool_requests == []


def test_the_assistant_is_told_every_group_it_can_use() -> None:
    from topcoat_hub import tool_groups

    avail = [
        {"capability": c}
        for c in (
            "docker.ps",
            "omada.devices",
            "kuma.monitors",
            "web.search",
            "docs.read",
            "health.check",
        )
    ]
    keys = tool_groups.present(avail)
    assert keys == ["docker", "host", "network", "monitors", "notes", "web"]
    text = chat.system_prompt(
        None,
        None,
        chat.Capabilities(
            tools=tool_groups.labels(keys), knowledge=True, web_search=True, decider=True
        ),
    )
    for want in (
        "Docker containers",
        "Omada",
        "Uptime Kuma",
        "knowledge base",
        "web search",
        "look_at_my_systems",
    ):
        assert want in text, want
    assert "use tools or access devices" not in text  # nothing is denied that is offered
    assert tool_groups.only(avail, ["web"]) == [{"capability": "web.search"}]
    assert tool_groups.only(avail, None) == avail


def test_a_report_card_the_model_wrote_itself_is_cut_from_the_answer() -> None:
    from topcoat_hub.api.chat import REPORT_LEAD, strip_fake_card

    fake = f"Ich schaue nach.\n\n{REPORT_LEAD}\n**Summary:** Suche läuft"
    assert strip_fake_card(fake) == "Ich schaue nach."
    assert strip_fake_card("Normale Antwort") == "Normale Antwort"
    from topcoat_hub.api.chat import starts_with_fake_card

    assert starts_with_fake_card(f"{REPORT_LEAD}\n**Summary:** alles hochgeladen\n\nFertig.")
    assert not starts_with_fake_card("Ich schaue nach.")


def test_a_chat_without_a_check_is_told_not_to_announce_a_search() -> None:
    caps = chat.Capabilities(tools=("shell.read",))
    assert "cannot run anything" in chat.system_prompt(caps=caps)


def test_the_check_has_room_for_a_code_search() -> None:
    assert chat_tools.CHECK_LIMITS.max_tool_calls >= 12


@pytest.mark.parametrize(
    "text",
    [
        "Was steht in Pull Request 106?",
        "Welche PRs sind offen?",
        "Zeig mir die offenen Issues im Repo",
        "Was hat sich im letzten Commit geändert?",
        "Was ist mit #106?",
        "Wie sieht der Quellcode von chat_tools aus?",
    ],
)
def test_a_question_about_the_code_starts_a_check_when_github_is_offered(text: str) -> None:
    assert chat_tools.check_need(text, NODES, github_tool=True) == "check"
    assert chat_tools.check_need(text, NODES) != "check"  # not offered: nothing changes


@pytest.mark.parametrize(
    "text",
    [
        "Wie erstelle ich einen Pull Request?",
        "What is a pull request?",
        "Erkläre mir den Unterschied zwischen merge und rebase in einem Branch",
        "Mach die Farbe #ff00ff und &#123; kaputt",
        "Hallo",
        "Wie ist das Wetter, ich brauche einen Prozess",
    ],
)
def test_general_questions_and_look_alikes_do_not_start_a_github_check(text: str) -> None:
    assert chat_tools.check_need(text, NODES, github_tool=True) != "check"


def _two_machines(client: TestClient, world, connect):
    _, tnode, node = world
    _, other = tools_node(client, connect, "vm-db")
    other.capability_answers["system.info"] = {"hostname": "db", "uptime_seconds": 5}
    look = json.dumps(
        {
            "action": "tool",
            "node": "vm-docker",
            "capability": "docker.logs",
            "input": {"container": "web", "lines": 50},
            "reason": "see why web restarts",
        }
    )
    return tnode, other, node, look


def test_a_check_the_hub_starts_runs_on_the_machine_the_question_names(
    client: TestClient, world, connect
) -> None:
    """The whole way of a check order (O7, live 2026-11-09 "Audio auf node-amd"): a question that
    names a machine starts the check without the model's fields; with two machines in the scope
    the hub names the one in the question, the run sees only it, and "in letzter Zeit" asks for
    days, not the last 24 hours."""
    tnode, other, _, look = _two_machines(client, world, connect)
    scripted = use_native_model(client, look, finish("web ran out of memory.", FINDINGS))
    conv = new_conv(client)
    question = "Warum startet der Container web auf VM-Docker in letzter Zeit immer wieder neu?"
    _, events, _ = ask_with(client, conv, question)
    start = next(d for e, d in events if e == "tools_start")
    assert start["order"]["target"] == "vm-docker" and start["order"]["window"] == "14d"
    task_text = scripted.calls[0][1]["content"]
    assert "Maschine: vm-docker" in task_text and "Zeitraum: 14d" in task_text
    assert "vm-db" not in scripted.calls[0][0]["content"]  # the run sees only its machine
    assert [c["capability"] for c in tnode.capability_calls] == ["docker.logs"]
    assert other.capability_calls == []
    assert events[-1][0] in ("done", "memory")


def test_a_model_order_without_target_keeps_its_period_on_any_machine(
    client: TestClient, world, connect
) -> None:
    """The model calls the function with a period but no machine, and the question names none:
    the order runs on "any" and keeps the period (before, the whole order was dropped)."""
    _, _, node, look = _two_machines(client, world, connect)
    node.tool_call_fn = lambda req: {"what": "why web restarts", "window": "7d"}
    scripted = use_native_model(client, look, finish("web ran out of memory.", FINDINGS))
    conv = new_conv(client)
    _, events, _ = ask_with(client, conv, "Hast du eine Idee, warum bei mir alles so hakt?")
    assert [d for e, d in events if e == "meta"][-1]["checking"] == "decided"
    start = next(d for e, d in events if e == "tools_start")
    assert start["order"]["target"] == "any" and start["order"]["window"] == "7d"
    assert "Zeitraum: 7d" in scripted.calls[0][1]["content"]


def test_a_plain_yes_to_an_offered_check_accepts_it() -> None:
    # live 2026-11-10: "ja bitte" to "soll ich die Docker-Konfiguration prüfen?" got the offer again
    history = [
        ("user", "In welchem Ordner landet die Datei von sabnzbd auf vm-b?"),
        (
            "assistant",
            "Das weiß ich nicht aus dem Kopf. Ich schlage vor, die Docker-Konfiguration des "
            "Containers zu prüfen; soll ich das tun?",
        ),
        ("user", "ja bitte"),
    ]
    accepted = chat_tools.accepted_offer(history)
    assert accepted is not None and accepted.kind == "check"
    # the offer is the task, the question before it is context (live 2026-11-15/17)
    assert accepted.task.startswith("Ich schlage vor, die Docker-Konfiguration")
    assert "for context only: In welchem Ordner landet die Datei" in accepted.task
    for yes in ("Ja", "gerne!", "ok, mach das", "yes please", "Ja, bitte."):
        assert chat_tools.accepted_offer([*history[:2], ("user", yes)]), yes
    # anything else is a new message, and an answer without an offer is no offer
    assert not chat_tools.accepted_offer([*history[:2], ("user", "ja, aber nur die Logs")])
    assert not chat_tools.accepted_offer([*history[:2], ("user", "nein")])
    told = [history[0], ("assistant", "Die Datei landet in /data/complete."), ("user", "ja")]
    assert not chat_tools.accepted_offer(told)


@pytest.mark.parametrize(
    ("offer", "kind"),
    [
        ("Soll ich dir erklären, wie du das Logging aktivierst?", "explain"),
        ("Soll ich dir zeigen, wie das geht?", "explain"),
        ("Möchtest du, dass ich es dir Schritt für Schritt beschreibe?", "explain"),
        ("Shall I explain how to switch it on?", "explain"),
        ("Möchtest du, dass ich den Batch für Aufgabe 1.6 erneut vorbereite?", "check"),
        ("Soll ich die Logs prüfen und dir erklären, was los ist?", "check"),
        ("Soll ich nachsehen?", "check"),
        ("Soll ich die Aufgaben 1.8 bis 1.8.2 anlegen?", "check"),
    ],
)
def test_an_offer_to_explain_is_no_check(offer: str, kind: str) -> None:
    history = [
        ("user", "Was ist mit dem WLAN?"),
        ("assistant", f"Kein Fehler. {offer}"),
        ("user", "ja"),
    ]
    accepted = chat_tools.accepted_offer(history)
    assert accepted is not None and accepted.kind == kind


def test_a_yes_to_an_offer_to_explain_answers_without_a_check(client: TestClient, world) -> None:
    # live 2026-11-17: the same diagnosis ran again and ended with the same offer
    _, tnode, node = world
    node.stream_text_fn = lambda req: "web läuft. Soll ich dir erklären, wie du die Logs liest?"
    conv = new_conv(client)
    ask_with(client, conv, "Was macht der Container web gerade?", "off")
    seen: list[str] = []

    def answer(req: dict) -> str:
        seen.append(str(req["messages"][-1]["content"]))
        return "So liest du die Logs: docker logs web."

    node.stream_text_fn = answer
    _, events, _ = ask_with(client, conv, "ja bitte")
    assert "tools_start" not in [e for e, _ in events]
    assert tnode.capability_calls == []
    assert "The person just said yes to your offer" in seen[-1]
    assert "wie du die Logs liest" in seen[-1]


def test_a_yes_to_an_offer_starts_the_check_in_the_chat(client: TestClient, world) -> None:
    _, tnode, node = world
    offer = "Das weiß ich nicht aus dem Kopf. Soll ich die Logs von web prüfen?"
    node.stream_text_fn = lambda req: offer
    scripted = use_native_model(
        client,
        json.dumps(
            {
                "action": "tool",
                "node": "vm-docker",
                "capability": "docker.logs",
                "input": {"container": "web", "lines": 20},
                "reason": "see",
            }
        ),
        finish("web ran out of memory.", FINDINGS),
    )
    conv = new_conv(client)
    ask_with(client, conv, "Was macht der Container web gerade?", "off")  # the offer, no check
    node.stream_text_fn = None
    _, events, _ = ask_with(client, conv, "ja bitte")
    assert "tools_start" in [e for e, _ in events]
    task = str(scripted.calls[0][1]["content"])
    # the offer leads, the earlier question follows as context
    assert task.index("Soll ich die Logs von web prüfen?") < task.index(
        "Was macht der Container web gerade?"
    )
    assert [c["capability"] for c in tnode.capability_calls] == ["docker.logs"]


def test_a_question_naming_a_model_node_is_a_check_not_an_offer(client: TestClient, world) -> None:
    """node-a serves models and offers no tool: its name still makes a state question a check
    (live 2026-11-11: "Wie ist der Stand von node-nvidia gerade?" got "Soll ich nachsehen?")."""
    script_check(client)
    conv = new_conv(client)
    _, events, _ = ask_with(client, conv, "Wie ist der Stand von node-a gerade?")
    assert events[0][1].get("checking") == "detected"


def test_the_decider_looks_up_facts_about_the_world_when_the_web_is_there() -> None:
    with_web = chat_tools.decider_tool(("the web",), web=True)["function"]["description"]
    without = chat_tools.decider_tool(("notes",))["function"]["description"]
    assert "look it up instead of guessing" in with_web
    assert "general knowledge" not in with_web
    assert "Never say you searched" in with_web
    assert "general knowledge" in without


@pytest.mark.parametrize(
    ("text", "fact"),
    [
        ("wann haben Ian McKellen und Patrick Stewart sich kennengelernt?", True),
        ("Wer hat den Film Alien gedreht?", True),
        ("Wie alt ist Patrick Stewart?", True),
        ("When did the Berlin Wall fall?", True),
        ("Wann soll ich die Backups machen?", False),
        ("Wie alt bin ich laut meinen Notizen?", False),
        ("Wie installiere ich Docker?", False),
        ("Hallo", False),
    ],
)
def test_a_fact_of_the_world_is_looked_up_at_once(text: str, fact: bool) -> None:
    assert chat_tools.fact_question(text) is fact


def test_a_check_that_stopped_looking_with_sourced_findings_is_not_cut_short() -> None:
    from topcoat_hub.report import cut_short

    sourced = {"findings": [{"claim": "x", "evidence": ["E5"], "tag": "web"}]}
    assert not cut_short("no_progress", sourced)
    assert cut_short("no_progress", {"findings": [{"claim": "x", "evidence": []}]})
    assert cut_short("time_budget", sourced)
    assert not cut_short(None, {})


@pytest.mark.parametrize(
    ("text", "here"),
    [
        ("Worum geht es auf dieser Seite?", True),
        ("Was steht hier drin?", True),
        ("What does this task still need?", True),
        ("Wann kam Alien in die Kinos?", False),
        ("Wie installiere ich das hier?", False),
    ],
)
def test_a_question_about_the_open_window_is_read_at_once(text: str, here: bool) -> None:
    assert chat_tools.refers_to_place(text) is here
