"""The hub's own reads in the chat (pruefauftrag T3.6, T3.8): what is offered, how arguments are
read, and which part of a record a question gets."""

from topcoat_hub import local_tools


def _names(keys: set[str]) -> list[str]:
    return [d["function"]["name"] for d in local_tools.definitions(keys)]


def test_the_state_functions_are_offered_only_with_their_group():
    always = [local_tools.RECORD, local_tools.ABOUT]  # the record and the contract (1.9)
    assert _names(set()) == always
    assert _names({"topcoat"}) == [local_tools.NODE_STATE, *always]
    assert _names({"monitors", "host"}) == [local_tools.MONITOR_STATE, *always]


def test_arguments_have_safe_defaults():
    assert local_tools.args_of("not json") == {}
    assert local_tools.node_state_input({}) == {"what": "nodes"}
    assert local_tools.monitor_input({}) == {"only_problems": True}
    assert local_tools.monitor_input({"only_problems": "yes please"}) == {"only_problems": True}


def test_monitor_lines_are_short_and_capped():
    rows = [{"name": f"m{i}", "status": "down"} for i in range(50)]
    text = local_tools.monitor_text(rows)
    assert len(text.splitlines()) == local_tools.MONITOR_LINES
    assert local_tools.monitor_text([]) == "no monitor has a problem"


def test_a_record_gives_the_part_the_question_needs_and_no_excerpts_by_default():
    card = {
        "summary": "Audio fine.",
        "findings": [{"tag": "live", "claim": "driver is current"}],
        "evidence": [
            {
                "ref": "E1",
                "capability": "windows.events",
                "node": "node-amd",
                "excerpt": "EVENT 225",
            }
        ],
        "failures": [
            {"capability": "windows.services", "node": "node-amd", "message": "name refused"}
        ],
    }
    default = local_tools.record_text(card)
    assert "driver is current" in default and "E1" in default and "EVENT 225" not in default
    assert "EVENT 225" in local_tools.record_text(card, ref="E1")
    assert "no evidence E9" in local_tools.record_text(card, ref="E9")
    assert "name refused" in local_tools.record_text(card, section="failed")
    assert local_tools.record_text(None) == "no such record of yours"


def test_a_state_reply_is_cut():
    assert len(local_tools.state_text({"x": "a" * 5000})) <= local_tools.NODE_STATE_CHARS
