"""A "chords over lyrics" page turned into ChordPro.

Most chord pages print a line of chords above each line of lyrics; where a chord stands over the
lyrics says on which syllable it changes. A model loses those columns, so this is done here: a line
whose words are all chords (or bar lines, ``x2``, ``N.C.``) is a chord line, and its chords are put
into the lyric line below at their columns. A chord line without lyrics below stays a line of
chords. ``Capo 2``, ``Key: A``, ``Tonart``/``Kapo`` lines become directives; a heading
(``[Chorus]``, ``Verse 1:``, ``Refrain``) starts a part.

Tab lines (``e|--3--``) are kept as comments: they are no lyrics and no chords.
"""

import re

from topcoat_hub.sheets.chordpro import kind_of
from topcoat_hub.sheets.chords import parse_chord

_FILLER = re.compile(
    r"^(?:\|+|:?\|\|?:?|[-\N{EN DASH}/%.]+|\(?x\s?\d+\)?|\(?\d+x\)?|n\.?c\.?|\(|\))$", re.I
)
_TAB = re.compile(r"^\s*[eBGDAEbgdah]?\s*\|[-0-9|hpbrx/\\~^().\s]{4,}\|?\s*$")
_META = (
    ("capo", re.compile(r"^\s*(?:capo|kapo)\b\s*[:=]?\s*(?:on\s+|im\s+|auf\s+)?(\d+)", re.I)),
    (
        "key",
        re.compile(
            r"^\s*(?:key|tonart)\s*[:=]\s*([A-H][#b]?m?(?:\s*(?:major|minor|dur|moll))?)\s*$", re.I
        ),
    ),
    ("tuning", re.compile(r"^\s*(?:tuning|stimmung)\s*[:=]\s*(.+?)\s*$", re.I)),
    ("tempo", re.compile(r"^\s*(?:tempo|bpm)\s*[:=]\s*(\d+)", re.I)),
)


# a chord's fingering in a legend ("Em7  0-2-2-0-3-3", "G 320003"): neither lyrics nor chords
_FINGERING = re.compile(r"^\s*\S+\s+(?:[x0-9]{1,2}(?:[-\s][x0-9]{1,2}){3,6}|[x0-9]{4,6})\s*$", re.I)


# a tabber's header ("Artist: Oasis", "Tab by: …"): no lyrics; the research knows these facts
_HEADER = re.compile(
    r"^\s*(?:artist|song|title|album|tab(?:bed)? by|transcribed by|chords by|difficulty|author|"
    r"interpret|titel|written by|composer|tempo|strumming|rhythm)\s*:",
    re.I,
)
# a remark in brackets on a line of its own ("(NOTE: play it softly)"): a comment, no lyrics
_REMARK = re.compile(r"^\s*[(*].*[)*]\s*$")
# a line counting the beats ("1 + 2 + 3 + 4 +"): no lyrics
_COUNT = re.compile(r"^[\s\d+&]*\d\s*\+[\s\d+&]*$")
# a part the page names in brackets on a line of its own ("[Slow down]", "[Main Riff]")
_BRACKETED = re.compile(r"^\s*\[([^\]\[]{2,30})\]\s*$")
# a remark after the chords of a line ("C  G  (to fade)")
_TRAILING_REMARK = re.compile(r"^(.*?\S)(\s+\([^()]*[A-Za-z]{2}[^()]*\))\s*$")


def _remark_of(line: str) -> str:
    m = _TRAILING_REMARK.match(line)
    return m.group(2).strip() if m and not _FILLER.match(m.group(2).strip()) else ""


def _without_remark(line: str) -> str:
    remark = _remark_of(line)
    return line[: line.rstrip().rfind(remark)].rstrip() if remark else line


def chord_lines(text: str) -> int:
    """How many lines of a text are chord lines (to tell a chord sheet from other text)."""
    return sum(1 for ln in text.split("\n") if _chord_tokens(ln.expandtabs(8)) is not None)


def _chord_tokens(line: str) -> list[tuple[int, str]] | None:
    """The chords of a chord line with their columns, or ``None`` if it is no chord line."""
    line = _without_remark(line)
    tokens = [(m.start(), m.group()) for m in re.finditer(r"\S+", line)]
    if not tokens:
        return None
    chords = []
    for col, tok in tokens:
        bare = tok.strip("()*")  # "Gm7*": another voicing of the same chord
        if parse_chord(bare) is not None:
            chords.append((col + (1 if tok.startswith("(") else 0), bare))
        elif not _FILLER.match(tok):
            return None
    return chords or None


def _heading(line: str) -> str | None:
    t = line.strip()
    if not t or len(t) > 40:
        return None
    return t.strip("[]():").strip() if kind_of(t) else None


