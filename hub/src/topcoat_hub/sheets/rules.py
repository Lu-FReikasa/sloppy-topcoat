"""Rules that clean one sheet on its own, before (and without) the other sources of the song.

Imported sheets carry the same patterns over and over; they are fixed here as rules, not per song:

``chord lines``  a line written in chord names but outside ``[...]`` ("Intro: C F G", ``{comment:
                 Am  G  F}``, a line of chords in a ChordPro file) gets real chords, so the
                 transposition and the instrument view cover it. A line with a word that is no chord
                 (:func:`topcoat_hub.sheets.chords.parse_chord`) stays text; a heading in front
                 ("Intro:") and bar lines or repeat marks ("|", "x2") may stand with the chords.
``tab blocks``   tab staff lines (``e|--3--``) are a part of kind ``tab`` (``{start_of_tab}``), no
                 lyrics and no comments, so a reader can show or hide them.
``header``       the lines before the first chord that name the title, artist, tuning, source or a
                 credit ("Title: …", "by …", "Standard tuning", a web address) go: title, artist,
                 key, tempo and tuning into the sheet's facts when it has none, the rest away.

Every rule leaves a sheet it has cleaned as it is, so running them twice changes nothing.
"""

import re
from dataclasses import dataclass, field

from topcoat_hub.sheets.chordpro import Line, Section, Segment, Sheet, kind_of, parse_line
from topcoat_hub.sheets.chords import Key, parse_chord, uses_german_names
from topcoat_hub.sheets.plaintext import _FILLER, _TAB, _without_remark

# the parts a tab block inside them is named after ("Riff" as tab, not "Tab")
_PLAYED = ("intro", "solo", "riff", "interlude", "outro")
# "Intro: C F G", "[Riff] Am G": a heading in front of the chords
_LABELLED = re.compile(r"^(\s*\[?\s*)([^:\]\[]{2,30}?)(\s*\]?\s*:\s*|\s*\]\s*)(\S.*)$")


@dataclass
class Applied:
    chord_lines: int = 0  # lines whose chords were plain text
    tabs: int = 0  # tab blocks made a part of their own
    removed: list[str] = field(default_factory=list)  # header lines dropped or made facts


def apply(sheet: Sheet, german: bool = False, shapes: int = 0) -> Applied:
    """Clean ``sheet`` in place; what was done, for the report. ``german``: the source writes German
    note names (its ``[...]`` chords had an ``H``), so a plain ``B`` is a B flat. ``shapes``: the
    source's ``{capo}`` (its chords are shapes, ``chordpro.shape_capo``); under ``{x_capo}`` they
    sound as written."""
    done = Applied()
    _chord_lines(sheet, done, german, shapes)
    _tab_blocks(sheet, done)
    _header(sheet, done)
    return done


# --- chord lines -----------------------------------------------------------------------------


def _plain(ln: Line) -> str | None:
    """The text of a line without any chord or bracket, else ``None``."""
    if ln.comment is not None:
        return ln.comment
    if any(g.chord is not None or g.raw is not None for g in ln.segments):
        return None
    return ln.lyrics


def _chord_marks(text: str, german: bool) -> list[tuple[int, int]] | None:
    """Where the chords of a line of chords stand (start, end), or ``None`` if it is none."""
    body = _without_remark(text)  # "C  G  (to fade)": the remark stays text
    marks = []
    for m in re.finditer(r"\S+", body):
        tok = m.group()
        bare = tok.strip("()*")  # "(Am)", "Gm7*"
        if bare and parse_chord(bare, german) is not None:
            start = m.start() + tok.index(bare)
            marks.append((start, start + len(bare)))
        elif not _FILLER.match(tok):
            return None
    return marks or None


def _as_chord_line(text: str, german: bool) -> tuple[str, str] | None:
    """A line of plain chords with its chords in brackets, and the heading in front of it ("" if
    none): ``"Intro: C F"`` → ``("Intro", "[C] [F]")``; ``None`` if it is no line of chords."""
    label = ""
    m = _LABELLED.match(text)
    if m and kind_of(m.group(2)) and _chord_marks(m.group(4), german):
        label, text = m.group(2).strip(), m.group(4)
    marks = _chord_marks(text, german)
    if marks is None:
        return None
    for start, end in reversed(marks):
        text = f"{text[:start]}[{text[start:end]}]{text[end:]}"
    return label, text.rstrip()


def _german(sheet: Sheet) -> bool:
    """Whether the plain chords are German names (an ``H`` among them)."""
    names = []
    for sec in sheet.sections:
        for ln in sec.lines:
            text = _plain(ln) or ""
            m = _LABELLED.match(text)
            text = m.group(4) if m and kind_of(m.group(2)) else text
            names.extend(text[a:b] for a, b in _chord_marks(text, True) or [])
    return uses_german_names(names)


