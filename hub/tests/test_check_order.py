"""The fixed order of a server check: what the chat's fields turn into (pruefauftrag T3.2/T3.3)."""

from topcoat_hub.chat_tools import order_prompt
from topcoat_hub.check_order import Order, OrderProblem, resolve


def _cap(node: str, capability: str) -> dict:
    return {"node": node, "capability": capability}


SCOPE = [
    _cap("node-amd", "windows.events"),
    _cap("node-amd", "windows.services"),
    _cap("node-amd", "topcoat.state"),
    _cap("node-amd", "docs.search"),
    _cap("tools-host", "docker.ps"),
    _cap("tools-host", "logs.search"),
]


def test_a_windows_target_gets_the_host_group_and_the_always_groups():
    order = resolve({"target": "node-amd", "tools": ["docker"]}, SCOPE)
    assert isinstance(order, Order)
    assert order.target == "node-amd"
    # docker is not on this machine: dropped, and the drop is written down
    assert "host" in order.tools and "docker" not in order.tools
    assert "topcoat" in order.tools and "notes" in order.tools
    assert any("docker" in c for c in order.corrections)


def test_an_empty_group_choice_falls_back_to_the_machine_kind():
    order = resolve({"target": "node-amd"}, SCOPE)
    assert isinstance(order, Order)
    assert "host" in order.tools
    assert order.result == "diagnosis" and order.window == "24h"


def test_a_target_outside_the_scope_is_a_problem_not_a_run():
    problem = resolve({"target": "node-nvidia", "tools": ["host"]}, SCOPE)
    assert isinstance(problem, OrderProblem)
    assert "not in this chat's scope" in problem.reason


def test_the_scope_is_never_widened_by_the_order():
    # the order names a group the scope's machine does not have; the result is still inside SCOPE
    order = resolve({"target": "tools-host", "tools": ["host", "network"]}, SCOPE)
    assert isinstance(order, Order)
    assert "network" not in order.tools  # not on that machine in the scope, so it never comes back
    assert "host" in order.tools  # logs.search is part of host there


def test_a_bad_window_or_result_is_corrected_not_refused():
    order = resolve({"target": "node-amd", "window": "forever", "result": "poem"}, SCOPE)
    assert isinstance(order, Order)
    assert order.window == "24h" and order.result == "diagnosis"
    assert len(order.corrections) == 2


def test_a_long_window_is_kept_within_thirty_days():
    order = resolve({"target": "node-amd", "window": "14d"}, SCOPE)
    assert isinstance(order, Order) and order.window == "14d"
    order = resolve({"target": "node-amd", "window": "90d"}, SCOPE)
    assert isinstance(order, Order) and order.window == "24h"


def test_any_target_uses_the_whole_scope_and_goal_is_cut():
    order = resolve({"target": "any", "goal": "x" * 500}, SCOPE)
    assert isinstance(order, Order)
    assert len(order.goal) == 300
    assert "host" in order.tools and "docker" in order.tools


def test_the_order_prompt_states_the_fields_and_the_naming_rule():
    order = resolve(
        {"target": "node-amd", "goal": "Audio problems?", "window": "14d", "result": "table"}, SCOPE
    )
    assert isinstance(order, Order)
    text = order_prompt(order, "Ich hatte Probleme mit dem Audio.", [("user", "x")])
    for line in (
        "Ziel: Audio problems?",
        "Zeitraum: 14d",
        "Ergebnisform: Aussage | Beleg",
        "Maschine: node-amd",
    ):
        assert line in text
    assert "Nenne in der Zusammenfassung nur Maschinen" in text


def test_only_the_table_form_names_an_ergebnisform():
    # the agent's rule turns a named form into a table with its keys: a diagnosis must not name one
    for result, expected in (
        ("diagnosis", "Ergebnis: Diagnose"),
        ("yesno", "„Ja.“ oder „Nein.“"),
    ):
        order = resolve({"target": "node-amd", "result": result}, SCOPE)
        assert isinstance(order, Order)
        text = order_prompt(order, "Läuft der Audiodienst?", [])
        assert expected in text and "Ergebnisform" not in text


def test_a_table_result_reaches_the_card_its_text_and_the_check_block():
    from types import SimpleNamespace

    from topcoat_hub import report
    from topcoat_hub.chat_tools import CheckResult, check_block
    from topcoat_hub.check_order import table_markdown

    rows = [
        {"Aussage": "Audiosrv läuft", "Beleg": "E2"},
        {"Aussage": "Ereignis 225 | audiodg", "Beleg": "E3"},
    ]
    md = table_markdown(rows)
    assert md.splitlines()[0] == "| Aussage | Beleg |"
    assert r"Ereignis 225 \| audiodg" in md  # a pipe in a value does not break the table
    assert (
        table_markdown("no table") == ""
        and table_markdown([{"a": i} for i in range(30)]).count("\n") == 21
    )  # header, rule and at most twenty rows
    task = SimpleNamespace(
        id="t1",
        status="done",
        stop_reason=None,
        tool_calls=2,
        diagnosis={"summary": "Der Dienst läuft.", "findings": [], "table": rows},
    )
    card = report.build(task, [])
    assert card is not None and card["table"] == rows
    assert "| Aussage | Beleg |" in report.text(card)
    block = check_block(CheckResult(task=task, evidence=[]), 4000)
    assert "Result table of the check" in block and "| Audiosrv läuft | E2 |" in block


