"""A source's sheet cleaned with the other sources of the song (sheets/tidy.py) and the page
readers it relies on (plaintext.py): headers and notes go, unnamed parts get names (a riff a source
names, the name another source gives the same words), lyrics written without chords get the chords
of their part. The lyrics are made up."""

from topcoat_hub.sheets import chordpro, plaintext, research, tidy
from topcoat_hub.sheets.chordpro import Sheet, kind_of
from topcoat_hub.sheets.chords import parse_chord
from topcoat_hub.sheets.research import Request

VERSE = [
    "the river bends below the hill",
    "we wait for morning on the sill",
    "a lantern burning in the rain",
    "and every road leads home again",
]
CHORUS = ["sing it out tonight", "hold the lantern light"]


def sheet(text: str) -> Sheet:
    return chordpro.parse(plaintext.to_chordpro(text, "Example Song", "Example Band"))


def kinds(s: Sheet) -> list[tuple[str, str]]:
    return [(sec.kind, sec.label) for sec in s.sections]


def lyric_lines(s: Sheet) -> list[str]:
    return [ln.lyrics.strip() for sec in s.sections for ln in sec.lines if ln.comment is None]


# a source that names its parts and has a riff tab at the bottom
NAMED = f"""[Verse 1]
G         D
{VERSE[0]}
Em        C
{VERSE[1]}

[Pre-Chorus]
Am   C
oh oh oh

[Chorus]
C      G
{CHORUS[0]}
D      G
{CHORUS[1]}

main riff as promised:
Em   Am   Bm
e|---3-0----0---|
B|-------0-3----|
"""

# the source to clean: a header, unnamed parts, the riff as a line of chords
LOOSE = f"""EXAMPLE SONG - Example Band
Tabbed by: someone

[Verse 1]
G         D
{VERSE[0]}
Em        C
{VERSE[1]}

Am   C
oh oh oh

Em  Em  Am  Bm

[Chorus]
C      G
{CHORUS[0]}
D      G
{CHORUS[1]}

Em  Em  Am  Bm

Thanks for looking, rate this tab please
"""


def clean(text: str, *others: str, reference: str = "") -> tuple[Sheet, tidy.Tidied]:
    s = sheet(text)
    rest = [sheet(o) for o in others]
    patterns = [p for o in [s, *rest] for p in tidy.riffs(o)]
    return s, tidy.tidy(s, rest, reference, "Example Band", patterns)


def test_headers_and_notes_go_and_parts_get_their_names() -> None:
    s, done = clean(LOOSE, NAMED, reference="\n".join(VERSE + CHORUS))
    assert kinds(s) == [
        ("verse", "Verse 1"),
        ("pre_chorus", "Pre-Chorus"),
        ("riff", "Riff"),
        ("chorus", "Chorus"),
        ("riff", "Riff"),
    ]
    assert not any("EXAMPLE SONG" in ln or "rate this tab" in ln for ln in lyric_lines(s))
    assert any("Thanks for looking" in r for r in done.removed)
    assert "Riff" in done.named


def test_a_riff_named_in_its_own_source_is_found_across_a_blank_line() -> None:
    riffs = tidy.riffs(sheet(NAMED))
    assert [[c.name() for c in p] for p in riffs] == [["Em", "Am", "Bm"]]


def test_without_other_sources_a_part_of_chords_is_named_by_its_place() -> None:
    head = ["G  D  G", "", "[Verse]", "G     D", VERSE[0], "", "C  G  C", "", "[Chorus]", "C   G"]
    text = "\n".join([*head, CHORUS[0], "", "G  D"])
    s, _ = clean(text)
    assert [k for k, _ in kinds(s)] == ["intro", "verse", "interlude", "chorus", "outro"]


def test_lyrics_without_chords_take_the_chords_of_their_part() -> None:
    text = f"""[Verse 1]
G         D
{VERSE[0]}
Em        C
{VERSE[1]}

{VERSE[2]}
{VERSE[3]}

[Chorus 1]
C      G
{CHORUS[0]}
D      G
{CHORUS[1]}

[Chorus 2]
{CHORUS[1]}
{CHORUS[0]}
"""
    s, done = clean(text)
    verse = s.sections[0]
    # the words after the blank line go on with the verse and take its chords in turn
    assert [ln.chords[0].name() for ln in verse.lines] == ["G", "Em", "G", "Em"]
    chorus2 = next(sec for sec in s.sections if sec.label == "Chorus 2")
    assert [[c.name() for c in ln.chords] for ln in chorus2.lines] == [["C", "G"], ["D", "G"]]
    assert "Chorus 2 \N{LEFTWARDS ARROW} Chorus 1" in done.filled


