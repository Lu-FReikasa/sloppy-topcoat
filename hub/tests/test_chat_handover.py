"""The chat model may propose a hand-over (docs/DESKTOP.md, D72): offered as a function next to the
check, it prepares a dossier for the person and sends nothing; the answer says where it is."""

from typing import Any

from fastapi.testclient import TestClient

from tests.conftest import AUTH
from tests.test_chat_tools import ask_with, use_native_model, world  # noqa: F401 - the fixture
from topcoat_hub import chat_tools


def chat_conversation(client: TestClient) -> str:
    return client.post("/api/conversations", headers=AUTH, json={"application": "chat"}).json()[
        "id"
    ]


def quiet_dossier(client: TestClient) -> None:
    async def ask(system: str, user_text: str, max_tokens: int = 700) -> str | None:
        return None  # the dossier is written from the request itself

    client.app.state.handovers._ask = ask


def test_the_chat_model_is_offered_a_hand_over_to_the_other_programs(
    client: TestClient,
    world,  # noqa: F811
) -> None:
    _, _, node = world
    use_native_model(client)
    ask_with(client, chat_conversation(client), "Wie spät ist es in Tokio?")
    offered = {t["function"]["name"]: t["function"] for t in node.tool_requests[0]["tools"]}
    assert "hand_over" in offered
    targets = offered["hand_over"]["parameters"]["properties"]["to"]["enum"]
    assert "admin" in targets and "chat" not in targets  # not to itself
    assert "Docker containers" in offered["hand_over"]["description"]


def test_calling_it_prepares_a_dossier_and_sends_nothing(
    client: TestClient,
    world,  # noqa: F811
) -> None:
    _, _, node = world
    use_native_model(client)
    quiet_dossier(client)
    node.tool_call_fn = lambda req: {
        "__name": "hand_over",
        "to": "admin",
        "request": "Warum startet der Container web dauernd neu?",
    }
    conv = chat_conversation(client)
    status, events, _ = ask_with(client, conv, "Warum startet mein Container web dauernd neu?")
    kinds = [e for e, _ in events]
    assert status == 200 and kinds[-1] == "done" and "handover" in kinds
    assert "tools_start" not in kinds  # no check ran in Chat
    proposed: dict[str, Any] = next(d for e, d in events if e == "handover")
    assert proposed["status"] == "proposed" and proposed["to_application"] == "admin"
    assert proposed["dossier"]["question"] == "Warum startet der Container web dauernd neu?"
    done = next(d for e, d in events if e == "done")["message"]
    assert "Verwaltung" in done["content"] and "abschickst" in done["content"]
    # nothing went out: no task, the hand-over waits for the click
    listed = client.get("/api/handovers", headers=AUTH).json()["handovers"]
    assert [h["status"] for h in listed] == ["proposed"] and listed[0]["task_id"] is None


def test_a_target_the_model_made_up_is_not_prepared(client: TestClient, world) -> None:  # noqa: F811
    _, _, node = world
    use_native_model(client)
    node.tool_call_fn = lambda req: {"__name": "hand_over", "to": "nowhere"}
    _, events, _ = ask_with(client, chat_conversation(client), "Mach etwas Unmögliches")
    assert "handover" not in [e for e, _ in events]
    assert client.get("/api/handovers", headers=AUTH).json()["handovers"] == []


def test_the_arguments_are_read_safely() -> None:
    assert chat_tools.handover_from('{"to": "admin", "request": "a  b"}') == ("admin", "a b")
    assert chat_tools.handover_from("not json") == ("", "")
    assert chat_tools.handover_from('{"to": 3}') == ("", "")


def test_a_program_is_offered_for_what_it_adds_to_the_conversation(
    client: TestClient,
    world,  # noqa: F811
) -> None:
    """Chat has the notes and the web: Recherche adds no tool, but its kind of work, a thorough
    research filed as a report (2026-11-13: a hand-over "to Research" went to Projekte because
    Research was never offered); Verwaltung is offered for its servers. The web is not named as
    something another program adds."""
    _, _, node = world
    use_native_model(client)
    ask_with(client, chat_conversation(client), "Wie spät ist es in Tokio?")
    offered = {t["function"]["name"]: t["function"] for t in node.tool_requests[0]["tools"]}
    targets = offered["hand_over"]["parameters"]["properties"]["to"]["enum"]
    assert "research" in targets and "admin" in targets
    assert "thorough research" in offered["hand_over"]["description"]
    assert "the web" not in offered["hand_over"]["description"]


def test_a_project_question_in_chat_becomes_a_hand_over_not_a_hopeless_check(
    client: TestClient,
    world,  # noqa: F811
) -> None:
    """Live 2026-11-14: Chat has no project tools; the model chose to look itself, the check read
    notes and wiki for "the tasks of project X", repeated itself and said it did not know."""
    _, _, node = world
    use_native_model(client)
    quiet_dossier(client)
    node.tool_call_fn = lambda req: {"__name": "look_at_my_systems", "what": "Aufgaben lesen"}
    conv = chat_conversation(client)
    question = "Welche Aufgaben hat das Projekt TEST Messlatte?"
    status, events, _ = ask_with(client, conv, question)
    kinds = [e for e, _ in events]
    assert status == 200 and kinds[-1] == "done" and "handover" in kinds
    assert "tools_start" not in kinds  # no check without the tools it needs
    proposed: dict[str, Any] = next(d for e, d in events if e == "handover")
    assert proposed["to_application"] == "project"
    done = next(d for e, d in events if e == "done")["message"]
    assert "Projekte" in done["content"]


def test_only_a_project_question_without_project_tools_is_redirected() -> None:
    from types import SimpleNamespace

    from topcoat_hub import tool_groups
    from topcoat_hub.chat_engine.answer import project_owner

    targets = (("project", ", ".join(tool_groups.labels(["project", "notes"]))),)

    def turn(text: str, groups: list[str] | None) -> Any:
        return SimpleNamespace(
            scope={"groups": groups}, last_user_text=text, handover_targets=targets
        )

    assert project_owner(turn("Welche Aufgaben hat das Projekt X?", ["notes", "wiki"])) == "project"
    assert project_owner(turn("Welche Aufgaben hat das Projekt X?", ["project", "notes"])) is None
    assert project_owner(turn("Welche Aufgaben hat das Projekt X?", None)) is None
    assert project_owner(turn("Wie spät ist es in Tokio?", ["notes"])) is None


def test_a_check_the_hub_would_start_for_a_project_question_becomes_a_hand_over(
    client: TestClient,
    world,  # noqa: F811
    monkeypatch,  # type: ignore[no-untyped-def]
) -> None:
    """The second way into a check: the hub starts it itself (live: Chat has notes and wiki, so it
    "detected" a check). Live 2026-11-14 the redirect of the model's own call did not cover it."""
    from topcoat_hub.chat_engine import answer as chat_api

    use_native_model(client)
    quiet_dossier(client)

    async def detected(request, user, turn, tools, routable):  # type: ignore[no-untyped-def]
        turn.in_program = True
        return "detected", None

    monkeypatch.setattr(chat_api, "_plan_check", detected)
    conv = chat_conversation(client)
    status, events, _ = ask_with(client, conv, "Welche Aufgaben hat das Projekt X?")
    kinds = [e for e, _ in events]
    assert status == 200 and kinds[-1] == "done" and "handover" in kinds
    assert "tools_start" not in kinds
    proposed: dict[str, Any] = next(d for e, d in events if e == "handover")
    assert proposed["to_application"] == "project"
