"""Honesty by mechanism, part (b) (E3-07 D4): a finding holds for the node that gave it. "There is
no Plex" is only said after every node that could be asked was asked; otherwise the hub says "not
found on <node>, the others were not checked". For a question about a service the agent asks the
monitors and the notes first. The case: "Läuft Plex?" — the Host-a node had no Plex, the answer
said Plex does not exist, and Plex ran on a machine without a tool node (with a monitor for it)."""

import json
from typing import Any

import pytest
from fastapi.testclient import TestClient

from tests.fakenode import online_node
from tests.test_agent import ScriptedModel, finish, use
from tests.test_chat import new_conv, use_model
from tests.test_chat_tools import ask_with
from tests.test_tools import tools_node
from topcoat_hub import agent_prompts as p
from topcoat_hub import chat, report

# --- the words -----------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        "Plex gibt es nicht.",
        "Einen Container namens plex gibt es hier nicht",
        "Plex existiert nicht auf diesem Server",
        "Der Dienst läuft nicht.",
        "Plex wurde nicht gefunden",
        "Plex ist nicht installiert",
        "Es gibt nirgends einen Plex-Container",
        "Keinen Plex-Container gefunden",
        "Plex does not exist",
        "There is no plex container",
        "Plex is not running",
        "not found",
    ],
)
def test_sentences_that_say_something_is_absent_are_recognised(text: str) -> None:
    assert chat.says_nothing_exists(text)


@pytest.mark.parametrize(
    "text",
    [
        "Plex läuft auf dem echo.",
        "Der Container web läuft seit drei Tagen.",
        "Plex is running on node-b.",
        "Alle Container sind gesund.",
        "Fertig, das Wichtigste steht im Bericht.",
    ],
)
def test_other_sentences_are_left_alone(text: str) -> None:
    assert not chat.says_nothing_exists(text)


def test_a_claim_of_absence_names_the_nodes_that_were_asked_and_the_ones_that_were_not() -> None:
    claim = "Plex gibt es nicht."
    assert chat.qualify_claim(claim, "de", ["tools-a"], []) == claim  # everyone was asked
    assert chat.qualify_claim("Plex läuft.", "de", ["tools-a"], ["tools-b"]) == "Plex läuft."
    assert chat.qualify_claim(claim, "de", ["tools-a"], ["tools-b", "tools-c"]) == (
        "Plex gibt es nicht — nur auf tools-a geprüft; nicht geprüft: tools-b, tools-c"
    )
    assert chat.qualify_claim("Plex does not exist.", "en", ["tools-a"], ["tools-b"]) == (
        "Plex does not exist — checked on tools-a only; not checked: tools-b"
    )


def test_the_closing_text_cannot_say_nothing_exists_without_the_scope() -> None:
    text = "Plex gibt es nicht. Sonst ist alles ruhig."
    assert chat.guard_scope(text, "de", ["tools-a"], ["tools-b"]) == (
        "Auf tools-a nicht gefunden; nicht geprüft: tools-b. Sonst ist alles ruhig."
    )
    assert chat.guard_scope("Plex does not exist.", "en", ["a"], ["b"]) == (
        "Not found on a; not checked: b."
    )
    assert chat.guard_scope(text, "de", ["tools-a"], []) == text  # everyone was asked
    assert chat.guard_scope("Alles ruhig.", "de", ["tools-a"], ["tools-b"]) == "Alles ruhig."


# --- the agent ---------------------------------------------------------------------------------


TOOLS = [
    {"node": "tools-a", "capability": "docker.ps"},
    {"node": "kuma-node", "capability": "kuma.monitors"},
    {"node": "knowledge", "capability": "knowledge.search"},
]


@pytest.mark.parametrize(
    "prompt",
    [
        "Läuft Plex?",
        "Gibt es Plex bei mir?",
        "Is plex running?",
        "Wo läuft Nextcloud?",
        "Plex online?",
    ],
)
def test_a_question_about_a_service_is_recognised(prompt: str) -> None:
    assert p.service_question(prompt)


