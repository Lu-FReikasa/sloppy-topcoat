"""The knowledge base's directory (E3-04): ``10_KI_Verzeichnis.md``, a file the maintainer generates
(``scripts/dev/make_kb_index.py``) that says, for every host and service, *which sections of which
notes are about it* — with the line numbers. The search looks a host or service named in the
question up there first and returns exactly those sections (not whole files, not "files that are
similar"); the similarity search fills what is left.

The format (also what the script writes)::

    ## tools-alpha
    aliases: alpha, docker host
    - `03_Server/alpha.md:120-180` Docker stack
    - `04_Dienste/kuma.md` § Uptime Kuma einrichten

An entry is a ``##`` (or ``###``) heading, an optional ``aliases:`` line and list items that name a
note and, optionally, lines (``path:120-180``, ``path (Zeile 120-180)``, ``path Z. 120-180``) or a
heading (``§ Name``, ``#Name`` or the rest of the line). One-line entries ``- name -> path:1-9,
path2:5-8`` work too. Nothing here reads a file: it parses text and cuts sections out of text."""

import re
from dataclasses import dataclass, field

DIRECTORY_FILE = "10_KI_Verzeichnis.md"
MAX_ENTITIES = 3  # hosts / services taken from one question
MAX_REFS = 6  # sections returned from the directory
DEFAULT_SPAN = 60  # lines of a section whose end the directory does not give

_PATH = re.compile(r"(?P<path>[\w./\-]+\.(?:md|markdown|txt))", re.IGNORECASE)
_DASH = "-\u2013\u2014"  # hyphen, en dash, em dash
_COLON_LINES = re.compile(rf"^:(?P<a>\d+)(?:\s*[{_DASH}]\s*(?P<b>\d+))?")
_WORD_LINES = re.compile(
    rf"(?:zeilen?|z\.|lines?|ll?\.|l)\s*(?P<a>\d+)(?:\s*[{_DASH}]\s*(?P<b>\d+))?", re.IGNORECASE
)
_HEADING = re.compile(r"^(#{1,6})[ \t]+(.+?)[ \t#]*$")
_ALIASES = re.compile(r"^\s*(?:aliases?|auch|also)\s*:\s*(?P<v>.+)$", re.IGNORECASE)
_ARROW = re.compile(r"^\s*[-*]\s*\*{0,2}(?P<name>[^*:>→]+?)\*{0,2}\s*(?:→|->|=>)\s*(?P<refs>.+)$")


@dataclass(frozen=True)
class Ref:
    """A section of a note: lines ``start``..``end`` (1-based, inclusive), or the section under
    the heading ``label``; with neither, the whole start of the file."""

    path: str
    start: int | None = None
    end: int | None = None
    label: str = ""


@dataclass
class Entry:
    name: str
    aliases: list[str] = field(default_factory=list)
    refs: list[Ref] = field(default_factory=list)

    def names(self) -> list[str]:
        return [self.name, *self.aliases]


def _safe(path: str) -> bool:
    """A path inside the notes: relative, no ``..``, no drive or backslash tricks."""
    return (
        bool(path)
        and not path.startswith(("/", "~"))
        and ".." not in path.split("/")
        and "\\" not in path
        and ":" not in path
    )


def _refs_in(text: str) -> list[Ref]:
    """Every ``path[:lines]`` / ``path (Zeile …)`` / ``path § heading`` in a line of text."""
    out: list[Ref] = []
    matches = list(_PATH.finditer(text))
    for i, m in enumerate(matches):
        path = m.group("path").lstrip("./") if m.group("path").startswith("./") else m.group("path")
        rest = text[m.end() : matches[i + 1].start() if i + 1 < len(matches) else len(text)]
        start = end = None
        lines = _COLON_LINES.match(rest.lstrip("`)"))
        if lines is None:
            lines = _WORD_LINES.search(rest)
        if lines:
            start = int(lines.group("a"))
            end = int(lines.group("b")) if lines.group("b") else None
            rest = rest.replace(lines.group(0), " ", 1)
        label = re.sub(r"[`()\[\]*§#|,;]+", " ", rest)
        label = " ".join(label.replace(":", " ").split()).strip(" " + _DASH)
        if _safe(path):
            out.append(Ref(path, start, end, label))
    return out


def parse(text: str) -> list[Entry]:
    """The entries of a directory file; lines that are none of the forms above are ignored."""
    entries: list[Entry] = []
    current: Entry | None = None
    in_fence = False
    for raw in text.splitlines():
        line = raw.rstrip()
        if line.lstrip().startswith(("```", "~~~")):
            in_fence = not in_fence
            continue
        if in_fence or not line.strip() or line.lstrip().startswith("<!--"):
            continue
        h = _HEADING.match(line)
        if h:
            level, name = len(h.group(1)), h.group(2).strip().strip("`*")
            if level in (2, 3) and name:
                current = Entry(name)
                entries.append(current)
            else:
                current = None
            continue
        arrow = _ARROW.match(line)
        if arrow and _PATH.search(arrow.group("refs")):
            entries.append(Entry(arrow.group("name").strip(), [], _refs_in(arrow.group("refs"))))
            continue
        if current is None:
            continue
        alias = _ALIASES.match(line)
        if alias:
            current.aliases += [a.strip().strip("`*") for a in re.split(r"[,;]", alias.group("v"))]
            current.aliases = [a for a in current.aliases if a]
            continue
        current.refs += _refs_in(line)
    return [e for e in entries if e.refs]