def _merge(chords: list[tuple[int, str]], lyric: str) -> str:
    out = lyric
    for col, name in sorted(chords, reverse=True):
        # a chord printed over the gap between two words belongs to the next word
        if 0 < col < len(out) and out[col] == " " and out[col - 1] != " ":
            nxt = len(out) - len(out[col:].lstrip(" "))
            col = nxt if nxt < len(out) else col
        if col > len(out):
            out = out + " " * (col - len(out))
        out = out[:col] + f"[{name}]" + out[col:]
    return out


_REPEAT = re.compile(r"\(?(?:x\s?(\d+)|(\d+)\s?x)\)?", re.I)


def _chords_only(chords: list[tuple[int, str]], line: str = "") -> str:
    """A line of chords; a repeat mark on it ("x4") and a remark ("(to fade)") stay as text."""
    out = " ".join(f"[{name}]" for _, name in chords)
    if _remark_of(line):
        return f"{out} {_remark_of(line)}"
    m = next((m for m in _REPEAT.finditer(line) if line[m.start() - 1 : m.start()].isspace()), None)
    return f"{out} x{m.group(1) or m.group(2)}" if m else out


def to_chordpro(text: str, title: str = "", artist: str = "") -> str:
    """ChordPro for a plain chord page (chords in the shapes it shows; a capo line becomes
    ``{capo}``, so :func:`topcoat_hub.sheets.chordpro.parse` turns them into sounding chords)."""
    lines = [ln.expandtabs(8).rstrip() for ln in text.replace("\r\n", "\n").split("\n")]
    out: list[str] = []
    if title:
        out.append(f"{{title: {title}}}")
    if artist:
        out.append(f"{{artist: {artist}}}")
    body: list[str] = []
    i = 0
    while i < len(lines):
        line = lines[i]
        meta = next(((name, m.group(1)) for name, rx in _META if (m := rx.match(line))), None)
        if meta:
            out.append(f"{{{meta[0] if meta[0] != 'tuning' else 'x_tuning'}: {meta[1]}}}")
            i += 1
            continue
        if _TAB.match(line):
            body.append(f"{{comment: {line.strip()}}}")
            i += 1
            continue
        if _FINGERING.match(line) and parse_chord(line.split()[0]) is not None:
            i += 1  # a chord legend: the chords come again where they are played
            continue
        if _HEADER.match(line) or _COUNT.match(line):
            i += 1
            continue
        if _REMARK.match(line) and _chord_tokens(line) is None:
            body.append(f"{{comment: {line.strip()}}}")
            i += 1
            continue
        # "Intro: G D Em C", "[Intro] C C/E | F G"
        inline = re.match(r"^\s*\[?([^:\]]{2,30})\]?\s*:\s*(\S.*)$", line) or re.match(
            r"^\s*\[([^:\]]{2,30})\]\s*(\S.*)$", line
        )
        if inline and kind_of(inline.group(1)) and _chord_tokens(inline.group(2)) is not None:
            # "Intro: G D Em C": a heading and its chords on one line
            body.extend(["", f"{{comment: {inline.group(1).strip()}}}"])
            body.append(_chords_only(_chord_tokens(inline.group(2)) or [], inline.group(2)))
            i += 1
            continue
        heading = _heading(line)
        if heading:
            body.extend(["", f"{{comment: {heading}}}"])
            i += 1
            continue
        named = _BRACKETED.match(line)
        if named and parse_chord(named.group(1)) is None:
            body.extend(["", f"{{x_part: {named.group(1).strip()}}}"])
            i += 1
            continue
        chords = _chord_tokens(line)
        if chords is not None:
            nxt = lines[i + 1] if i + 1 < len(lines) else ""
            lyric_below = (
                nxt.strip()
                and _chord_tokens(nxt) is None
                and not _heading(nxt)
                and not _TAB.match(nxt)
            )
            if lyric_below:
                body.append(_merge(chords, nxt))
                i += 2
            else:
                body.append(_chords_only(chords, line))
                i += 1
            continue
        body.append(line)
        i += 1
    return _joined_syllables("\n".join([*out, *body]).strip()) + "\n"


_SYLLABLE_HYPHEN = re.compile(r"(?<=[a-z])-((?:\[[^\]]*\])?)(?=[a-z])")


def _joined_syllables(text: str) -> str:
    """Words split into syllables ("a-round the cor-ner") joined again where a page does that
    throughout (to set chords on syllables): the words are what the other sources and the
    reference lyrics hold. A page with a few hyphens ("ahhh-ahhh") keeps them."""
    lines = text.split("\n")
    lyric = [ln for ln in lines if not ln.startswith("{")]
    words = sum(len(ln.split()) for ln in lyric)
    hyphens = sum(len(_SYLLABLE_HYPHEN.findall(ln)) for ln in lyric)
    if hyphens < 10 or hyphens < 0.08 * words:
        return text
    return "\n".join(ln if ln.startswith("{") else _SYLLABLE_HYPHEN.sub(r"\1", ln) for ln in lines)
