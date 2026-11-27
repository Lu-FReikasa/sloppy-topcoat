"""The rules that clean one sheet on its own (sheets/rules.py): chords written as text get real
chords, tab staves go, header lines go. All sheets here are made up."""

import pytest

from topcoat_hub.sheets import chordpro, plaintext, rules, store, tidy
from topcoat_hub.sheets.chordpro import Sheet


def cp(text: str) -> Sheet:
    return chordpro.parse(text)


def lyric_lines(s: Sheet) -> list[str]:
    return [ln.lyrics.strip() for sec in s.sections for ln in sec.lines if ln.comment is None]


def names(s: Sheet) -> list[str]:
    return [c.name() for c in s.chords()]


# --- chord lines -----------------------------------------------------------------------------


def test_a_heading_with_plain_chords_names_its_part_and_transposes() -> None:
    s = cp("Intro: C  F  G\n\n{start_of_verse}\n[C]little paper boat\n{end_of_verse}\n")
    done = rules.apply(s)
    assert done.chord_lines == 1
    assert (s.sections[0].kind, s.sections[0].label) == ("intro", "Intro")
    assert [c.name() for c in s.sections[0].lines[0].chords] == ["C", "F", "G"]
    up = s.transpose(2)
    assert [c.name() for c in up.sections[0].lines[0].chords] == ["D", "G", "A"]


def test_plain_chords_in_a_comment_become_chords() -> None:
    s = cp("{start_of_verse}\n{comment: Am  G  | F  x2}\n[Am]paper boats are sailing\n"
           "{end_of_verse}\n")  # fmt: skip
    rules.apply(s)
    first = s.sections[0].lines[0]
    assert first.comment is None
    assert [c.name() for c in first.chords] == ["Am", "G", "F"]
    assert "x2" in first.lyrics and "|" in first.lyrics  # bar line and repeat mark stay text
    assert "{comment" not in chordpro.write(s)


def test_a_labelled_chord_line_inside_a_part_keeps_its_label_as_text() -> None:
    s = cp("{start_of_verse}\n[G]paper boats\nRiff: Em  D\n{end_of_verse}\n")
    rules.apply(s)
    line = s.sections[0].lines[1]
    assert line.lyrics.startswith("Riff:")
    assert [c.name() for c in line.chords] == ["Em", "D"]
    assert s.sections[0].kind == "verse"


def test_a_line_with_a_word_that_is_no_chord_stays_text() -> None:
    s = cp("{start_of_verse}\n[C]start\nA E D G and then some\nAm I dreaming\n{end_of_verse}\n")
    done = rules.apply(s)
    assert done.chord_lines == 0
    assert lyric_lines(s)[1:] == ["A E D G and then some", "Am I dreaming"]


def test_german_plain_chords_are_read_as_german() -> None:
    s = cp("Intro: H  E  B\n\n[E]wir singen\n")  # H is B, B is Bb
    rules.apply(s)
    assert [c.name() for c in s.sections[0].lines[0].chords] == ["B", "E", "A#"]


def test_german_bracket_chords_make_the_plain_ones_german_too() -> None:
    s = store.read("{start_of_verse}\n[H]wir [E]singen\n{comment: B  E}\n{end_of_verse}\n")
    assert names(s) == ["B", "E", "A#", "E"]  # the plain B is a B flat


def test_plain_chords_with_a_capo_are_shapes() -> None:
    s = store.read("{capo: 2}\n{start_of_verse}\n{comment: C  G}\n[C]boats [G]sail\n"
                   "{end_of_verse}\n")  # fmt: skip
    assert names(s) == ["D", "A", "D", "A"]  # sounding, like the chords in brackets


def test_plain_chords_of_a_stored_sheet_already_sound() -> None:
    # the stored form ({x_capo}, the editor's text) is in sounding pitch: nothing is moved
    s = store.read("{x_capo: 2}\n{start_of_verse}\n{comment: D  A}\n[D]boats [A]sail\n"
                   "{end_of_verse}\n")  # fmt: skip
    assert names(s) == ["D", "A", "D", "A"]


def test_a_remark_after_plain_chords_stays_text() -> None:
    s = cp("{start_of_outro}\nC  G  (to fade)\n{end_of_outro}\n")
    rules.apply(s)
    line = s.sections[0].lines[0]
    assert [c.name() for c in line.chords] == ["C", "G"]
    assert "(to fade)" in line.lyrics


# --- tab blocks ------------------------------------------------------------------------------

STAFF = ["e|---0---3---|", "B|---1---0---|", "G|---0---0---|"]