def _word(name: str) -> re.Pattern[str]:
    return re.compile(rf"(?<![\w\-]){re.escape(name.lower())}(?![\w\-])", re.IGNORECASE)


def match(entries: list[Entry], query: str, limit: int = MAX_ENTITIES) -> list[Entry]:
    """The hosts and services the question names (a name or alias of at least three characters as
    a whole word), the most specific (longest) match first."""
    low = query.lower()
    scored: list[tuple[int, int, Entry]] = []
    for i, e in enumerate(entries):
        best = 0
        for n in e.names():
            if len(n) >= 3 and _word(n).search(low):
                best = max(best, len(n))
        if best:
            scored.append((-best, i, e))
    scored.sort(key=lambda t: (t[0], t[1]))
    return [e for _, _, e in scored[:limit]]


def section(text: str, ref: Ref, max_chars: int = 2000) -> tuple[str, str]:
    """The lines a :class:`Ref` points at, as ``(heading, text)``: the given line range, else the
    section under the heading named by the label (up to the next heading of the same or a higher
    level), else the start of the file. Cut at ``max_chars``."""
    lines = text.splitlines()
    if not lines:
        return "", ""
    if ref.start is not None:
        a = max(ref.start - 1, 0)
        b = min(ref.end if ref.end is not None else a + DEFAULT_SPAN, len(lines))
        a = min(a, len(lines))
        # the nearest heading above the range names it
        heading = ""
        for ln in reversed(lines[: a + 1]):
            h = _HEADING.match(ln)
            if h:
                heading = h.group(2).strip()
                break
        body = "\n".join(lines[a:b]).strip()
        return heading or ref.label, _clip(body, max_chars)
    if ref.label:
        want = ref.label.lower()
        for i, ln in enumerate(lines):
            h = _HEADING.match(ln)
            if h and want in h.group(2).lower():
                level, j = len(h.group(1)), i + 1
                while j < len(lines):
                    nxt = _HEADING.match(lines[j])
                    if nxt and len(nxt.group(1)) <= level:
                        break
                    j += 1
                return h.group(2).strip(), _clip("\n".join(lines[i + 1 : j]).strip(), max_chars)
    # no lines and no (found) heading: the start of the note, named by its first heading
    first = ""
    for ln in lines[:DEFAULT_SPAN]:
        h = _HEADING.match(ln)
        if h:
            first = h.group(2).strip()
            break
    return ref.label or first, _clip("\n".join(lines[:DEFAULT_SPAN]).strip(), max_chars)


def _clip(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[:limit].rstrip() + " …"


def outline(text: str) -> list[tuple[int, str, int, int]]:
    """``(level, heading, first line, last line)`` of every heading's section (1-based, inclusive;
    the section runs to the line before the next heading of the same or a higher level)."""
    lines = text.splitlines()
    heads: list[tuple[int, str, int]] = []
    in_fence = False
    for i, ln in enumerate(lines, start=1):
        if ln.lstrip().startswith(("```", "~~~")):
            in_fence = not in_fence
            continue
        h = None if in_fence else _HEADING.match(ln)
        if h:
            heads.append((len(h.group(1)), h.group(2).strip(), i))
    out = []
    for k, (level, name, start) in enumerate(heads):
        end = len(lines)
        for level2, _n, start2 in heads[k + 1 :]:
            if level2 <= level:
                end = start2 - 1
                break
        out.append((level, name, start, end))
    return out


def render(entries: list[Entry], generated: str, source: str = "") -> str:
    """The directory file for ``entries`` in the format :func:`parse` reads."""
    head = [
        "# KI-Verzeichnis",
        f"<!-- generated by scripts/dev/make_kb_index.py on {generated}"
        + (f" from {source}" if source else "")
        + "; do not edit by hand -->",
        "",
        "Host/Dienst -> Abschnitte der Notizen (mit Zeilen), damit die KI gezielt nachschlägt.",
        "",
    ]
    for e in entries:
        head.append(f"## {e.name}")
        if e.aliases:
            head.append("aliases: " + ", ".join(e.aliases))
        for r in e.refs:
            span = "" if r.start is None else f":{r.start}-{r.end}" if r.end else f":{r.start}"
            head.append(f"- `{r.path}{span}`" + (f" § {r.label}" if r.label else ""))
        head.append("")
    return "\n".join(head).rstrip() + "\n"
