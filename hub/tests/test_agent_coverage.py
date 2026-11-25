"""Which machines a run names as "not checked": only those that could have answered the same kind
of question (live 2026-11-17: a Wi-Fi question asked of the one Omada node listed five unrelated
nodes as not checked, and the chat answer began with them)."""

from topcoat_hub.agent_loop import coverage

TOOLS = [
    {"node": "tools-a", "capability": "omada.clients"},
    {"node": "tools-a", "capability": "docker.logs"},
    {"node": "gpu-box", "capability": "docker.logs"},
    {"node": "gpu-box", "capability": "system.info"},
    {"node": "embed", "capability": "system.info"},
]


def live(node: str, capability: str) -> tuple[str, dict]:
    return "live", {"node": node, "capability": capability}


def test_a_wifi_question_names_no_unrelated_node() -> None:
    asked, not_asked = coverage(
        TOOLS, [live("tools-a", "omada.clients"), live("topcoat", "topcoat.state")]
    )
    assert asked == ["tools-a"]  # Topcoat itself is no machine
    assert not_asked == []


def test_nodes_of_the_same_family_are_named() -> None:
    asked, not_asked = coverage(TOOLS, [live("gpu-box", "docker.logs")])
    assert asked == ["gpu-box"]
    assert not_asked == ["tools-a"]


def test_a_question_answered_from_the_notes_names_no_node() -> None:
    assert coverage(TOOLS, [("doc", {"node": "knowledge", "capability": "knowledge.search"})]) == (
        [],
        [],
    )
