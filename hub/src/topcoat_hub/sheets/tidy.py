"""A source's sheet made clean with the help of the other sources of the same song.

A chord page is written for people: it starts with a header ("THE POWER OF LOVE / Huey Lewis &
the News / Words & Music by …"), has notes of the tabber in it, leaves most parts unnamed after a
blank line, and writes the chords of a verse once ("the other verses the same"). Read as it is,
the sheet shows the header as lyrics, many parts as "Other" and long passages without chords.
What the other sources of the song say settles most of it:

``remove``  an unnamed part without chords whose words no other source and not the reference
            lyrics hold is no part of the song (a header, a note, a credit); a line of such words
            inside a part is dropped the same way
``name``    an unnamed part gets the name another source gives the same words (the same chords for
            a part without words); a part of chords only that plays the riff a source names (a
            "main riff" tab with its chords) is the riff; what is still unnamed follows the part
            before it (the words after a blank line) or, without words, its place (intro,
            instrumental, outro)
``fill``    lyrics without chords in a part whose kind has chords elsewhere (the second verse, a
            chorus written once) get the chords of that part, line by line, on the same place of
            the line

The rules that need no other source (chords written as text, tab blocks, header lines) run first
(``rules.py``). The work is on the sheet in place; :class:`Tidied` says what was done, for the
report.
"""

import re
from dataclasses import dataclass, field
from difflib import SequenceMatcher

from topcoat_hub.sheets import rules
from topcoat_hub.sheets.chordpro import Line, Section, Segment, Sheet
from topcoat_hub.sheets.chords import Chord

SUPPORT = 0.5  # share of a part's words other sources must hold for it to be part of the song
LINE_SUPPORT = 0.3  # the same for a single line (lyrics of one version only are still lyrics)
MIN_VOCABULARY = 40  # fewer known words than this: too little to tell lyrics from notes
CONTAINED = 0.7  # share of a part's words in order inside another part to take its name
_CREDIT = re.compile(
    r"@|https?://|www\.|\b(?:chorded|tabbed|transcribed|arranged|submitted|corrected)\b|"
    r"\bwords\s*(?:&|and)\s*music\b|\b(?:lyrics|music|chords|tab)\s+by\b|\bby:",
    re.I,
)
_RIFF = re.compile(r"\briff\b", re.I)
# a line that says how to play, not what to sing ("Repeat Verse 1", "(play twice)")
_INSTRUCTION = re.compile(r"^\W*(?:repeat|wiederhol|play\b|spiel|x\s?\d+\b|\d+\s?x\b)", re.I)
_SONG_PARTS = ("verse", "chorus", "pre_chorus", "bridge")


@dataclass
class Tidied:
    removed: list[str] = field(default_factory=list)  # the text of what was dropped
    named: list[str] = field(default_factory=list)  # "Riff", "Pre-Hook (as another source)"
    filled: list[str] = field(default_factory=list)  # "Chorus 2 ← Chorus 1"


def _words(text: str) -> list[str]:
    text = re.sub(
        r"\([^)]*\)",
        " ",
        text.lower().replace("'", "").replace("\N{RIGHT SINGLE QUOTATION MARK}", ""),
    )
    return [w for w in re.findall(r"[a-z0-9äöüß]+", text) if not re.fullmatch(r"x\d+", w)]


def _lyric(ln: Line) -> bool:
    return ln.comment is None and bool(_words(ln.lyrics))


def _chorded(ln: Line) -> bool:
    return any(g.chord is not None for g in ln.segments)


def _part_words(sec: Section) -> list[str]:
    if sec.kind == "tab":
        return []  # a tab block's lines are frets, no words
    return [w for ln in sec.lines if _lyric(ln) for w in _words(ln.lyrics)]


def _chord_only(sec: Section) -> bool:
    return any(_chorded(ln) for ln in sec.lines) and not any(_lyric(ln) for ln in sec.lines)