def test_tab_staves_from_a_plain_page_go() -> None:
    page = "\n".join(
        ["[Verse 1]", "C         G", "paper boats are sailing", "", "[Riff]", *STAFF, "",
         *STAFF, "", "[Chorus]", "F       C", "sail away"]
    )  # fmt: skip
    s = cp(plaintext.to_chordpro(page))
    done = rules.apply(s)
    assert done.tabs >= 1
    assert [(x.kind, x.label) for x in s.sections] == [("verse", "Verse 1"), ("chorus", "Chorus")]
    out = chordpro.write(s)
    assert "e|" not in out and "start_of_tab" not in out


def test_tab_lines_inside_a_part_go_and_its_lyrics_stay() -> None:
    text = "\n".join(
        ["{start_of_chorus}", "[C]sail away", *STAFF, "[G]sail back home", "{end_of_chorus}",
         "{start_of_verse}", "[D]a verse", "{end_of_verse}", "{chorus}"]
    )  # fmt: skip
    s = cp(text)
    rules.apply(s)
    assert [x.kind for x in s.sections] == ["chorus", "verse", "chorus"]
    assert lyric_lines(s)[:2] == ["sail away", "sail back home"]
    assert s.sections[2].repeat_of == 0  # {chorus} still repeats the whole chorus
    stored = cp(chordpro.write(s))  # and still does once the sheet is stored and read again
    assert stored.sections[2].repeat_of == 0


def test_a_tab_part_and_its_repeat_go() -> None:
    lines = ["{start_of_verse}", "[C]paper boats", "{end_of_verse}", "",
             "{start_of_tab: Riff}", *STAFF, "{end_of_tab}", "{chorus: Riff}"]  # fmt: skip
    s = cp("\n".join(lines))
    rules.apply(s)
    assert [x.kind for x in s.sections] == ["verse"]
    tidy.tidy(s, [], "")
    assert lyric_lines(s) == ["paper boats"]


# --- header ----------------------------------------------------------------------------------


HEADER = """Title: Paper Boat Song
Artist: The Example Band
by Jane Example
Source: chords.example.com
Tuning: Eb Ab Db Gb Bb Eb
Key: G
Tempo: 96

[Verse 1]
G         D
paper boats are sailing
"""


def test_header_lines_become_facts_or_go() -> None:
    s = cp(plaintext.to_chordpro(HEADER))
    done = rules.apply(s)
    assert s.meta.get("x_tuning") == "Eb Ab Db Gb Bb Eb"
    assert lyric_lines(s) == ["paper boats are sailing"]
    assert "by Jane Example" in done.removed and "Source: chords.example.com" in done.removed


def test_a_chordpro_header_sets_title_artist_and_tuning() -> None:
    s = cp(
        "Title: Paper Boat Song\nArtist: The Example Band\nStandard tuning\n"
        "www.chords.example.com\n\n{start_of_verse}\n[G]paper boats\n{end_of_verse}\n"
    )
    rules.apply(s)
    assert (s.title, s.artist) == ("Paper Boat Song", "The Example Band")
    assert s.meta["x_tuning"] == "Standard"
    assert [x.kind for x in s.sections] == ["verse"]


def test_a_line_with_the_title_and_artist_goes() -> None:
    s = cp("{title: Paper Boat}\n{artist: The Examples}\nPAPER BOAT - THE EXAMPLES\n"
           "[G]paper boat on the water\n")  # fmt: skip
    rules.apply(s)
    assert lyric_lines(s) == ["paper boat on the water"]


@pytest.mark.parametrize(
    ("line", "tuning"),
    [("Drop-D tuning", "Drop-D"), ("Half-step down tuning", "Half-step down"),
     ("1/2 step down tuning", "1/2 step down"), ("DADGAD tuning", "DADGAD")],
)  # fmt: skip
def test_tuning_lines_with_hyphens_and_halves_are_facts(line: str, tuning: str) -> None:
    s = cp(f"{line}\n\n[G]paper boats\n")
    assert rules.apply(s).removed == [line]
    assert s.meta["x_tuning"] == tuning


def test_lyrics_before_the_first_chord_stay() -> None:
    lines = ["by the river we sing", "the radio tuning in", "we keep on tuning",
             "Album of the year"]  # fmt: skip
    s = cp("\n".join(lines) + "\n\n[G]paper boats\n")
    done = rules.apply(s)
    assert done.removed == [] and "x_tuning" not in s.meta
    assert lyric_lines(s)[:4] == lines


def test_header_words_after_the_first_chord_stay() -> None:
    s = cp("[G]paper boats\nTitle: is a word in this song\n")
    rules.apply(s)
    assert "Title: is a word in this song" in lyric_lines(s)


# --- together --------------------------------------------------------------------------------


def test_the_rules_change_nothing_the_second_time() -> None:
    page = HEADER + "\nRiff: Em  D\n" + "\n".join(STAFF) + "\n\nOutro: C G\n"
    s = cp(plaintext.to_chordpro(page))
    rules.apply(s)
    first = chordpro.write(s)
    again = cp(first)
    done = rules.apply(again)
    assert chordpro.write(again) == first
    assert (done.chord_lines, done.tabs, done.removed) == (0, 0, [])


