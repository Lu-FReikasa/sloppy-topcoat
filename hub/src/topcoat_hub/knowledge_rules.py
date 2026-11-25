"""Rules of the knowledge base (E2-03): what is never read, and how a file becomes chunks.

Pure functions, no I/O. The exclude list exists twice on purpose — on the node (which never
lists or reads such a file) and here (which never asks for one, never indexes one and drops a
hit whose path matches even if the list grew after indexing). The two lists are kept in step by
hand (node ``tools.DefaultDocsExclude``); a test on each side holds the same samples.
"""

import re
from dataclasses import dataclass
from fnmatch import fnmatchcase

from topcoat_hub import memory_filter

# Cannot be switched off: a path with a segment (directory or file name) matching one of these
# globs, case-insensitive, is not read, not indexed and not named in any result.
DEFAULT_EXCLUDE = (
    "*SENSIBEL*",
    "*ZUGANGSDATEN*",
    "*.key",
    "*.pem",
    "*.env",
    ".env*",
    "*password*",
    "*passwort*",
    "*secret*",
    "*credential*",
    "*.kdbx",
    "id_rsa*",
    "id_ed25519*",
    ".git",
)
EXTENSIONS = (".md", ".markdown", ".txt")
MAX_FILE_BYTES = 1 << 20  # larger files are not indexed (the node refuses to read them too)
CHUNK_CHARS = 1200
MIN_CHUNK_CHARS = 20


def excluded(path: str, extra: tuple[str, ...] | list[str] = ()) -> bool:
    """Does any segment of ``path`` match the exclude list (the fixed one plus ``extra``)?"""
    patterns = [p.lower() for p in (*DEFAULT_EXCLUDE, *extra) if p.strip()]
    for segment in re.split(r"[/\\]", path):
        s = segment.lower()
        if s and any(fnmatchcase(s, p) for p in patterns):
            return True
    return False


def wanted(path: str) -> bool:
    return path.lower().endswith(EXTENSIONS)


PROFILE_FOLDER = "steckbriefe"


def is_profile_path(path: str) -> bool:
    """A note in a folder called ``steckbriefe`` (any depth): a profile of a host or service that
    the memory mirrors (E3-06)."""
    parts = [p.lower() for p in re.split(r"[/\\]", path)]
    return PROFILE_FOLDER in parts[:-1] and wanted(path)


@dataclass(frozen=True)
class Chunk:
    heading: str  # "Web > Restart"
    text: str
    ord: int


_HEADING = re.compile(r"^(#{1,6})[ \t]+(.+?)[ \t#]*$")
_FENCE = re.compile(r"^\s*(```|~~~)")


def _sections(text: str, title: str) -> list[tuple[str, str]]:
    """(heading path, body) per heading of a Markdown text; text before the first heading gets
    the file's title."""
    stack: list[tuple[int, str]] = []
    out: list[tuple[str, list[str]]] = [(title, [])]
    in_fence = False
    for line in text.splitlines():
        if _FENCE.match(line):
            in_fence = not in_fence
        m = None if in_fence else _HEADING.match(line)
        if m:
            level, name = len(m.group(1)), m.group(2).strip()
            while stack and stack[-1][0] >= level:
                stack.pop()
            stack.append((level, name))
            out.append((" > ".join(n for _, n in stack), []))
        else:
            out[-1][1].append(line)
    return [(h, "\n".join(body).strip()) for h, body in out if "\n".join(body).strip()]


def _split(body: str, limit: int) -> list[str]:
    """Pieces of at most ``limit`` characters, cut at blank lines, then lines, then anywhere."""
    if len(body) <= limit:
        return [body]
    pieces: list[str] = []
    current = ""
    for para in re.split(r"\n\s*\n", body):
        if len(para) > limit:
            if current:
                pieces.append(current)
                current = ""
            line_buf = ""
            for line in para.splitlines() or [para]:
                while len(line) > limit:
                    if line_buf:
                        pieces.append(line_buf)
                        line_buf = ""
                    pieces.append(line[:limit])
                    line = line[limit:]
                if len(line_buf) + len(line) + 1 > limit and line_buf:
                    pieces.append(line_buf)
                    line_buf = ""
                line_buf = f"{line_buf}\n{line}" if line_buf else line
            if line_buf:
                pieces.append(line_buf)
        elif len(current) + len(para) + 2 > limit and current:
            pieces.append(current)
            current = para
        else:
            current = f"{current}\n\n{para}" if current else para
    if current:
        pieces.append(current)
    return pieces


