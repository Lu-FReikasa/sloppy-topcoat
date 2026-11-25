"""A chord: root and bass as pitch classes, the rest of its name kept as written.

Names are international (``B`` and ``Bb``). A source in German names (``H`` for B, ``B`` for Bb) is
read with ``german=True``; :func:`uses_german_names` tells from a list of chord names whether a
source does. Spelling (``A#`` or ``Bb``) is not stored: it follows the key the chord is shown in.
"""

import re
from dataclasses import dataclass, replace

SHARP_NAMES = ("C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B")
FLAT_NAMES = ("C", "Db", "D", "Eb", "E", "F", "Gb", "G", "Ab", "A", "Bb", "B")
_LETTER = {"C": 0, "D": 2, "E": 4, "F": 5, "G": 7, "A": 9, "B": 11, "H": 11}
_ACCIDENTAL = {"": 0, "#": 1, "♯": 1, "b": -1, "♭": -1, "##": 2, "bb": -2}

# Relative majors whose key signature has flats (C counts: pop charts in C write Bb, Eb, Ab).
_FLAT_MAJORS = frozenset({0, 5, 10, 3, 8, 1})

_NOTE = r"[A-H](?:##|bb|[#♯b♭])?"
_CHORD_RE = re.compile(rf"^({_NOTE})(.*?)(?:/({_NOTE}))?$")
# What may follow the root: qualities, extensions, alterations. "Chorus" or "Am I" are not chords.
_SUFFIX_RE = re.compile(
    r"^(?:maj|Maj|min|dim|aug|sus|add|alt|no|omit|m|M|[0-9]|[#b+\-\N{MINUS SIGN}()°øΔ^,.])*$"
)


def note_pc(name: str, german: bool = False) -> int:
    """Pitch class (0 = C) of a note name; ``ValueError`` if it is none."""
    name = name.strip()
    if not name or name[0] not in _LETTER:
        raise ValueError(f"not a note: {name!r}")
    letter, acc = name[0], name[1:]
    if acc not in _ACCIDENTAL:
        raise ValueError(f"not a note: {name!r}")
    if german and letter == "B" and acc == "":
        return 10  # German B is Bb
    if german and letter == "H" and acc in ("b", "♭"):
        return 10
    return (_LETTER[letter] + _ACCIDENTAL[acc]) % 12


def prefers_flats(tonic: int, minor: bool) -> bool:
    """Whether a key writes its black keys as flats (by its relative major's key signature)."""
    major = (tonic + 3) % 12 if minor else tonic % 12
    return major in _FLAT_MAJORS


def spell(pc: int, flats: bool) -> str:
    return (FLAT_NAMES if flats else SHARP_NAMES)[pc % 12]


@dataclass(frozen=True)
class Key:
    tonic: int
    minor: bool = False

    @property
    def flats(self) -> bool:
        return prefers_flats(self.tonic, self.minor)

    @property
    def name(self) -> str:
        return spell(self.tonic, self.flats) + ("m" if self.minor else "")

    def transpose(self, semitones: int) -> "Key":
        return Key((self.tonic + semitones) % 12, self.minor)

    @classmethod
    def parse(cls, text: str, german: bool = False) -> "Key | None":
        """``"A"``, ``"F#m"``, ``"Bb minor"``, ``"E-Dur"``, ``"c-moll"``; ``None`` if it is none."""
        t = text.strip().replace("-", " ")
        m = re.match(rf"^({_NOTE})\s*(.*)$", t[:1].upper() + t[1:])
        if not m:
            return None
        rest = m.group(2).strip().lower()
        if rest in ("", "maj", "major", "dur"):
            minor = False
        elif rest in ("m", "min", "minor", "moll"):
            minor = True
        else:
            return None
        try:
            return cls(note_pc(m.group(1), german), minor)
        except ValueError:
            return None


@dataclass(frozen=True)
class Chord:
    root: int
    suffix: str = ""
    bass: int | None = None

    @property
    def triad(self) -> str:
        """The chord's family: ``maj``, ``min``, ``dim``, ``aug``, ``sus2``, ``sus4`` or ``5``."""
        s = self.suffix
        if s.startswith(("dim", "°", "ø")) or s.startswith("m7b5") or s.startswith("m7-5"):
            return "dim"
        if s.startswith("min") or (s.startswith("m") and not s.startswith("maj")):
            return "min"
        if s.startswith(("aug", "+")):
            return "aug"
        if "sus2" in s:
            return "sus2"
        if "sus" in s:
            return "sus4"
        if s == "5":
            return "5"
        return "maj"

    @property
    def extensions(self) -> str:
        """What the suffix adds to its family (``7``, ``maj7``, ``add9`` …), for "simplified"."""
        s = self.suffix
        for head in ("min", "dim", "aug", "m", "°", "ø", "+"):
            if s.startswith(head) and not s.startswith("maj"):
                s = s[len(head) :]
                break
        return s.replace("sus4", "").replace("sus2", "").replace("sus", "")

    def transpose(self, semitones: int) -> "Chord":
        bass = None if self.bass is None else (self.bass + semitones) % 12
        return replace(self, root=(self.root + semitones) % 12, bass=bass)

    def name(self, flats: bool = False) -> str:
        out = spell(self.root, flats) + self.suffix
        if self.bass is not None:
            out += "/" + spell(self.bass, flats)
        return out

    def in_key(self, key: Key) -> str:
        return self.name(key.flats)


def parse_chord(text: str, german: bool = False) -> Chord | None:
    """A chord name, or ``None`` when the text is no chord (``N.C.``, ``x2``, a word)."""
    t = text.strip()
    m = _CHORD_RE.match(t)
    if not m or not _SUFFIX_RE.match(m.group(2)) or "--" in m.group(2) or "mm" in m.group(2):
        # "C------" is a chord held over bars of a tab, "Hmmm" a hum: no chord names
        return None
    try:
        root = note_pc(m.group(1), german)
        bass = note_pc(m.group(3), german) if m.group(3) else None
    except ValueError:
        return None
    return Chord(root, _normal_suffix(m.group(2)), bass)


def _normal_suffix(s: str) -> str:
    """One spelling for the same quality, so ``Amin`` and ``Am`` compare equal."""
    s = s.replace("\N{MINUS SIGN}", "-").replace("Δ", "maj").replace("^", "maj")
    if s.startswith("min"):
        s = "m" + s[3:]
    elif s.startswith("Maj"):
        s = "maj" + s[3:]
    elif s.startswith("M"):
        s = "maj" + s[1:]
    if s == "maj":
        s = ""
    return s


def uses_german_names(names: list[str]) -> bool:
    """Whether a source writes German note names: it has an ``H`` chord (no English chart does)."""
    return any(re.match(r"^H(?![a-z]{2})", n.strip()) for n in names)