@pytest.mark.parametrize(
    "prompt",
    ["Warum startet web neu?", "Wie viel Platz ist auf der Platte frei?", "Prüf meine Container"],
)
def test_other_tasks_get_no_service_hint(prompt: str) -> None:
    assert not p.service_question(prompt)
    assert p.service_hint(prompt, TOOLS) == ""


def test_the_hint_names_only_the_tools_that_exist_and_puts_the_machines_last() -> None:
    hint = p.service_hint("Läuft Plex?", TOOLS)
    assert "kuma.monitors" in hint and "knowledge.search" in hint
    assert (
        hint.index("kuma.monitors") < hint.index("knowledge.search") < hint.index("single machines")
    )
    assert "no tool node" in hint
    only_notes = p.service_hint("Läuft Plex?", [TOOLS[0], TOOLS[2]])
    assert "knowledge.search" in only_notes and "kuma.monitors" not in only_notes
    assert p.service_hint("Läuft Plex?", [TOOLS[0]]) == ""  # nothing to ask first
    message = p.task_message("Läuft Plex?", __import__("datetime").datetime(2026, 10, 2), hint)
    assert message.startswith("Task: Läuft Plex?") and message.endswith(hint)


def test_both_prompts_tell_the_agent_that_a_result_holds_for_its_node() -> None:
    tools = [{"node": "tools-a", "capability": "docker.ps", "input_schema": {}}]
    for prompt in (p.system_prompt(tools), p.system_prompt_native(tools)):
        assert "holds for the node that returned it" in prompt
        # a machine without a tool node is not "unreachable": the hub knows its state (AGT-03)
        assert "cannot be asked for logs or containers" in prompt
        assert "ask topcoat.state with what=nodes" in prompt


def test_the_coverage_of_a_diagnosis_and_its_claims() -> None:
    diagnosis = {
        "summary": "s",
        "findings": [
            {"claim": "Plex gibt es nicht.", "tag": "live", "evidence": ["E1"]},
            {"claim": "Der Container web läuft.", "tag": "live", "evidence": ["E1"]},
        ],
    }
    p.scope_findings(diagnosis, ["tools-a"], ["tools-b"], "de")
    assert diagnosis["coverage"] == {"asked": ["tools-a"], "not_asked": ["tools-b"]}
    claims = [f["claim"] for f in diagnosis["findings"]]
    assert claims == [
        "Plex gibt es nicht — nur auf tools-a geprüft; nicht geprüft: tools-b",
        "Der Container web läuft.",
    ]
    assert diagnosis["findings"][0]["scoped"] is True and "scoped" not in diagnosis["findings"][1]


def test_the_card_shows_which_nodes_were_asked() -> None:
    from types import SimpleNamespace

    task = SimpleNamespace(
        id="11111111-1111-4111-8111-111111111111",
        status="done",
        stop_reason=None,
        tool_calls=1,
        diagnosis={
            "summary": "s",
            "findings": [],
            "coverage": {"asked": ["tools-a"], "not_asked": ["tools-b"]},
        },
    )
    card = report.build(task, [])
    assert card is not None and card["coverage"] == {"asked": ["tools-a"], "not_asked": ["tools-b"]}
    assert "**Checked on:** tools-a; **not checked:** tools-b" in report.text(card)


# --- through the chat: the Plex case ------------------------------------------------------------


@pytest.fixture
def two_nodes(client: TestClient, connect: Any):
    """Two tool nodes (neither knows Plex) and a model node that answers the chat."""
    _, first = tools_node(client, connect, "vm-docker")
    _, second = tools_node(client, connect, "vm-two")
    for node in (first, second):
        node.capability_answers["docker.ps"] = {"containers": [{"name": "web", "state": "running"}]}
    model = online_node(client, connect, "node-a", loaded=["small"])
    use_model(client)
    return first, second, model


