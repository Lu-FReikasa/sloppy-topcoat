"""Two sheets of a song compared: what is the same once transposition and capo are taken out, and
exactly where they differ.

The second sheet is first moved into the pitch of the first. The shift is the one under which the
most chords agree (not the difference of the named keys: a sheet may name the relative minor, or a
wrong key), so a transposed or capo version is reported once, as such, and not as a different chord
on every line. Then the parts are paired by their order in the flow, the lines of a pair by their
lyrics, and each pair of lines is compared:

``lyrics``          other words (or a line only one of them has)
``chords``          other chords
``chord_detail``    the same chords with more or fewer extensions (``Fmaj7`` and ``F``)
``chord_position``  the same chords on other words
``form``            another order or number of parts
``key`` / ``capo``  the whole sheet is transposed, or played with another capo

Chords of the second sheet are named in the first sheet's key, so they read against each other.
"""

import re
from dataclasses import asdict, dataclass, field
from difflib import SequenceMatcher
from typing import Any

from topcoat_hub.sheets import keys
from topcoat_hub.sheets.chordpro import Line, Section, Sheet
from topcoat_hub.sheets.chords import Chord, Key


@dataclass
class Difference:
    kind: str
    where: str
    a: str
    b: str
    note: str = ""


@dataclass
class Comparison:
    key_a: str | None
    key_b: str | None
    shift: int  # semitones the second sheet sounds above the first
    capo_a: int
    capo_b: int
    lyrics_similarity: float  # 0..1
    chords_similarity: float  # 0..1, after the shift
    differences: list[Difference] = field(default_factory=list)

    def counts(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for d in self.differences:
            out[d.kind] = out.get(d.kind, 0) + 1
        return out

    def to_dict(self) -> dict[str, Any]:
        return {**asdict(self), "counts": self.counts()}


def _norm_words(text: str) -> list[str]:
    return re.findall(r"[\w']+", text.lower())


def _key_of(sheet: Sheet) -> Key | None:
    return sheet.key or keys.estimate(sheet.chords())[0]


def _signature(chords: list[Chord]) -> list[tuple[int, str]]:
    return [(c.root, c.triad) for c in chords]


def best_shift(a: Sheet, b: Sheet) -> int:
    """Semitones ``b`` sounds above ``a``: the shift under which most chords agree in order."""
    ca, cb = _signature(a.chords()), _signature(b.chords())
    if not ca or not cb:
        ka, kb = _key_of(a), _key_of(b)
        return _signed((kb.tonic - ka.tonic) if ka and kb else 0)
    scores = []
    for s in range(12):
        moved = [((r - s) % 12, t) for r, t in cb]
        scores.append(
            (SequenceMatcher(None, ca, moved, autojunk=False).ratio(), -abs(_signed(s)), s)
        )
    return _signed(max(scores)[2])


def _signed(s: int) -> int:
    s %= 12
    return s - 12 if s > 6 else s


def _lines(sheet: Sheet, sec: Section) -> list[Line]:
    if sec.repeat_of is not None and 0 <= sec.repeat_of < len(sheet.sections):
        return sheet.sections[sec.repeat_of].lines
    return sec.lines


_REPEAT_MARK = re.compile(r"x(\d+)", re.I)  # "x4" after a line of chords: play it four times


@dataclass
class _Word:
    """A word of a part with the chords that change on it (a line break plays no part)."""

    line: int  # the index of its line in the part
    text: str
    norm: str
    chords: list[Chord] = field(default_factory=list)


def _words_of(lines: list[Line]) -> tuple[list[_Word], list[tuple[int, list[Chord]]]]:
    """The words of a part with their chords, and the lines that hold chords only (bars).

    A chord belongs to the word it stands over, also in the middle of a word (``Rea[E]lised``); a
    chord after the last word of a line belongs to that word."""
    words: list[_Word] = []
    bars: list[tuple[int, list[Chord]]] = []
    for i, ln in enumerate(lines):
        text, marks = "", []
        for g in ln.segments:
            if g.chord is not None:
                marks.append((len(text), g.chord))
            text += g.text
        spans = list(re.finditer(r"[\w']+", text))
        repeat = next((int(m.group(1)) for m in _REPEAT_MARK.finditer(text)), 1)
        spans = [m for m in spans if not _REPEAT_MARK.fullmatch(m.group())]
        if not spans:
            if marks:
                bars.extend((i, [c for _, c in marks]) for _ in range(min(repeat, 16)))
            continue
        line_words = [_Word(i, m.group(), m.group().lower()) for m in spans]
        for pos, chord in marks:
            k = next((n for n, m in enumerate(spans) if m.end() > pos), len(spans) - 1)
            line_words[k].chords.append(chord)
        words.extend(line_words)
    return words, bars


def _names(chords: list[Chord], key: Key | None) -> str:
    flats = key.flats if key else False
    return " ".join(c.name(flats) for c in chords) or "-"


def _on_words(words: list[_Word], key: Key | None) -> str:
    """The chords of some words as ``[A]word``, the words without a chord left out."""
    flats = key.flats if key else False
    return " ".join(
        "".join(f"[{c.name(flats)}]" for c in w.chords) + w.text for w in words if w.chords
    )


@dataclass
class _LineChords:
    a: list[Chord] = field(default_factory=list)
    b: list[Chord] = field(default_factory=list)
    a_words: list[_Word] = field(default_factory=list)
    b_words: list[_Word] = field(default_factory=list)
    # for words both sheets have: the index of the word each chord is on (None: not comparable)
    a_at: list[int | None] = field(default_factory=list)
    b_at: list[int | None] = field(default_factory=list)


def _families(chords: list[Chord]) -> list[tuple[int, str]]:
    """Root and family of each chord, a run of the same one as one (``Fmaj7 F6`` and ``Am Am/G``
    are one chord with a movement inside it)."""
    out: list[tuple[int, str]] = []
    for c in chords:
        fam = (c.root, c.triad)
        if not out or out[-1] != fam:
            out.append(fam)
    return out


def _richness(chords: list[Chord]) -> int:
    """How much a line of chords says beyond its families: extensions, bass notes, movements."""
    return sum(len(c.extensions) + (c.bass is not None) for c in chords) + len(chords)


# a chord placed one word earlier or later is the column precision of a hand-written sheet
POSITION_TOLERANCE = 1


def _chord_difference(where: str, lc: _LineChords, key: Key | None) -> Difference | None:
    ca, cb = lc.a, lc.b
    if ca == cb:
        comparable = None not in lc.a_at and None not in lc.b_at
        moved = comparable and any(
            abs(x - y) > POSITION_TOLERANCE  # type: ignore[operator]
            for x, y in zip(lc.a_at, lc.b_at, strict=True)
        )
        if moved:
            return Difference(
                "chord_position", where, _on_words(lc.a_words, key), _on_words(lc.b_words, key),
                "same chords on other words",
            )  # fmt: skip
        return None
    if _families(ca) == _families(cb):
        richer = _richness(ca) > _richness(cb)
        note = "second is simplified" if richer else "second adds detail"
        return Difference("chord_detail", where, _names(ca, key), _names(cb, key), note)
    return Difference("chords", where, _names(ca, key), _names(cb, key))


def _compare_sections(
    label: str, la: list[Line], lb: list[Line], key: Key | None, out: list[Difference]
) -> None:
    """Two parts word by word: the lyrics by their words (a line broken elsewhere is the same
    line), the chords by the words they stand on, reported per line of the first sheet."""
    la = [ln for ln in la if ln.comment is None]
    lb = [ln for ln in lb if ln.comment is None]
    wa, bars_a = _words_of(la)
    wb, bars_b = _words_of(lb)
    per_line: dict[int, _LineChords] = {}

    def line_at(i: int) -> int:
        if i < len(wa):
            return wa[i].line
        return wa[-1].line if wa else 0

    sm = SequenceMatcher(None, [w.norm for w in wa], [w.norm for w in wb], autojunk=False)
    for op, i1, i2, j1, j2 in sm.get_opcodes():
        if op == "equal":
            for k in range(i2 - i1):
                x, y = wa[i1 + k], wb[j1 + k]
                lc = per_line.setdefault(x.line, _LineChords())
                lc.a += x.chords
                lc.b += y.chords
                lc.a_at += [i1 + k] * len(x.chords)
                lc.b_at += [i1 + k] * len(y.chords)
                if x.chords:
                    lc.a_words.append(x)
                if y.chords:
                    lc.b_words.append(y)
            continue
        line = line_at(i1) if i1 < i2 else line_at(max(i1 - 1, 0))
        note = "only in second" if i1 == i2 else "only in first" if j1 == j2 else ""
        out.append(
            Difference(
                "lyrics", f"{label}, line {line + 1}",
                " ".join(w.text for w in wa[i1:i2]), " ".join(w.text for w in wb[j1:j2]), note,
            )
        )  # fmt: skip
        lc = per_line.setdefault(line, _LineChords())
        for w in wa[i1:i2]:
            lc.a += w.chords
            lc.a_at += [None] * len(w.chords)
        for w in wb[j1:j2]:
            lc.b += w.chords
            lc.b_at += [None] * len(w.chords)
    for line in sorted(per_line):
        d = _chord_difference(f"{label}, line {line + 1}", per_line[line], key)
        if d is not None:
            out.append(d)

    # lines of chords only (an intro, a turnaround): in order
    sig_a = [" ".join(f"{c.root}{c.triad}" for c in cs) for _, cs in bars_a]
    sig_b = [" ".join(f"{c.root}{c.triad}" for c in cs) for _, cs in bars_b]
    bm = SequenceMatcher(None, sig_a, sig_b, autojunk=False)
    for _op, i1, i2, j1, j2 in bm.get_opcodes():
        pairs = list(zip(range(i1, i2), range(j1, j2), strict=False))
        for i, j in pairs:
            lc = _LineChords(a=bars_a[i][1], b=bars_b[j][1])
            d = _chord_difference(f"{label}, line {bars_a[i][0] + 1}", lc, key)
            if d is not None:
                out.append(d)
        for i in range(i1 + len(pairs), i2):
            where = f"{label}, line {bars_a[i][0] + 1}"
            out.append(Difference("chords", where, _names(bars_a[i][1], key), "", "only in first"))
        for j in range(j1 + len(pairs), j2):
            out.append(Difference("chords", label, "", _names(bars_b[j][1], key), "only in second"))


def _section_words(sheet: Sheet, sec: Section) -> list[str]:
    return _norm_words(" ".join(ln.lyrics for ln in _lines(sheet, sec)))


def pair_sections(a: Sheet, b: Sheet) -> list[tuple[int, int]]:
    """Which part of ``b`` is compared with which part of ``a``: parts of the same kind, those with
    the most similar words first (a song sung with other verses, or verses in another order, keeps
    each verse with its own); what is left of a kind pairs in order; parts of a kind the other sheet
    lacks are left out (the form difference names them)."""
    words_a = [_section_words(a, s) for s in a.sections]
    words_b = [_section_words(b, s) for s in b.sections]
    scored = []
    for i, sa in enumerate(a.sections):
        for j, sb in enumerate(b.sections):
            if sa.kind != sb.kind or not words_a[i] or not words_b[j]:
                continue
            ratio = SequenceMatcher(None, words_a[i], words_b[j], autojunk=False).ratio()
            if ratio >= 0.5:
                scored.append((-ratio, abs(i - j), i, j))
    used_a: set[int] = set()
    used_b: set[int] = set()
    pairs = []
    for _, _, i, j in sorted(scored):
        if i not in used_a and j not in used_b:
            pairs.append((i, j))
            used_a.add(i)
            used_b.add(j)
    for kind in dict.fromkeys(s.kind for s in a.sections):
        rest_a = [i for i, s in enumerate(a.sections) if s.kind == kind and i not in used_a]
        rest_b = [j for j, s in enumerate(b.sections) if s.kind == kind and j not in used_b]
        pairs.extend(zip(rest_a, rest_b, strict=False))
    return sorted(pairs)


def _flow_text(sheet: Sheet) -> str:
    return " · ".join(label or kind for kind, label in sheet.flow())


def compare(a: Sheet, b: Sheet) -> Comparison:
    ka, kb = _key_of(a), _key_of(b)
    shift = best_shift(a, b)
    bn = b.transpose(-shift)
    diffs: list[Difference] = []
    if shift:
        diffs.append(
            Difference(
                "key",
                "",
                ka.name if ka else "?",
                kb.name if kb else "?",
                f"second is transposed {shift:+d} semitones",
            )
        )
    if a.capo != b.capo:
        sa = ka.transpose(-a.capo).name if ka else "?"
        sb = kb.transpose(-b.capo).name if kb else "?"
        diffs.append(
            Difference(
                "capo",
                "",
                f"capo {a.capo} ({sa} shapes)",
                f"capo {b.capo} ({sb} shapes)",
                "same sound" if not shift else "",
            )
        )
    kinds_a = [s.kind for s in a.sections]
    kinds_b = [s.kind for s in bn.sections]
    if kinds_a != kinds_b:
        diffs.append(Difference("form", "", _flow_text(a), _flow_text(b)))
    for i, j in pair_sections(a, bn):
        sa, sb = a.sections[i], bn.sections[j]
        if sa.repeat_of is not None and sb.repeat_of is not None:
            continue  # both repeat a part compared already
        _compare_sections(sa.label or sa.kind, _lines(a, sa), _lines(bn, sb), ka, diffs)

    words_a = _norm_words(" ".join(ln.lyrics for s in a.sections for ln in _lines(a, s)))
    words_b = _norm_words(" ".join(ln.lyrics for s in b.sections for ln in _lines(b, s)))
    lyr = (
        SequenceMatcher(None, words_a, words_b, autojunk=False).ratio()
        if words_a or words_b
        else 0.0
    )
    ch = SequenceMatcher(
        None, _signature(a.chords()), _signature(bn.chords()), autojunk=False
    ).ratio()
    return Comparison(
        ka.name if ka else None,
        kb.name if kb else None,
        shift,
        a.capo,
        b.capo,
        round(lyr, 3),
        round(ch, 3),
        diffs,
    )