def _sounding(ln: Line, capo: int) -> Line:
    """Plain chords are written as the source wrote them: the shapes when it plays with a capo (the
    chords in ``[...]`` were moved to sounding pitch already, ``chordpro.parse``)."""
    if not capo:
        return ln
    segs = [
        Segment(g.text, g.chord.transpose(capo) if g.chord else None, g.raw) for g in ln.segments
    ]
    return Line(segs, ln.comment)


def _chord_lines(sheet: Sheet, done: Applied, german: bool, shapes: int) -> None:
    german = german or _german(sheet)
    out: list[Section] = []
    for sec in sheet.sections:
        out.append(sec)
        if sec.kind == "tab" or sec.repeat_of is not None:
            continue
        lines, sec.lines = sec.lines, []
        part, implicit = sec, _unnamed(sec)
        for ln in lines:
            text = _plain(ln)
            found = None if text is None or _TAB.match(text) else _as_chord_line(text, german)
            if found is None:
                part.lines.append(ln)
                continue
            label, chords = found
            if label and implicit:
                # "Intro: C F G" in a part the sheet does not name: the heading starts a part
                if part.lines:
                    part = Section("other", "")
                    out.append(part)
                part.kind, part.label = kind_of(label) or "other", label
            elif label:
                chords = f"{label}: {chords}"
            part.lines.append(_sounding(parse_line(chords, german), shapes))
            done.chord_lines += 1
    _replace(sheet, out)


def _unnamed(sec: Section) -> bool:
    """A part the sheet does not name (``parse`` calls it "Other")."""
    return (
        sec.kind == "other"
        and re.fullmatch(r"(?:other(?:\s*\d+)?)?", sec.label.strip(), re.I) is not None
    )


# --- tab blocks ------------------------------------------------------------------------------


def _is_tab(ln: Line) -> bool:
    text = _plain(ln)
    return text is not None and bool(_TAB.match(text))


def _tab_blocks(sheet: Sheet, done: Applied) -> None:
    out: list[Section] = []
    for sec in sheet.sections:
        if sec.kind == "tab" or sec.repeat_of is not None or not any(map(_is_tab, sec.lines)):
            out.append(sec)
            continue
        name = sec.label if sec.kind in _PLAYED and sec.label else "Tab"
        # a part of tab and comments ("(let ring)") is one tab block: the comments stay in it
        if all(_is_tab(ln) or ln.comment is not None for ln in sec.lines):
            prev = out[-1] if out else None
            if _unnamed(sec) and prev is not None and prev.kind == "tab" and prev.repeat_of is None:
                prev.lines.extend(_staff(ln) for ln in sec.lines)  # the next staves of a block
                continue
            sec.kind, sec.label = "tab", name
            sec.lines = [_staff(ln) for ln in sec.lines]
            _add(out, sec, done)
            continue
        # The part keeps all its other lines in one piece: split in two, a {chorus} would repeat
        # only one half of it, and a second "Verse 1" would not pair with the other sources. A tab
        # block at its start stands before it, any other after it.
        lead = 0
        while lead < len(sec.lines) and _is_tab(sec.lines[lead]):
            lead += 1
        before = [_staff(ln) for ln in sec.lines[:lead]]
        after = [_staff(ln) for ln in sec.lines[lead:] if _is_tab(ln)]
        sec.lines = [ln for ln in sec.lines[lead:] if not _is_tab(ln)]
        if before:
            _add(out, Section("tab", name, before), done)
        out.append(sec)
        if after:
            _add(out, Section("tab", name, after), done)
    _replace(sheet, out)


def _staff(ln: Line) -> Line:
    """A tab line as a line of its own (no comment): the tab block shows it as it is."""
    return Line([Segment(ln.comment)]) if ln.comment is not None and _is_tab(ln) else ln


def _add(out: list[Section], sec: Section, done: Applied) -> None:
    """A tab block; one right after another of the same name (the next staves) joins it."""
    prev = out[-1] if out else None
    if (
        prev is not None
        and prev.kind == "tab"
        and prev.repeat_of is None
        and prev.label == sec.label
    ):
        prev.lines.extend(sec.lines)
        return
    out.append(sec)
    done.tabs += 1


def _replace(sheet: Sheet, sections: list[Section]) -> None:
    """New sections without empty parts; ``repeat_of`` follows the part it points at."""
    old = {id(s): i for i, s in enumerate(sheet.sections)}
    keep = [s for s in sections if s.repeat_of is not None or s.lines]
    new_of_old = {old[id(s)]: j for j, s in enumerate(keep) if id(s) in old}
    for s in keep:
        if s.repeat_of is not None:
            s.repeat_of = new_of_old.get(s.repeat_of)
    sheet.sections = keep


# --- header ----------------------------------------------------------------------------------

