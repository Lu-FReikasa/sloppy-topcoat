"""The hub's own chores do not think (gateway.chore_payload): a thinking model would spend the
token limit of a short note on its thoughts and answer with nothing."""

from topcoat_hub.gateway import CHORE_LABELS, chore_payload

CHAT = "/v1/chat/completions"


def test_a_chore_gets_the_switch_that_turns_thinking_off() -> None:
    payload = {"model": "m", "max_tokens": 300}
    for label in (
        "memory", "review", "digest_summary", "digest_check", "core_review", "care", "quality",
        "precheck",
    ):  # fmt: skip
        out = chore_payload(payload, label, CHAT)
        assert out["chat_template_kwargs"] == {"enable_thinking": False}, label
    assert "chat_template_kwargs" not in payload  # the caller's dict is not changed


def test_what_is_not_a_chore_is_left_alone() -> None:
    payload = {"model": "m"}
    assert chore_payload(payload, "", CHAT) is payload  # a chat or a task: its own effort table
    assert chore_payload(payload, "agent", CHAT) is payload
    assert chore_payload(payload, "revise", CHAT) is payload


def test_a_switch_the_caller_set_is_kept_and_embeddings_are_not_touched() -> None:
    own = {"model": "m", "chat_template_kwargs": {"enable_thinking": True}}
    assert chore_payload(own, "memory", CHAT) is own
    effort = {"model": "m", "reasoning_effort": "low"}
    assert chore_payload(effort, "review", CHAT) is effort
    embed = {"model": "bge", "input": "x"}
    assert chore_payload(embed, "memory", "/v1/embeddings") is embed


def test_the_chores_are_the_labels_the_background_work_uses() -> None:
    assert {"memory", "care", "digest_summary", "digest_check", "review"} <= CHORE_LABELS