def test_only_the_free_chat_inherits_the_last_server_choice():
    from topcoat_hub.chat_store import is_free_chat

    assert is_free_chat(None) and is_free_chat("") and is_free_chat("chat")
    assert not is_free_chat("admin") and not is_free_chat("project")


def test_a_run_of_an_order_is_cut_to_its_machine_and_groups():
    from topcoat_hub.check_order import scope_for

    order = resolve({"target": "node-amd"}, SCOPE)
    assert isinstance(order, Order)
    cut = scope_for(order, {"nodes": None, "groups": None, "internet": False})
    assert cut["nodes"] == ["node-amd"] and cut["groups"] == list(order.tools)
    # a chat that already narrowed the nodes keeps its narrowing: the order never widens it
    cut = scope_for(order, {"nodes": ["tools-host"], "groups": None, "internet": False})
    assert cut["nodes"] == []
    anyway = resolve({"target": "any"}, SCOPE)
    assert isinstance(anyway, Order)
    assert scope_for(anyway, {"nodes": None, "groups": None})["nodes"] is None


def test_the_order_fields_are_offered_only_with_targets():
    from topcoat_hub.chat_tools import decider_tool

    plain = decider_tool(("docker",))["function"]["parameters"]["properties"]
    assert set(plain) == {"what"}
    fixed = decider_tool(("docker",), ("node-amd",), ("host",))["function"]["parameters"][
        "properties"
    ]
    assert {"target", "tools", "goal", "result", "window"} <= set(fixed)
    assert fixed["target"]["enum"] == ["node-amd", "any"]


def test_a_call_without_fixed_fields_keeps_the_old_path():
    from types import SimpleNamespace

    from topcoat_hub.chat_engine.answer import _order_of

    turn = SimpleNamespace(decide_available=tuple(SCOPE), last_user_text="Wie ist der Stand?")
    order, focus = _order_of(turn, '{"what": "disk space"}')
    assert order is None and focus == "disk space"
    order, _ = _order_of(turn, '{"target": "node-amd", "what": "audio"}')
    assert isinstance(order, Order) and order.target == "node-amd"
    order, note = _order_of(turn, '{"target": "node-nvidia"}')
    assert order is None and "not in this chat's scope" in note


def test_the_activity_record_of_a_check_keeps_its_order():
    from topcoat_hub.activity import ActivityLog, CheckTrace
    from topcoat_hub.check_order import card

    order = resolve({"target": "node-amd", "goal": "Audio?", "window": "14d"}, SCOPE)
    assert isinstance(order, Order)
    log = ActivityLog()
    trace = CheckTrace(log)
    trace.event("tools_start", {"task_id": "t1", "order": card(order)})
    data = log._items[-1]["data"]  # the check's item: the only one this trace wrote
    assert data["order"]["target"] == "node-amd"
    assert data["order"]["window"] == "14d" and data["order"]["mode"] == "read"
    assert data["task_id"] == "t1"


def test_with_one_machine_in_scope_the_hub_names_it_when_the_model_does_not():
    from types import SimpleNamespace

    from topcoat_hub.chat_engine.answer import _order_of

    one = [_cap("node-amd", "windows.events"), _cap("node-amd", "topcoat.state")]
    turn = SimpleNamespace(decide_available=tuple(one), last_user_text="Audio?")
    order, _ = _order_of(turn, '{"what": "audio", "window": "14d"}')
    assert isinstance(order, Order) and order.target == "node-amd" and order.window == "14d"


def test_a_question_about_a_longer_time_widens_the_default_window():
    order = resolve(
        {"target": "node-amd", "window": "24h"},
        SCOPE,
        "Ich hatte in letzter Zeit immer wieder Probleme mit dem Audio",
    )
    assert isinstance(order, Order) and order.window == "14d"
    assert any("longer time" in c for c in order.corrections)
    # an explicit choice of the model stays
    explicit = resolve({"target": "node-amd", "window": "2d"}, SCOPE, "in letzter Zeit")
    assert isinstance(explicit, Order) and explicit.window == "2d"
    # a plain question keeps the default
    plain = resolve({"target": "node-amd"}, SCOPE, "Wie ist der Stand?")
    assert isinstance(plain, Order) and plain.window == "24h"


