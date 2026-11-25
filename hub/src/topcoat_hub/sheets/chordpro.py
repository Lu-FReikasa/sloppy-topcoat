"""ChordPro read and written, and the sheet it describes.

A :class:`Sheet` holds its chords in **sounding pitch**. The capo is a way to show it: written with
``view="capo"`` the file is standard ChordPro (``{capo: 2}`` and the shapes played with it); the
stored form (``view="sounding"``) keeps the real chords and names the capo as ``{x_capo: 2}``
(custom directives start with ``x_`` in the ChordPro spec). Reading follows the same rule: under
``{capo}`` the chords are shapes and become sounding, under ``{x_capo}`` they already are.

Sections come from ``{start_of_X}``/``{end_of_X}`` (any X, plus the short forms ``soc``, ``sov``,
``sob``) and from a comment that is a section name (``{c: Chorus}``, ``{c: Strophe 2}``), which many
sheets use instead. ``{chorus}`` repeats the last chorus: it is a step of the flow, not new lines.
"""

import re
from dataclasses import dataclass, field

from topcoat_hub.sheets import keys
from topcoat_hub.sheets.chords import Chord, Key, parse_chord, uses_german_names

KINDS = (
    "intro", "verse", "pre_chorus", "chorus", "bridge", "solo", "riff", "interlude", "outro", "tab",
)  # fmt: skip

# How sheets name their parts (English and German); the first match wins, so "pre-chorus" is
# matched before "chorus".
_KIND_WORDS: tuple[tuple[str, str], ...] = (
    (
        "pre_chorus",
        r"pre[\s_-]?chorus|prechorus|pre[\s_-]?refrain|vor[\s_-]?refrain|build[\s_-]?up|"
        r"pre[\s_-]?hook",
    ),
    ("intro", r"intro|einleitung|vorspiel"),
    ("verse", r"verse|vers|strophe|str\.?"),
    ("chorus", r"chorus|refrain|ref\.?|hook"),
    ("bridge", r"bridge|br\.?|middle\s?8"),
    ("solo", r"solo|guitar solo|gitarrensolo"),
    ("riff", r"(?:main |lead |guitar |intro |haupt)?riff|lick"),
    ("interlude", r"interlude|instrumental|zwischenspiel|break"),
    ("outro", r"outro|coda|ending|schluss|nachspiel"),
)
_SHORT = {
    "t": "title",
    "st": "subtitle",
    "c": "comment",
    "ci": "comment_italic",
    "cb": "comment_box",
    "soc": "start_of_chorus",
    "eoc": "end_of_chorus",
    "sov": "start_of_verse",
    "eov": "end_of_verse",
    "sob": "start_of_bridge",
    "eob": "end_of_bridge",
    "sot": "start_of_tab",
    "eot": "end_of_tab",
}
_DIRECTIVE = re.compile(r"^\{\s*([A-Za-z_][\w-]*)\s*(?:[:\s]\s*(.*?))?\s*\}$")
_CHORD_TOKEN = re.compile(r"\[([^\]]*)\]")


def kind_of(label: str) -> str | None:
    """The part a heading names (``"Refrain 2"`` → ``chorus``), or ``None``."""
    t = label.strip().strip("[]():").strip().lower()
    t = re.sub(r"\s*\(?(?:x\s?\d+|\d+\s?x)\)?$", "", t)  # "Chorus x2"
    # "Verse 2", "Verse 1, 2", "Verse 1 & 2", "Strophe II"
    t = re.sub(r"[\s.]*(?:(?:\d+|[ivx]+)(?:\s*[,&+/]\s*(?:\d+|[ivx]+))*)?\s*[:.]?$", "", t).strip()
    for part in (t, t.split("/")[0].strip()):  # "Verse/Chorus 1": a verse that runs into it
        for kind, words in _KIND_WORDS:
            if re.fullmatch(rf"(?:{words})", part):
                return kind
    return None


@dataclass
class Segment:
    """Text that follows a chord: ``[G]Hello`` is one segment. ``chord`` is ``None`` before the
    first chord of a line, ``raw`` keeps an unparsed bracket (``[N.C.]``, ``[*Riff]``)."""

    text: str
    chord: Chord | None = None
    raw: str | None = None