def _unnamed(sec: Section) -> bool:
    return (
        sec.kind == "other"
        and sec.repeat_of is None
        and (not sec.label or re.fullmatch(r"other(\s*\d+)?", sec.label.strip(), re.I) is not None)
    )


def _sequence(chords: list[Chord]) -> list[str]:
    """The chords as played, a chord held over bars counted once (``Em Em Am Bm`` → Em Am Bm)."""
    out: list[str] = []
    for c in chords:
        name = c.name()
        if not out or out[-1] != name:
            out.append(name)
    return out


def _contained(part: list[str], other: list[str]) -> float:
    """The share of ``part``'s words found in order in ``other``."""
    if not part or not other:
        return 0.0
    sm = SequenceMatcher(None, part, other, autojunk=False)
    return sum(b.size for b in sm.get_matching_blocks()) / len(part)


def _plain_label(label: str) -> str:
    return re.sub(r"[\s:]*\d+\s*:?$", "", label).strip() or label


def riffs(sheet: Sheet) -> list[list[Chord]]:
    """The chord sequences a sheet names as a riff: a part called riff, or the chord lines that
    follow a line speaking of one ("*main riff as promised:" over its tab)."""
    out = []
    for sec in sheet.sections:
        if sec.kind == "riff":
            chords = [c for ln in sec.lines for c in ln.chords]
            if len(_sequence(chords)) >= 2:
                out.append(chords)
            continue
    # the lines in order; the chords after a mention may stand after a blank line (a part of
    # their own) but not under the next named part
    flat = [
        (k, ln, j == 0 and not _unnamed(sec))
        for k, sec in enumerate(sheet.sections)
        if sec.kind != "riff" and sec.repeat_of is None
        for j, ln in enumerate(sec.lines)
    ]
    for i, (_, ln, _) in enumerate(flat):
        said = ln.comment if ln.comment is not None else ln.lyrics
        if not _RIFF.search(said or ""):
            continue
        chords: list[Chord] = []
        for _, nxt, starts_named in flat[i + 1 :]:
            if starts_named or (_lyric(nxt) and not _RIFF.search(nxt.lyrics)):
                break
            if nxt.comment is None:  # comments are the tab itself
                chords.extend(nxt.chords)
        if len(_sequence(chords)) >= 2:
            out.append(chords)
    return out


