"""knowledge.read: a document in pieces, so a long wiki page is read to its end (knowledge.py)."""

import asyncio
import uuid

from topcoat_hub import agent_loop, agent_prompts
from topcoat_hub.knowledge import READ_CAPABILITY, READ_CHARS, read_page, read_tool

# what the agent sees of one tool output on the live hub (agent_observation_chars)
OBSERVATION_CHARS = 3000


def read_all(text: str) -> str:
    out, offset = [], 0
    while True:
        page = read_page(text, offset)
        out.append(page["content"])
        if page["next_offset"] is None:
            return "".join(out)
        offset = page["next_offset"]


def paragraphs(count: int, size: int) -> str:
    return "\n\n".join(("Satz eins. " * size)[:size] for _ in range(count))


def test_a_short_document_comes_whole_with_no_next_offset() -> None:
    page = read_page("kurz", 0)
    assert page == {"content": "kurz", "total_chars": 4, "offset": 0, "next_offset": None}


def test_a_document_without_any_break_is_cut_at_the_maximum_only_then() -> None:
    text = "".join(str(i % 10) for i in range(READ_CHARS * 2 + 500))
    first = read_page(text, 0)
    assert len(first["content"]) == READ_CHARS
    assert first["next_offset"] == READ_CHARS
    assert read_all(text) == text


def test_a_piece_ends_at_a_paragraph_break_instead_of_in_the_middle_of_a_paragraph() -> None:
    text = paragraphs(5, 1000)  # paragraph breaks after 1000, 2002, 3004 characters
    first = read_page(text, 0)
    assert first["content"].endswith("\n\n")
    assert len(first["content"]) <= READ_CHARS
    assert first["next_offset"] == len(first["content"])


def test_short_pieces_are_not_made_from_an_early_break() -> None:
    # a break at character 100 must not end the piece: the window's second half has no break
    text = "kurz\n\n" + "w" * (READ_CHARS + 500)
    first = read_page(text, 0)
    assert len(first["content"]) == READ_CHARS


def test_the_pieces_read_back_to_the_end_unchanged() -> None:
    text = paragraphs(9, 900) + "\n\nSchluss."
    assert read_all(text) == text


def test_the_last_piece_has_no_next_offset() -> None:
    text = "x" * (READ_CHARS + 10)
    last = read_page(text, READ_CHARS)
    assert last["content"] == "x" * 10
    assert last["next_offset"] is None


def test_an_offset_past_the_end_gives_an_empty_piece() -> None:
    page = read_page("abc", 50)
    assert page["content"] == "" and page["next_offset"] is None and page["total_chars"] == 3


def test_a_worst_case_piece_reaches_the_agent_whole_with_its_next_offset_first() -> None:
    # the wiki page of 2026-11-07 (3189 characters): its continuation sat behind a cut and was lost
    text = paragraphs(3, 1060) + "\n"
    output = {"path": "Seite~1.md", "source": "Topcoat Doku", **read_page(text, 0)}
    shown = agent_prompts.output_text(output)
    shown_text, was_cut = agent_prompts.cut(shown, OBSERVATION_CHARS, newest=False)
    assert was_cut is False, shown_text[-80:]
    assert shown.splitlines()[0].count("next_offset=") == 1
    assert "\\n" not in shown  # the text comes unescaped, not inside JSON


class _Session:
    async def __aenter__(self) -> None:
        return None

    async def __aexit__(self, *exc: object) -> None:
        return None


def test_a_read_answers_in_the_envelope_the_agent_loop_reads() -> None:
    # the loop treats an answer without ``ok`` as a failure: the read must say ok, with the piece
    from types import SimpleNamespace

    from topcoat_hub import knowledge, knowledge_store

    chunks = [(SimpleNamespace(text="erster Abschnitt", source_id=1), "Topcoat Doku")]

    async def fake_chunks(self: object, path: str) -> list[object]:
        return chunks if path == "Seite~1.md" else []

    original = knowledge_store.KnowledgeStore.chunks_of_path
    knowledge_store.KnowledgeStore.chunks_of_path = fake_chunks  # type: ignore[method-assign]
    try:
        service = knowledge.KnowledgeService.__new__(knowledge.KnowledgeService)
        service._sm = _Session  # type: ignore[attr-defined]
        answer = asyncio.run(service.read(uuid.uuid4(), {"path": "Seite~1.md"}))
    finally:
        knowledge_store.KnowledgeStore.chunks_of_path = original  # type: ignore[method-assign]
    assert answer["ok"] is True
    assert answer["output"]["content"] == "erster Abschnitt"
    assert answer["output"]["source"] == "Topcoat Doku" and answer["output"]["next_offset"] is None


def test_the_read_tool_is_offered_under_its_own_capability() -> None:
    tool = read_tool()
    assert tool["capability"] == READ_CAPABILITY
    assert tool["input_schema"]["required"] == ["path"]
    assert set(tool["input_schema"]["properties"]) == {"path", "offset"}


def test_a_repeated_read_is_told_to_read_on_not_to_read_another_range() -> None:
    hint = agent_loop._repeat_hint(READ_CAPABILITY)
    assert "next_offset" in hint
    assert "start_line" not in hint and "find=" not in hint


def test_a_repeated_search_is_not_told_about_file_ranges() -> None:
    hint = agent_loop._repeat_hint("knowledge.search")
    assert "start_line" not in hint and "find=" not in hint


def test_a_path_joined_with_its_source_name_reads_the_same_document() -> None:
    # a search hit names the source and the path in two fields; a caller that joins them
    # ("Akten/x.md") means "x.md" and gets that document, not a not_found loop
    from types import SimpleNamespace

    from topcoat_hub import knowledge, knowledge_store

    chunks = [(SimpleNamespace(text="Akte", source_id=1), "Akten")]
    asked: list[str] = []

    async def fake_chunks(self: object, path: str) -> list[object]:
        asked.append(path)
        return chunks if path == "akte-1.md" else []

    original = knowledge_store.KnowledgeStore.chunks_of_path
    knowledge_store.KnowledgeStore.chunks_of_path = fake_chunks  # type: ignore[method-assign]
    try:
        service = knowledge.KnowledgeService.__new__(knowledge.KnowledgeService)
        service._sm = _Session  # type: ignore[attr-defined]
        answer = asyncio.run(service.read(uuid.uuid4(), {"path": "Akten/akte-1.md"}))
    finally:
        knowledge_store.KnowledgeStore.chunks_of_path = original  # type: ignore[method-assign]
    assert asked == ["Akten/akte-1.md", "akte-1.md"]
    assert answer["ok"] is True and answer["output"]["path"] == "akte-1.md"