def chunk_file(path: str, text: str) -> list[Chunk]:
    """Chunks by headings (Markdown) of at most ``CHUNK_CHARS`` characters each."""
    title = path.rsplit("/", 1)[-1].rsplit(".", 1)[0]
    out: list[Chunk] = []
    for heading, body in _sections(text, title):
        for piece in _split(body, CHUNK_CHARS):
            if len(piece.strip()) >= MIN_CHUNK_CHARS:
                out.append(Chunk(heading, piece.strip(), len(out)))
    return out


def vet(text: str) -> tuple[str | None, str | None]:
    """The D19 secret filter for a chunk: ``(text to index, rule name)``. A chunk with a secret
    is **not indexed** (``None``, the rule); the one exception is a long mixed token — common in
    URLs and hashes of ordinary notes — which is replaced by a marker when nothing else is found.
    The word "Passwort" alone is no secret in documentation: the ``password`` rule needs a value
    behind it (``check(prose=True)``); a leftover value is still caught after scrubbing."""
    rule = memory_filter.check(text, prose=True)  # notes mention "Passwort"; only a value counts
    if rule is None:
        return text, None
    if rule == "long_token":
        scrubbed, _ = memory_filter.scrub(text)
        if memory_filter.check_scrubbed(scrubbed) is None:
            return scrubbed, None
    return None, rule


def vet_lines(text: str) -> tuple[str | None, list[str]]:
    """:func:`vet` that keeps what is not secret: ``(text to index, rules of what was removed)``.

    A chunk that passes is returned as it is (no rules). When it does not, only the **lines** that
    carry the secret are dropped and the rest stays searchable: a short note with one password
    line must not vanish whole. The text is vetted again after the removal; if anything still
    matches (a secret that spreads over lines in a way the pieces do not show), or the chunk holds a
    private key, or nothing is left, the whole chunk is held back as before (``None``)."""
    body, rule = vet(text)
    if body is not None:
        return body, []
    if rule == "private_key":
        return None, [rule]
    lines = text.split("\n")
    vetted = [vet(line) if line.strip() else (line, None) for line in lines]
    dropped = [body is None for body, _ in vetted]
    if not any(dropped):
        # a secret that only shows across two neighbouring lines ("Passwort:" / "hunter2x")
        for i in range(len(lines) - 1):
            if (
                lines[i].strip()
                and lines[i + 1].strip()
                and vet(lines[i] + "\n" + lines[i + 1])[0] is None
            ):
                dropped[i] = dropped[i + 1] = True
        if not any(dropped):
            return None, [rule or "secret"]
    found = sorted(
        {r or "secret" for (b, r), d in zip(vetted, dropped, strict=True) if d and b is None}
        or {rule or "secret"}
    )
    kept = "\n".join(
        b if b is not None else line
        for (b, _), line, d in zip(vetted, lines, dropped, strict=True)
        if not d
    )
    kept = re.sub(r"\n{3,}", "\n\n", kept).strip()
    if not kept:
        return None, found
    again, _ = vet(kept)
    if again is None:
        return None, [rule or "secret"]
    return again, found


# Words that say nothing about a topic (English and German): a question like "how is the backup of
# the database done" must not match every note that contains "the".
_STOP_TEXT = (
    "the and for are was were with that this from have has had not but you your how what when "
    "where why who which does did can could will would should into over its our out all any "
    "there their then than them they been being also more most some such only about after "
    "der die das und ist sind war waren mit von dem den des ein eine einen einem einer nicht "
    "auch wie was wann wer warum welche welcher welches auf aus bei nach zum zur dass oder aber "
    "wird werden wurde kann kannst haben hat hatte ich mein meine meinen wir sie ihr uns "
    "für fuer über ueber noch nur mehr sehr wenn dann doch schon bitte"
)
STOPWORDS = frozenset(_STOP_TEXT.split())


def keywords(query: str) -> list[str]:
    """The words of a question that can find something: 3+ letters, no stop words, no repeats."""
    seen: dict[str, None] = {}
    for word in re.findall(r"[^\W_]{3,}", query.casefold()):
        if word not in STOPWORDS:
            seen.setdefault(word)
    return list(seen)