@dataclass
class Line:
    segments: list[Segment] = field(default_factory=list)
    comment: str | None = None  # a comment line inside a section ({c: ...} that is no heading)

    @property
    def lyrics(self) -> str:
        return "".join(s.text for s in self.segments)

    @property
    def chords(self) -> list[Chord]:
        return [s.chord for s in self.segments if s.chord is not None]


@dataclass
class Section:
    kind: str  # one of KINDS or "other"
    label: str = ""
    lines: list[Line] = field(default_factory=list)
    repeat_of: int | None = None  # index of the section this one repeats ({chorus})


@dataclass
class Sheet:
    title: str = ""
    artist: str = ""
    key: Key | None = None  # sounding key
    capo: int = 0
    meta: dict[str, str] = field(default_factory=dict)  # tempo, time, x_* … as written
    sections: list[Section] = field(default_factory=list)

    def chords(self) -> list[Chord]:
        return [
            c for s in self.sections if s.repeat_of is None for ln in s.lines for c in ln.chords
        ]

    def flow(self) -> list[tuple[str, str]]:
        """The order of the parts as played: ``[("intro", "Intro"), ("verse", "Verse 1"), …]``."""
        return [(s.kind, s.label) for s in self.sections]

    def transpose(self, semitones: int) -> "Sheet":
        """A copy that sounds ``semitones`` higher (the capo stays: it is how it is played)."""
        out = Sheet(self.title, self.artist, self.key.transpose(semitones) if self.key else None)
        out.capo, out.meta = self.capo, dict(self.meta)
        for s in self.sections:
            lines = [
                Line(
                    [
                        Segment(g.text, g.chord.transpose(semitones) if g.chord else None, g.raw)
                        for g in ln.segments
                    ],
                    ln.comment,
                )
                for ln in s.lines
            ]
            out.sections.append(Section(s.kind, s.label, lines, s.repeat_of))
        return out


def _default_label(kind: str, sections: list[Section]) -> str:
    n = sum(1 for s in sections if s.kind == kind and s.repeat_of is None) + 1
    name = {"pre_chorus": "Pre-Chorus"}.get(kind, kind.capitalize())
    return f"{name} {n}" if kind == "verse" else name


def parse_line(text: str, german: bool = False) -> Line:
    segments: list[Segment] = []
    pos = 0
    pending: tuple[Chord | None, str | None] | None = None
    for m in _CHORD_TOKEN.finditer(text):
        before = text[pos : m.start()]
        if pending is None:
            if before:
                segments.append(Segment(before))
        else:
            segments.append(Segment(before, pending[0], pending[1]))
        name = m.group(1).strip()
        chord = parse_chord(name, german)
        pending = (chord, None if chord else name)
        pos = m.end()
    rest = text[pos:]
    if pending is None:
        segments.append(Segment(rest))
    else:
        segments.append(Segment(rest, pending[0], pending[1]))
    return Line(segments)