_FACT = re.compile(
    r"^(title|song(?:\s*title)?|titel|artist|band|interpret|performed by|tuning|stimmung|key|"
    r"tonart|tempo|bpm)\s*[:=]\s*(\S.*?)\s*$",
    re.I,
)
_FACT_NAMES = {
    "title": "title", "song": "title", "song title": "title", "songtitle": "title",
    "titel": "title", "artist": "artist", "band": "artist", "interpret": "artist",
    "performed by": "artist", "tuning": "tuning", "stimmung": "tuning", "key": "key",
    "tonart": "key", "tempo": "tempo", "bpm": "tempo",
}  # fmt: skip
# "Standard tuning", "Drop D tuning", "Eb tuning (half step down)"
_TUNING = re.compile(r"^(\S+(?:\s+\S+){0,3})\s+tuning\s*((?:[:(\-].*)?)$", re.I)
# the words in front of "tuning" ("we keep on tuning" is lyrics; "Drop-D", "1/2 step down" are not)
_TUNING_WORD = re.compile(
    r"^(?:standard|drop|open|half|whole|step|down|1/2|[a-g][#b]?|[a-g#b]{4,8})$", re.I
)
_SOURCE = re.compile(
    r"^(?:source|quelle|from|taken from|found (?:at|on))\s*:|https?://|www\.|"
    r"^\S+\.(?:com|net|org|de|at|ch|info|co\.uk|fr|it|es|nl)(?:/\S*)?$",
    re.I,
)
_CREDIT = re.compile(
    r"^(?:album|difficulty|author|composer)\s*:|"  # "Album of the year…" is lyrics
    r"^(?:written by|words and music|words & music|lyrics by|music by|chords? by|tab(?:bed)? by|"
    r"transcribed by|arranged by|submitted by|corrected by)\b|@",
    re.I,
)
_FILLER_WORDS = {"by", "von", "feat", "ft", "and", "the", "und", "chords", "tab", "akkorde"}


def _words(text: str) -> list[str]:
    return re.findall(r"[a-z0-9äöüß]+", text.lower().replace("'", ""))


def _names_song(text: str, sheet: Sheet) -> bool:
    """A line of the title and/or the artist and little else ("SONG - BAND", "Song by Band")."""
    title, artist = set(_words(sheet.title)), set(_words(sheet.artist))
    words = [w for w in _words(text) if w not in _FILLER_WORDS]
    if not words or not (title or artist):
        return False
    known = title | artist
    return all(w in known for w in words) and (
        (bool(title) and title <= set(words)) or (bool(artist) and artist <= set(words))
    )


def _header_line(text: str, sheet: Sheet) -> bool:
    """Whether a line before the first chord is a header line; facts it names go to the sheet."""
    t = text.strip().strip("()").strip()
    if not t:
        return False
    m = _FACT.match(t)
    if m:
        what, value = _FACT_NAMES.get(m.group(1).lower(), ""), m.group(2).strip()
        if what == "title" and not sheet.title:
            sheet.title = value
        elif what == "artist" and not sheet.artist:
            sheet.artist = value
        elif what == "key" and sheet.key is None and sheet.capo == 0:
            sheet.key = Key.parse(value)
        elif what in ("tuning", "tempo"):
            sheet.meta.setdefault("x_tuning" if what == "tuning" else "tempo", value)
        return bool(what)
    m = _TUNING.match(t)
    if m and all(_TUNING_WORD.match(w) for w in re.split(r"[\s-]+", m.group(1)) if w):
        sheet.meta.setdefault("x_tuning", (m.group(1) + m.group(2)).strip())
        return True
    return bool(_SOURCE.search(t) or _CREDIT.search(t) or _by_name(t) or _names_song(t, sheet))


def _by_name(text: str) -> bool:
    """A credit ("by Some Name", "By: J. Doe"): names are capitalised, "by the river" is lyrics."""
    m = re.match(r"^by\s*:?\s+(.+)$", text, re.I)
    names = re.findall(r"[^\W\d_][\w.'-]*", m.group(1)) if m else []
    return bool(names) and len(names) <= 5 and all(n[0].isupper() for n in names)


def _header(sheet: Sheet, done: Applied) -> None:
    """Header lines in the unnamed parts before the first chord of the sheet."""
    for sec in sheet.sections:
        if not _unnamed(sec) or sec.repeat_of is not None:
            break
        keep: list[Line] = []
        chorded = False
        for ln in sec.lines:
            chorded = chorded or any(g.chord is not None for g in ln.segments)
            text = _plain(ln)
            if not chorded and text is not None and _header_line(text, sheet):
                done.removed.append(text.strip())
            else:
                keep.append(ln)
        sec.lines = keep
        if chorded:
            break
    _replace(sheet, list(sheet.sections))
