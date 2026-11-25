"""A file read is shown as lines, cut at a line, with the exact next call (live 2026-11-11)."""

from topcoat_hub.agent_prompts import read_window


def _out(lines: list[str], start: int = 1, total: int | None = None) -> dict:
    return {
        "repo": "app",
        "path": "web/src/desktop/theme.ts",
        "start_line": start,
        "end_line": start + len(lines) - 1,
        "total_lines": total or start + len(lines) - 1,
        "text": "\n".join(lines),
        "truncated": False,
        "more_after": False,
        "masked": 0,
    }


def test_a_long_file_is_cut_at_a_line_and_names_the_next_start() -> None:
    lines = [f"const line{i} = {'x' * 60};" for i in range(1, 268)]
    text, nxt = read_window(_out(lines), limit=2000)
    shown = text.splitlines()
    assert shown[0].startswith("web/src/desktop/theme.ts: lines 1-")
    last = int(shown[0].split("lines 1-")[1].split(" ")[0])
    assert nxt == last + 1 and 20 < last < 40
    assert shown[last] == lines[last - 1]  # whole lines, the last one complete
    assert f'"start_line": {nxt}' in shown[-1]
    assert chr(92) + "n" not in text  # lines, not one JSON string with escaped newlines


def test_the_next_part_goes_on_from_there_and_ends_the_file() -> None:
    lines = [f"l{i}" for i in range(100, 268)]
    text, nxt = read_window(_out(lines, start=100, total=267), limit=9000)
    assert nxt is None and "lines 100-267 of 267 (end of file)" in text


def test_other_outputs_are_left_to_the_old_path() -> None:
    assert read_window({"log": "x"}) is None
    assert read_window("plain") is None
    assert read_window({"text": "x"}) is None  # no line numbers: not a file read