def parse(text: str) -> Sheet:
    """A ChordPro text as a sheet in sounding pitch."""
    lines = text.replace("\r\n", "\n").split("\n")
    german = uses_german_names(_CHORD_TOKEN.findall(text))
    sheet = Sheet()
    shape_capo = 0  # {capo}: the chords are shapes
    current: Section | None = None
    explicit = False  # inside {start_of_X} … {end_of_X}

    def open_section(kind: str, label: str) -> Section:
        sec = Section(kind, label or _default_label(kind, sheet.sections))
        sheet.sections.append(sec)
        return sec

    for raw in lines:
        stripped = raw.strip()
        if stripped.startswith("#"):
            continue
        d = _DIRECTIVE.match(stripped)
        if d:
            name = _SHORT.get(d.group(1).lower(), d.group(1).lower())
            value = (d.group(2) or "").strip()
            if name == "title":
                sheet.title = value
            elif name in ("artist", "subtitle") and not sheet.artist:
                sheet.artist = value
            elif name == "key":
                sheet.key = Key.parse(value, german)
            elif name == "capo":
                shape_capo = _int(value)
                sheet.capo = shape_capo
            elif name == "x_capo":
                sheet.capo = _int(value)
            elif name.startswith("start_of_"):
                kind = name[len("start_of_") :]
                kind = kind if kind in KINDS else (kind_of(kind) or "other")
                current, explicit = open_section(kind, value), True
            elif name == "x_part":
                # a part the source names in its own words ("[Slow down]"): a part, not lyrics
                current, explicit = open_section(kind_of(value) or "other", value), False
            elif name.startswith("end_of_"):
                current, explicit = None, False
            elif name == "chorus":
                last = next(
                    (
                        i
                        for i in range(len(sheet.sections) - 1, -1, -1)
                        if sheet.sections[i].kind == "chorus"
                        and sheet.sections[i].repeat_of is None
                    ),
                    None,
                )
                label = value or (sheet.sections[last].label if last is not None else "Chorus")
                sheet.sections.append(Section("chorus", label, [], last))
                current, explicit = None, False
            elif name.startswith("comment"):
                kind = kind_of(value)
                if kind and not explicit:
                    current = open_section(kind, value)
                else:
                    if current is None:
                        current = open_section("other", "")
                    current.lines.append(Line(comment=value))
            else:
                sheet.meta[name] = value
            continue
        if not stripped:
            if current is not None and not explicit and current.lines:
                current = None  # a blank line ends an implicit part
            continue
        if current is None:
            current = open_section("other", "")
        current.lines.append(parse_line(raw.rstrip(), german))

    if shape_capo:
        # Sheets disagree whether {key} names the shapes or what sounds: if the shapes are in the
        # named key, it named the shapes.
        named = sheet.key
        shapes, _ = keys.estimate(sheet.chords())
        sounding = sheet.transpose(shape_capo)
        if named is not None and (shapes is None or shapes.tonic != named.tonic):
            sounding.key = named
        sheet = sounding
    return sheet


def _int(value: str) -> int:
    m = re.search(r"\d+", value)
    return int(m.group()) if m else 0


def shape_key(sheet: Sheet) -> Key | None:
    """The key of the shapes played with the capo."""
    return sheet.key.transpose(-sheet.capo) if sheet.key else None


def write(sheet: Sheet, view: str = "sounding") -> str:
    """ChordPro text. ``view="sounding"`` is the stored form; ``"capo"`` writes the shapes."""
    if view not in ("sounding", "capo"):
        raise ValueError(f"unknown view {view!r}")
    shown = sheet.transpose(-sheet.capo) if view == "capo" and sheet.capo else sheet
    key = shape_key(sheet) if view == "capo" else sheet.key
    flats = key.flats if key else False
    out: list[str] = []
    if sheet.title:
        out.append(f"{{title: {sheet.title}}}")
    if sheet.artist:
        out.append(f"{{artist: {sheet.artist}}}")
    if sheet.key:
        out.append(f"{{key: {sheet.key.name}}}")
    if sheet.capo:
        out.append(f"{{capo: {sheet.capo}}}" if view == "capo" else f"{{x_capo: {sheet.capo}}}")
    out.extend(f"{{{k}: {v}}}" for k, v in sheet.meta.items())
    for sec in shown.sections:
        out.append("")
        if sec.repeat_of is not None:
            out.append(f"{{chorus: {sec.label}}}")
            continue
        tag = sec.kind if sec.kind in KINDS else "part"
        out.append(f"{{start_of_{tag}: {sec.label}}}" if sec.label else f"{{start_of_{tag}}}")
        for ln in sec.lines:
            if ln.comment is not None:
                out.append(f"{{comment: {ln.comment}}}")
                continue
            parts = []
            for g in ln.segments:
                if g.chord is not None:
                    parts.append(f"[{g.chord.name(flats)}]")
                elif g.raw is not None:
                    parts.append(f"[{g.raw}]")
                parts.append(g.text)
            out.append("".join(parts))
        out.append(f"{{end_of_{tag}}}")
    return "\n".join(out).strip() + "\n"