def test_a_chord_held_over_one_line_is_not_filled() -> None:
    text = f"[Verse]\nG\n{VERSE[0]}\n{VERSE[1]}\nC\n{VERSE[2]}\n"
    s, done = clean(text)
    assert [len(ln.chords) for ln in s.sections[0].lines] == [1, 0, 1]
    assert done.filled == []


def test_page_reader_details() -> None:
    cp = plaintext.to_chordpro(
        "[Slow down]\nEm Em\n\n1 + 2 + 3 + 4 +\n[Intro] C G | Am F\n"
        "C  G  (to fade)\nC     G\n   Hmmm\n"
    )
    s = chordpro.parse(cp)
    assert ("other", "Slow down") in kinds(s)
    assert "1 + 2" not in cp
    assert "[C] [G] [Am] [F]" in cp and "[C] [G] (to fade)" in cp
    assert parse_chord("Hmmm") is None and "[Hmmm]" not in cp and "[H" not in cp
    assert kind_of("Verse 1, 2") == "verse" and kind_of("Verse/Chorus 1") == "verse"
    assert kind_of("Pre-Hook") == "pre_chorus" and kind_of("Main Riff") == "riff"


def test_syllables_are_joined_where_a_page_splits_all_words() -> None:
    split = "\n".join(["G      D", "a-long the ri-ver, un-der the hill-side"] * 4)
    cp = plaintext.to_chordpro(split)
    assert "[G]along [D]the river, under the hillside" in cp
    assert "ahhh-ahhh" in plaintext.to_chordpro("G         D\nahhh-ahhh oh")


def test_the_notes_of_a_tabber_mark_the_version() -> None:
    notes = ["A little different version, it makes it a bit easier for beginners."]
    assert research.note_flags(notes) == ["simplified", "version:arrangement"]
    assert research.note_flags(["This is an arrangement made for an acoustic duo"]) == [
        "version:arrangement"
    ]
    assert research.note_flags(["Tabbed from the record"]) == []


def test_the_report_says_what_was_cleaned() -> None:
    found = research.sources.Found("https://x/1", "x", "", votes=1)
    src = research.Source(found, sheet(f"[Verse]\nG   D\n{VERSE[0]}\n"))
    src.tidied = tidy.Tidied(
        ["a header"], ["Riff", "Riff"], ["Chorus 2 \N{LEFTWARDS ARROW} Chorus 1"]
    )
    out = research.Outcome(Request("Example Song"), src, [src], [None], None, [], [])
    text = research.report(out, "de")
    assert "1 Kopf-/Notizzeilen entfernt" in text and "benannt" in text and ": Riff\n" in text
    assert "Chorus 2 \N{LEFTWARDS ARROW} Chorus 1" in text


def test_a_name_typed_a_little_wrong_is_the_same_artist() -> None:
    assert research._same_name("Red Hot Chili Peppers", "red hot chilly peppers")
    assert not research._same_name("Red Hot Chili Peppers", "The Doobie Brothers")


def test_how_most_sources_set_the_capo_decides_between_equal_versions() -> None:
    text = f"[Verse]\nG     D\n{VERSE[0]}\nEm    C\n{VERSE[1]}\n"

    def src(capo: int) -> research.Source:
        found = research.sources.Found(f"https://x/{capo}", "x", text, votes=100, rating=4.5)
        s = sheet(text)
        s.capo = capo
        return research.Source(found, s)

    capo3, open1, open2 = src(3), src(0), src(0)
    assert research.choose([capo3, open1, open2], Request("Example Song")) is not capo3


def test_a_header_is_found_with_any_spelling_of_the_artist() -> None:
    text = f"EXAMPLE SONG\nThe Band & The Friends\n\n[Verse]\nG     D\n{VERSE[0]}\n"
    s = sheet(text)
    done = tidy.tidy(s, [], "", ["The Band and the Friends", "The Band & The Friends"])
    assert kinds(s) == [("verse", "Verse")] and len(done.removed) == 1