def _is_riff(seq: list[str], pattern: list[str]) -> bool:
    n = len(pattern)
    return len(seq) >= n and len(seq) % n == 0 and seq == pattern * (len(seq) // n)


def tidy(
    sheet: Sheet,
    others: list[Sheet],
    reference: str = "",
    artist: str | list[str] = "",
    riff_patterns: list[list[Chord]] | None = None,
) -> Tidied:
    """Clean ``sheet`` in place with what ``others`` (sheets of the same song, in the same pitch)
    and the reference lyrics hold. ``riff_patterns`` are the riffs the sources name (all of them,
    this sheet's own included: read them before any sheet is tidied)."""
    done = Tidied()
    done.removed.extend(rules.apply(sheet).removed)  # a no-op on a sheet read by research
    vocabulary = {w for o in others for s in o.sections for w in _part_words(s)}
    vocabulary |= set(_words(reference))
    _remove(sheet, vocabulary, artist, done)
    _name(sheet, others, [_sequence(p) for p in riff_patterns or []], done)
    _fill(sheet, done)
    return done


# --- remove ----------------------------------------------------------------------------------


def _support(words: list[str], vocabulary: set[str]) -> float:
    return sum(1 for w in words if w in vocabulary) / len(words) if words else 0.0


def _name_words(name: str) -> list[str]:
    return [w for w in _words(name) if w not in ("and", "the", "und")]


def _is_header(sec: Section, artist: str | list[str]) -> bool:
    """A line with the artist's name and little else ("LONG TRAIN RUNNING - THE DOOBIE BROTHERS")
    or a credit; ``artist`` may be several spellings ("Huey Lewis and the News", "… & The News")."""
    names = [n for n in ([artist] if isinstance(artist, str) else artist) if _name_words(n)]
    for ln in sec.lines:
        said = ln.comment if ln.comment is not None else ln.lyrics
        if _CREDIT.search(said or ""):
            return True
        w = _name_words(said or "")
        for name in map(_name_words, names):
            if len(name) <= len(w) <= len(name) + 8 and _contained(name, w) == 1.0:
                return True
    return False


def _remove(sheet: Sheet, vocabulary: set[str], artist: str | list[str], done: Tidied) -> None:
    known = len(vocabulary) >= MIN_VOCABULARY
    for sec in sheet.sections:
        for k, ln in enumerate(sec.lines):
            if ln.comment is None and not _chorded(ln) and _INSTRUCTION.match(ln.lyrics):
                sec.lines[k] = Line(comment=ln.lyrics.strip())  # "Repeat Verse 1": how to play
    chorded = [i for i, s in enumerate(sheet.sections) if any(_chorded(ln) for ln in s.lines)]
    first, last = (chorded[0], chorded[-1]) if chorded else (len(sheet.sections), -1)
    keep: list[Section] = []
    for i, sec in enumerate(sheet.sections):
        has_chords = any(_chorded(ln) for ln in sec.lines)
        if _unnamed(sec) and not has_chords:
            words = _part_words(sec)
            outside = i < first or i > last
            if (
                _is_header(sec, artist)
                # inside the song only with enough known words; before or after it, words no
                # other source holds are a note ("thanks for looking")
                or (
                    (known or (outside and vocabulary))
                    and words
                    and _support(words, vocabulary) < SUPPORT
                )
                or (not words and outside)  # a note before or after the song
            ):
                done.removed.append(_text(sec))
                continue
            if not words and keep:
                keep[-1].lines.extend(sec.lines)  # a note inside the song stays with its part
                continue
        if known and sec.kind != "tab":
            lines = []
            for ln in sec.lines:
                words = _words(ln.lyrics) if ln.comment is None else []
                note = (
                    len(words) >= 3
                    and not _chorded(ln)
                    and _support(words, vocabulary) < LINE_SUPPORT
                ) or (ln.comment is None and not _chorded(ln) and _CREDIT.search(ln.lyrics))
                if note:
                    done.removed.append(ln.lyrics.strip())
                else:
                    lines.append(ln)
            sec.lines = lines
            if not sec.lines and sec.repeat_of is None:
                continue
        keep.append(sec)
    _reindex(sheet, keep)


def _reindex(sheet: Sheet, keep: list[Section]) -> None:
    """``repeat_of`` points at an index of the old list; it follows its part into the new one."""
    old = {id(s): i for i, s in enumerate(sheet.sections)}
    new_of_old = {old[id(s)]: j for j, s in enumerate(keep)}
    for s in keep:
        if s.repeat_of is not None:
            s.repeat_of = new_of_old.get(s.repeat_of)
    sheet.sections = [s for s in keep if s.repeat_of is not None or s.lines]


def _text(sec: Section) -> str:
    return " / ".join(
        (ln.comment if ln.comment is not None else ln.lyrics).strip()
        for ln in sec.lines
        if (ln.comment if ln.comment is not None else ln.lyrics).strip()
    )


# --- name ------------------------------------------------------------------------------------


def _name(sheet: Sheet, others: list[Sheet], patterns: list[list[str]], done: Tidied) -> None:
    n = max(len(sheet.sections), 1)
    previous: tuple[int, int] | None = None  # the part of another source the last part matched
    named: set[int] = set()
    for i, sec in enumerate(sheet.sections):
        if sec.repeat_of is not None:
            continue
        if _chord_only(sec):
            seq = _sequence([c for ln in sec.lines for c in ln.chords])
            if any(_is_riff(seq, p) for p in patterns):
                if _unnamed(sec):
                    sec.kind, sec.label = "riff", "Riff"
                    done.named.append("Riff")
                elif (
                    sec.kind in ("interlude", "intro", "outro") and "riff" not in sec.label.lower()
                ):
                    sec.label = f"{sec.label} (Riff)"
                    done.named.append(sec.label)
                previous = None
                continue
        if not _unnamed(sec):
            previous = None
            continue
        match = _named_elsewhere(sec, i / n, others, previous)
        previous = match[2] if match else None
        found = (match[0], match[1]) if match else _named_here(sheet, i) or _by_place(sheet, i)
        if found is not None:
            kind, label = found
            # the sheet's own name for a kind it already has ("Chorus", not another's "Hook")
            own = next(
                (
                    s.label
                    for s in sheet.sections
                    if s.kind == kind and s is not sec and not _unnamed(s)
                ),
                None,
            )
            sec.kind, sec.label = kind, _plain_label(own) if own and kind != "verse" else label
            done.named.append(sec.label)
            named.add(id(sec))
    # a part named here that goes on with the part before it under the same name is that part
    # (the words after a blank line): one part in the flow, not two
    keep: list[Section] = []
    for sec in sheet.sections:
        prev = keep[-1] if keep else None
        if (
            prev is not None
            and id(sec) in named
            and prev.repeat_of is None
            and (prev.kind, prev.label) == (sec.kind, sec.label)
        ):
            prev.lines.extend(sec.lines)
            continue
        keep.append(sec)
    _reindex(sheet, keep)


def _named_elsewhere(
    sec: Section, place: float, others: list[Sheet], previous: tuple[int, int] | None
) -> tuple[str, str, tuple[int, int]] | None:
    """The kind and name another source gives the same words (the same chords for a part without
    words, at about the same place of the song). Of equal matches the one that goes on from the
    part the previous part matched wins: words a source names as one part stay one part."""
    words = _part_words(sec)
    best: tuple[tuple[float, ...], str, str, tuple[int, int]] | None = None
    if words:
        for k, o in enumerate(others):
            for j, s in enumerate(o.sections):
                if s.kind == "other" or s.repeat_of is not None:
                    continue
                theirs = _part_words(s)
                share = _contained(words, theirs)
                if share < CONTAINED:
                    continue
                goes_on = previous is not None and previous[0] == k and j - previous[1] in (0, 1)
                ratio = SequenceMatcher(None, words, theirs, autojunk=False).ratio()
                score = (round(share, 2), float(goes_on), ratio)
                if best is None or score > best[0]:
                    best = (score, s.kind, _plain_label(s.label), (k, j))
    elif _chord_only(sec):
        seq = _sequence([c for ln in sec.lines for c in ln.chords])
        for k, o in enumerate(others):
            m = max(len(o.sections), 1)
            for j, s in enumerate(o.sections):
                if s.repeat_of is not None or not _chord_only(s) or _unnamed(s):
                    continue
                if _sequence([c for ln in s.lines for c in ln.chords]) != seq:
                    continue
                distance = abs(j / m - place)
                if distance <= (0.2 if len(seq) >= 2 else 0.1) and (
                    best is None or (-distance,) > best[0]
                ):
                    best = ((-distance,), s.kind, _plain_label(s.label), (k, j))
    return (best[1], best[2], best[3]) if best else None


def _named_here(sheet: Sheet, i: int) -> tuple[str, str] | None:
    words = _part_words(sheet.sections[i])
    if not words:
        return None
    best: tuple[float, str, str] | None = None
    for j, s in enumerate(sheet.sections):
        if j == i or s.kind == "other" or s.repeat_of is not None:
            continue
        share = _contained(words, _part_words(s))
        if share >= CONTAINED and (best is None or share > best[0]):
            best = (share, s.kind, s.label)
    if best is not None:
        return best[1], best[2]
    before = sheet.sections[i - 1] if i > 0 else None
    if before is not None and before.kind in _SONG_PARTS and _part_words(before):
        return before.kind, before.label  # the words after a blank line go on with the part
    return None


def _by_place(sheet: Sheet, i: int) -> tuple[str, str] | None:
    if not _chord_only(sheet.sections[i]):
        return None
    sung_before = any(_part_words(s) for s in sheet.sections[:i])
    sung_after = any(_part_words(s) for s in sheet.sections[i + 1 :])
    if not sung_before:
        return "intro", "Intro"
    if not sung_after:
        return "outro", "Outro"
    return "interlude", "Instrumental"


# --- fill ------------------------------------------------------------------------------------


def _lyric_lines(sec: Section) -> list[Line]:
    return [ln for ln in sec.lines if _lyric(ln)]


def _template(sheet: Sheet, sec: Section) -> Section | None:
    """The part of the same kind that shows its chords best."""
    best: tuple[int, Section] | None = None
    for s in sheet.sections:
        if s is sec or s.kind != sec.kind or s.repeat_of is not None:
            continue
        n = sum(1 for ln in _lyric_lines(s) if _chorded(ln))
        if n >= 2 and (best is None or n > best[0]):
            best = (n, s)
    return best[1] if best else None


def _fill(sheet: Sheet, done: Tidied) -> None:
    for sec in sheet.sections:
        if sec.repeat_of is not None or sec.kind in ("other", "tab"):
            continue
        lines = _lyric_lines(sec)
        bare = [not _chorded(ln) for ln in lines]
        if not any(bare):
            continue
        whole = all(bare)
        wanted = [
            b and (whole or (k > 0 and bare[k - 1]) or (k + 1 < len(bare) and bare[k + 1]))
            for k, b in enumerate(bare)
        ]
        if not any(wanted):
            continue
        first_bare = next(k for k, w in enumerate(wanted) if w)
        own = [ln for ln in lines[:first_bare] if _chorded(ln)]
        if len(own) >= 2:
            # the part's first lines show its chords, the rest goes on without them: the
            # chords of its own lines, in turn ("Tougher than diamonds" after the first verse)
            tmpl, models = sec, own
        else:
            found = _template(sheet, sec)
            if found is None:
                continue
            tmpl, models = found, _lyric_lines(found)
        count = 0
        for k, ln in enumerate(lines):
            model = models[(k - first_bare if tmpl is sec else k) % len(models)]
            if wanted[k] and _chorded(model):
                ln.segments = _placed(model, ln.lyrics)
                count += 1
        if count:
            done.filled.append(f"{sec.label} \N{LEFTWARDS ARROW} {tmpl.label}")


def _placed(model: Line, text: str) -> list[Segment]:
    """``text`` with the chords of ``model`` on the same places of the line (by share of its
    length, moved to the start of the nearest word)."""
    model_text = "".join(g.text for g in model.segments)
    length = max(len(model_text.rstrip()), 1)
    starts = [m.start() for m in re.finditer(r"\S+", text)] or [0]
    marks: list[tuple[int, Chord]] = []
    pos = 0
    for g in model.segments:
        if g.chord is not None:
            if pos >= length:
                at = len(text)  # a chord after the last word stays after it
            else:
                target = pos / length * len(text.rstrip())
                at = min(starts, key=lambda s: (abs(s - target), s))
            marks.append((at, g.chord))
        pos += len(g.text)
    marks.sort(key=lambda m: m[0])
    segments: list[Segment] = []
    if not marks or marks[0][0] > 0:
        segments.append(Segment(text[: marks[0][0]] if marks else text))
    for k, (at, chord) in enumerate(marks):
        end = marks[k + 1][0] if k + 1 < len(marks) else len(text)
        segments.append(Segment(text[at:end], chord))
    return segments