def look_at(node: str) -> str:
    return json.dumps(
        {
            "action": "tool",
            "node": node,
            "capability": "docker.ps",
            "input": {},
            "reason": f"is plex on {node}",
        }
    )


def plex_finding(client: TestClient, *nodes: str) -> ScriptedModel:
    findings = [{"claim": "Plex gibt es nicht.", "tag": "live", "evidence": ["E1"]}]
    return use(client, *[look_at(n) for n in nodes], finish("Plex fehlt.", findings))


def card_of(events: list[tuple[str, dict[str, Any]]]) -> dict[str, Any]:
    return next(d for e, d in events if e == "report")["message"]["report"]


def test_plex_does_not_exist_is_only_said_with_the_nodes_it_was_looked_for_on(
    client: TestClient, two_nodes: Any
) -> None:
    _, _, model = two_nodes
    plex_finding(client, "vm-docker")  # the agent looked at one of the two nodes only
    model.stream_text_fn = lambda body: "Plex gibt es nicht. Sonst ist alles ruhig."
    conv = new_conv(client)
    status, events, _ = ask_with(client, conv, "Läuft Plex gerade?", "on")
    assert status == 200
    card = card_of(events)
    assert card["coverage"] == {"asked": ["vm-docker"], "not_asked": ["vm-two"]}
    assert card["findings"][0]["claim"] == (
        "Plex gibt es nicht — nur auf vm-docker geprüft; nicht geprüft: vm-two"
    )
    assert (
        "**not checked:** vm-two"
        in next(d for e, d in events if e == "report")["message"]["content"]
    )
    closing = events[-1][1]["message"]["content"]
    assert closing == "Auf vm-docker nicht gefunden; nicht geprüft: vm-two. Sonst ist alles ruhig."
    # the model that wrote the closing was told what the results hold for
    from tests.test_chat import sent_messages

    assert "NOT asked: vm-two" in sent_messages(model)[-1]["content"]


def test_with_every_node_asked_the_claim_stands_as_it_is(
    client: TestClient, two_nodes: Any
) -> None:
    _, _, model = two_nodes
    plex_finding(client, "vm-docker", "vm-two")
    model.stream_text_fn = lambda body: "Plex gibt es nicht."
    conv = new_conv(client)
    _, events, _ = ask_with(client, conv, "Läuft Plex gerade?", "on")
    card = card_of(events)
    assert card["coverage"] == {"asked": ["vm-docker", "vm-two"], "not_asked": []}
    assert card["findings"][0]["claim"] == "Plex gibt es nicht."  # asked everywhere: it may say so
    assert events[-1][1]["message"]["content"] == "Plex gibt es nicht."


def test_the_first_message_of_a_service_question_asks_for_the_monitors_first(
    client: TestClient, two_nodes: Any
) -> None:
    model = plex_finding(client, "vm-docker")
    conv = new_conv(client)
    ask_with(client, conv, "Läuft Plex gerade?", "on")
    first = model.calls[0]
    task = next(m["content"] for m in first if m["role"] == "user")
    # the test nodes offer neither Uptime Kuma nor notes: no hint naming tools that are not there
    assert "kuma.monitors" not in task
    # the hub's inventory is always there: the hint names it first, and only it
    assert "Hint:" in task and "inventory.list" in task
    assert any("holds for the node that returned it" in m["content"] for m in first)


def test_an_unrelated_check_is_not_touched(client: TestClient, two_nodes: Any) -> None:
    _, _, model = two_nodes
    use(
        client,
        look_at("vm-docker"),
        finish("web läuft.", [{"claim": "web läuft", "tag": "live", "evidence": ["E1"]}]),
    )
    model.stream_text_fn = lambda body: "Alles ruhig, web läuft."
    conv = new_conv(client)
    _, events, _ = ask_with(client, conv, "Prüf meine Container", "on")
    card = card_of(events)
    assert card["findings"][0]["claim"] == "web läuft"  # not a claim of absence: unchanged
    assert events[-1][1]["message"]["content"] == "Alles ruhig, web läuft."
