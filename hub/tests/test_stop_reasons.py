"""One table explains why a run ended (stop_reasons.py): the card, the report and the failure
message read it, and a code the loop can emit is never missing from it."""

import re
from pathlib import Path

from topcoat_hub import stop_reasons
from topcoat_hub.stop_reasons import ALIASES, STOPS

SRC = Path(__file__).resolve().parents[1] / "src" / "topcoat_hub"
# ends that are no "stopped early" story: a person's cancel, a crash, the children of a coordinator
NOT_EXPLAINED = {"canceled", "internal", "children_failed"}


def test_every_entry_has_both_languages_and_a_known_category() -> None:
    for code, stop in STOPS.items():
        assert set(stop.short) == {"de", "en"} and set(stop.long) == {"de", "en"}, code
        assert stop.category in {"node_away", "no_budget", "no_sources", "other"}, code
    assert all(target in STOPS for target in ALIASES.values())


def test_a_code_the_code_can_emit_is_in_the_table() -> None:
    """The loop and the service name their stop codes in a few call forms; a new one that is not
    in the table would show up as a raw code on the card."""
    found: set[str] = set()
    pattern = re.compile(r'stop_reason="([a-z_]+)"|_wrap_up\([^,()]+, "([a-z_]+)"')
    for name in ("agent_loop.py", "agent_service.py"):
        for a, b in pattern.findall((SRC / name).read_text(encoding="utf-8")):
            found.add(a or b)
    from topcoat_hub.agent_loop import MODEL_REASONS

    found |= set(MODEL_REASONS)  # the node's own reasons, named by the gateway
    assert found, "the pattern found nothing: the call forms changed"
    missing = {c for c in found if stop_reasons.long(c, "en") is None} - NOT_EXPLAINED
    assert not missing, f"add to stop_reasons.STOPS: {sorted(missing)}"


def test_the_words_and_categories() -> None:
    assert stop_reasons.short("max_turns", "de") == "die Schritte reichten nicht aus"
    assert stop_reasons.long("model_failed", "en").startswith("the model could not be reached")
    assert stop_reasons.short("something_new", "en") == "something_new"
    assert stop_reasons.long("something_new", "en") is None
    # a run that used up its steps or calls asks for a bigger budget; before, it was "other"
    assert stop_reasons.category("max_turns") == "no_budget"
    assert stop_reasons.category("max_tool_calls") == "no_budget"
    assert stop_reasons.category("turns") == "no_budget"  # the older name
    assert stop_reasons.category("model_unavailable") == "node_away"
    assert stop_reasons.category(None, has_error=True) == "tool_errors"
    assert stop_reasons.category("repeated_calls") == "other"