def test_without_a_target_the_hub_takes_the_machine_the_question_names():
    from types import SimpleNamespace

    from topcoat_hub.chat_engine.answer import _order_of

    # two machines in the scope; the question names one of them in its own spelling
    turn = SimpleNamespace(
        decide_available=tuple(SCOPE), last_user_text="Probleme mit dem Audio auf Node-AMD"
    )
    order, _ = _order_of(turn, '{"what": "audio"}')
    assert isinstance(order, Order) and order.target == "node-amd"
    # the hub starts the check itself (no fields at all): the named machine still counts
    order, _ = _order_of(turn, "{}")
    assert isinstance(order, Order) and order.target == "node-amd"


def test_without_a_target_the_other_fields_are_kept_on_any_machine():
    from types import SimpleNamespace

    from topcoat_hub.chat_engine.answer import _order_of

    turn = SimpleNamespace(decide_available=tuple(SCOPE), last_user_text="Läuft alles?")
    order, _ = _order_of(turn, '{"what": "state", "window": "7d", "tools": ["docker"]}')
    assert isinstance(order, Order)
    assert order.target == "any" and order.window == "7d" and "docker" in order.tools
    # nothing fixed and nothing named: the old path, as before
    order, _ = _order_of(turn, '{"what": "state"}')
    assert order is None


def test_a_question_about_topcoat_itself_gets_no_machine():
    from types import SimpleNamespace

    from topcoat_hub.chat_engine.answer import _order_of

    one = [_cap("node-amd", "windows.events"), _cap("node-amd", "topcoat.state")]
    question = "Ich würde gerne die Namen von Node-AMD hier in Topcoat ändern. Geht das?"
    turn = SimpleNamespace(decide_available=tuple(one), last_user_text=question)
    order, _ = _order_of(turn, '{"what": "rename", "tools": ["topcoat"]}')
    assert isinstance(order, Order) and order.target == "any"
    order, _ = _order_of(turn, '{"what": "rename"}')
    assert order is None


def test_a_name_is_taken_only_when_exactly_one_machine_matches():
    from topcoat_hub.check_order import named_machine

    machines = ["node-amd", "node-nvidia", "tools-host"]
    assert named_machine("audio on NODE AMD", machines) == "node-amd"
    assert named_machine("node-amd und node-nvidia", machines) is None
    assert named_machine("wie geht es dir", machines) is None


def test_a_question_that_asks_for_a_table_gets_the_table_form():
    order = resolve({"target": "node-amd"}, SCOPE, "Zeig mir als Tabelle: Welche Dienste stehen?")
    assert isinstance(order, Order) and order.result == "table"
    assert any("table" in c for c in order.corrections)
    # the model's own choice stays
    kept = resolve({"target": "node-amd", "result": "yesno"}, SCOPE, "als Tabelle bitte")
    assert isinstance(kept, Order) and kept.result == "yesno"
    plain = resolve({"target": "node-amd"}, SCOPE, "Welche Dienste stehen still?")
    assert isinstance(plain, Order) and plain.result == "diagnosis"


def test_the_card_shows_a_finding_s_quote_and_a_misquote_stays_with_the_assumption():
    from types import SimpleNamespace

    from topcoat_hub import report

    task = SimpleNamespace(
        id="t1",
        status="done",
        stop_reason=None,
        tool_calls=1,
        diagnosis={
            "summary": "s",
            "findings": [
                {"claim": "a", "tag": "live", "evidence": ["E1"], "quote": "Ereignis 225"},
                {"claim": "b", "tag": "live", "evidence": ["E1"], "unquoted": True},
                {
                    "claim": "c",
                    "tag": "assumption",
                    "evidence": [],
                    "downgraded": True,
                    "misquoted": "x y z",
                },
            ],
        },
    )
    card = report.build(task, [])
    assert card is not None
    assert card["findings"][0]["quote"] == "Ereignis 225" and card["findings"][1]["unquoted"]
    assert card["assumptions"][0]["misquoted"] == "x y z"


def test_a_projekte_check_is_framed_as_a_change_not_as_a_diagnosis():
    # live 2026-11-18: "split task 3.1.1" reached the run as "Zeitraum: 24h, Ergebnis: Diagnose";
    # it looked for facts for six minutes and prepared nothing
    order = resolve({"target": "node-amd", "goal": "3.1.1 teilen", "result": "diagnosis"}, SCOPE)
    assert isinstance(order, Order)
    text = order_prompt(order, "Teile 3.1.1.", [("user", "x")], "project")
    assert "Ziel: 3.1.1 teilen" in text and "Nachricht: Teile 3.1.1." in text
    assert "project.changeset.propose" in text
    for gone in ("Zeitraum", "Maschine:", "Ergebnis: Diagnose", "nur Maschinen"):
        assert gone not in text
    # live 2026-11-20: the task's title and points came in English, with a table form on a dev task
    assert "Titel und Text neuer oder umgeschriebener Aufgaben auf Deutsch" in text
    english = order_prompt(order, "Split 3.1.1.", [], "project", "en")
    assert "texts of new or rewritten tasks in English" in english
    # every other application keeps its frame
    assert "Zeitraum" in order_prompt(order, "Teile 3.1.1.", [], "admin")
