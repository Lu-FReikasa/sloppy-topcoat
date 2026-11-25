"""The knowledge base's pure rules (E2-03): exclude list, chunking, secret vetting."""

import pytest

from topcoat_hub import knowledge_rules as kr

SAMPLES = {
    "a/b/c.md": False,
    "SENSIBEL/x.md": True,
    "a/sensibel-notes/x.md": True,
    "a/x-ZUGANGSDATEN.md": True,
    "x.PEM": True,
    "deploy/.env.production": True,
    "a/My-Password-List.md": True,
    "a/PASSWORT.txt": True,
    "notes/secrets-of-cooking.md": True,
    "a/.git/HEAD": True,
    "keyboard.md": False,
    "servers/web.md": False,
}  # the same samples as the node's TestDocsExcludedMatchesEverySegmentCaseInsensitively


@pytest.mark.parametrize(("path", "want"), SAMPLES.items())
def test_exclude_list_matches_every_segment_case_insensitively(path: str, want: bool) -> None:
    assert kr.excluded(path) is want


def test_extra_globs_add_but_never_remove() -> None:
    assert kr.excluded("x/private/y.md", ["private"]) and not kr.excluded("x/y.md", ["private"])
    assert kr.excluded("SENSIBEL/y.md", ["private"])
    assert kr.excluded("SENSIBEL/y.md", [""])  # an empty glob changes nothing


def test_only_text_files_are_wanted() -> None:
    assert kr.wanted("a/b.MD") and kr.wanted("x.txt") and not kr.wanted("x.png")


DOC = """Intro text before any heading, long enough to count.

# Web

The web stack runs on vm-docker1.

## Restart

Use `docker compose restart web`.

```
# not a heading, a comment in a code block
echo "still the same section, long enough to be a chunk"
```

## Backups

Nightly at 03:00, seven days kept.
"""


def test_chunks_follow_headings_and_keep_the_path() -> None:
    chunks = kr.chunk_file("notes/lab.md", DOC)
    assert [c.heading for c in chunks] == ["lab", "Web", "Web > Restart", "Web > Backups"]
    assert [c.ord for c in chunks] == [0, 1, 2, 3]
    restart = chunks[2].text
    assert "docker compose restart web" in restart and "# not a heading" in restart


def test_long_sections_are_split_below_the_limit() -> None:
    body = "\n\n".join(f"Paragraph {i}: " + "word " * 60 for i in range(20))
    chunks = kr.chunk_file("long.md", "# Long\n\n" + body)
    assert len(chunks) > 3 and all(len(c.text) <= kr.CHUNK_CHARS for c in chunks)
    assert "Paragraph 19" in chunks[-1].text and "Paragraph 0" in chunks[0].text
    huge_line = "x" * 5000
    assert all(len(c.text) <= kr.CHUNK_CHARS for c in kr.chunk_file("l.txt", huge_line))


def test_tiny_and_empty_pieces_are_dropped() -> None:
    assert kr.chunk_file("e.md", "# A\n\n# B\n\nok") == []


@pytest.mark.parametrize(
    "text",
    [
        "the admin password is hunter2hunter2 for the nas",
        "-----BEGIN RSA PRIVATE KEY-----\nMIIEow",
        "connect postgres://app:demo-password@db/app",
        "token sk-abcdefghijklmnopqrstuvwxyz",
    ],
)
def test_a_chunk_with_a_secret_is_not_indexed(text: str) -> None:
    kept, rule = kr.vet(text)
    assert kept is None and rule


def test_a_long_mixed_token_is_replaced_and_the_rest_is_kept() -> None:
    text = (
        "See https://example.invalid/d/0123456789abcdefghijklmnopqrstuvwxyz0123 for the dashboard."
    )
    kept, rule = kr.vet(text)
    assert rule is None and kept is not None
    assert "0123456789abcdef" not in kept and "for the dashboard" in kept


def test_keywords_drop_stop_words_and_repeats() -> None:
    assert kr.keywords("How is the nightly backup of the database done? Backup!") == [
        "nightly", "backup", "database", "done",
    ]  # fmt: skip
    assert kr.keywords("Wie wird das Backup der Datenbank gemacht?") == [
        "backup",
        "datenbank",
        "gemacht",
    ]
    assert kr.keywords("is it of to") == []