def test_several_headings_with_chords_start_their_own_parts() -> None:
    s = cp("Intro: C F\nRiff: Am G\nOutro: F C\n\n[C]boats\n")
    rules.apply(s)
    assert [x.kind for x in s.sections][:3] == ["intro", "riff", "outro"]


def test_an_import_applies_the_rules() -> None:
    s = store.read("Title: Paper Boat\nIntro: C F G\n\n{start_of_verse}\n[C]boats\n{end_of_verse}")
    assert s.title == "Paper Boat"
    assert s.sections[0].kind == "intro" and len(s.chords()) == 4
    assert names(s.transpose(-2))[:3] == ["A#", "D#", "F"]


# --- patterns found on the stored sheets (2026-11-26) ----------------------------------------


def test_a_star_as_accent_does_not_hide_a_line_of_chords() -> None:
    s = cp("{start_of_verse}\n *    A   D\npaper boats are sailing\n{end_of_verse}\n")
    rules.apply(s)
    assert [c.name() for c in s.sections[0].lines[0].chords] == ["A", "D"]
    assert [c.name() for c in s.transpose(2).sections[0].lines[0].chords] == ["B", "E"]


def test_chords_over_chords_become_one_line_of_the_written_chords() -> None:
    s = cp("{start_of_verse}\n[A]A        [D]D     [D]F   *  A\n[A]little [D]paper boat\n"
           "{end_of_verse}\n")  # fmt: skip
    done = rules.apply(s)
    assert done.chord_lines == 1
    assert [c.name() for c in s.sections[0].lines[0].chords] == ["A", "D", "F", "A"]
    assert [c.name() for c in s.sections[0].lines[1].chords] == ["A", "D"]  # lyrics untouched


def test_a_lyric_line_with_one_chord_word_is_not_a_line_of_chords() -> None:
    s = cp("{start_of_verse}\n[A]A little [D]boat\n{end_of_verse}\n")
    rules.apply(s)
    assert [c.name() for c in s.sections[0].lines[0].chords] == ["A", "D"]
    assert lyric_lines(s) == ["A little boat"]


def test_staves_side_by_side_with_a_fret_taken_for_a_chord_go_with_their_heading() -> None:
    s = cp("{start_of_part: Other}\n *  Riff 1:        ** Riff 2:\n"
           "e|-------[B]0-|         e|-------2-|\n"
           "B|-------2-|         B|-------3-|\nG|-------2-|         G|-------2-|\n{end_of_part}\n"
           "\n{start_of_verse}\n[A]paper boat\n{end_of_verse}\n")  # fmt: skip
    done = rules.apply(s)
    assert done.tabs == 1
    assert [x.kind for x in s.sections] == ["verse"]
    assert "e|" not in chordpro.write(s)


def test_a_tuning_sentence_in_the_header_goes_to_the_facts() -> None:
    s = cp("{start_of_part: Other}\nTune your guitar to 435 Hz to match the record.\n"
           "{end_of_part}\n\n{start_of_verse}\n[C]paper boat\n{end_of_verse}\n")  # fmt: skip
    done = rules.apply(s)
    assert len(done.removed) == 1
    assert s.meta.get("x_tuning") == "435 Hz"
    assert lyric_lines(s) == ["paper boat"]


def test_a_minor_major_ninth_is_a_chord_but_a_hum_is_not() -> None:
    from topcoat_hub.sheets.chords import parse_chord

    assert parse_chord("Ammaj9") is not None
    assert parse_chord("Hmmm", german=True) is None


def test_the_names_of_riffs_without_their_tab_go_from_the_header() -> None:
    s = cp("{start_of_part: Other}\n *  Riff 1:        ** Riff 2:\n{end_of_part}\n"
           "\n{start_of_verse}\n[A]paper boat\n{end_of_verse}\n")  # fmt: skip
    done = rules.apply(s)
    assert len(done.removed) == 1
    assert [x.kind for x in s.sections] == ["verse"]


def test_a_lyric_with_the_word_riff_stays() -> None:
    s = cp("{start_of_part: Other}\nplay me that riff again\n{end_of_part}\n"
           "\n{start_of_verse}\n[A]paper boat\n{end_of_verse}\n")  # fmt: skip
    rules.apply(s)
    assert lyric_lines(s)[0] == "play me that riff again"


def test_accent_stars_on_chord_lines_go_and_a_star_in_lyrics_stays() -> None:
    s = cp("{start_of_verse}\n *    A   D\n**\npaper *boats* are sailing\n{end_of_verse}\n")
    rules.apply(s)
    lines = s.sections[0].lines
    assert len(lines) == 2  # the lone "**" line went
    assert "*" not in lines[0].lyrics and [c.name() for c in lines[0].chords] == ["A", "D"]
    assert lines[1].lyrics == "paper *boats* are sailing"
