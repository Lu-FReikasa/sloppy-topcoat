"""Helpers to check the system prompt of a chat (E3-06): core block first, then the fixed text."""

from topcoat_hub import chat


def fixed_text(*, memory: bool = True, compact: bool = False) -> str:
    """What follows the core block: the fixed rules for these capabilities."""
    return chat.system_prompt(
        None, "du", chat.Capabilities(memory=memory), compact, core="CORE"
    ).removeprefix("CORE\n\n")


def assert_system(
    content: str, *, memory: bool = True, compact: bool = False, contains: tuple[str, ...] = ()
) -> None:
    """``content`` is a system prompt of a chat: the core block (it says what ``contains``), then
    the fixed text of the capabilities."""
    assert content.startswith("# Core"), content
    assert content.endswith("\n\n" + fixed_text(memory=memory, compact=compact)), content
    for text in contains:
        assert text in content, text
