"""AGT-04: the assistant knows where it runs and what it may do."""

from fastapi.testclient import TestClient

from tests.fakenode import online_node
from tests.test_chat import new_conv, sent_messages, use_model
from tests.test_memory import chat_once
from topcoat_hub import chat


def test_the_line_names_program_model_machine_and_the_rule_for_changes() -> None:
    line = chat.where_block(
        "project", "gemma-12B", "node-amd", "AMD RX 9070 XT 16 GB, 32 GB RAM", True
    )
    assert "program project" in line
    assert "model gemma-12B on the machine node-amd (AMD RX 9070 XT 16 GB, 32 GB RAM)" in line
    assert "you propose, and the person confirms each change" in line


def test_what_is_not_known_is_left_out_not_guessed() -> None:
    assert chat.where_block(None, None, None) == ""
    assert chat.where_block(None, "gemma", None) == "You are the model gemma."
    assert "machine" not in chat.where_block("chat", "gemma", None)
    assert "propose" not in chat.where_block("chat", "gemma", "node-a")


def test_the_prompt_of_a_chat_answer_carries_the_line(client: TestClient, connect) -> None:
    node = online_node(client, connect, "node-a", loaded=["small"])
    use_model(client)
    node.stream_text_fn = lambda req: "Ich laufe auf node-a."
    conv = new_conv(client)
    chat_once(client, "Auf welcher Maschine läufst du?", conv)
    system = sent_messages(node)[0]["content"]
    assert "the model small on the machine node-a" in system


def test_a_small_context_gets_the_shortest_line_or_none() -> None:
    assert chat.where_block("chat", "gemma", "node-a", "16 GB", True, compact=True) == (
        "You are gemma on node-a."
    )
    assert chat.where_block("chat", "gemma", None, compact=True) == ""
