"""The rules that clean one sheet on its own (sheets/rules.py): chords written as text get real
chords, tab staves become a tab part, header lines go. All sheets here are made up."""

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


def test_a_remark_after_plain_chords_stays_text() -> None:
    s = cp("{start_of_outro}\nC  G  (to fade)\n{end_of_outro}\n")
    rules.apply(s)
    line = s.sections[0].lines[0]
    assert [c.name() for c in line.chords] == ["C", "G"]
    assert "(to fade)" in line.lyrics


# --- tab blocks ------------------------------------------------------------------------------

STAFF = ["e|---0---3---|", "B|---1---0---|", "G|---0---0---|"]


def test_tab_staves_from_a_plain_page_are_a_tab_part() -> None:
    page = "\n".join(
        ["[Verse 1]", "C         G", "paper boats are sailing", "", "[Riff]", *STAFF, "",
         *STAFF, "", "[Chorus]", "F       C", "sail away"]
    )  # fmt: skip
    s = cp(plaintext.to_chordpro(page))
    done = rules.apply(s)
    assert done.tabs == 1
    assert [(x.kind, x.label) for x in s.sections] == [
        ("verse", "Verse 1"), ("tab", "Riff"), ("chorus", "Chorus"),
    ]  # fmt: skip
    tab = s.sections[1]
    assert [ln.lyrics for ln in tab.lines] == STAFF * 2  # both staves, no comments
    out = chordpro.write(s)
    assert "{start_of_tab: Riff}\n" + STAFF[0] in out and "{comment: e|" not in out
    assert all("e|" not in ln for ln in lyric_lines(cp("\n".join(
        ln for ln in out.split("\n") if not ln.startswith(("e|", "B|", "G|"))
    ))))  # fmt: skip


def test_tab_lines_inside_a_part_are_split_off() -> None:
    text = "\n".join(
        ["{start_of_chorus}", "[C]sail away", *STAFF, "[G]sail back home", "{end_of_chorus}",
         "{chorus}"]
    )  # fmt: skip
    s = cp(text)
    rules.apply(s)
    assert [x.kind for x in s.sections] == ["chorus", "tab", "chorus", "chorus"]
    assert s.sections[1].label == "Tab"
    assert s.sections[3].repeat_of == 0  # {chorus} still repeats the first part
    assert lyric_lines(s)[0] == "sail away"


def test_tab_lines_carry_no_words_into_tidy() -> None:
    s = cp("\n".join(["{start_of_verse}", "[C]paper boats", "{end_of_verse}", "", *STAFF]))
    rules.apply(s)
    assert s.sections[-1].kind == "tab"
    assert tidy._part_words(s.sections[-1]) == []
    tidy.tidy(s, [], "")
    assert s.sections[-1].kind == "tab" and len(s.sections[-1].lines) == 3


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


def test_lyrics_before_the_first_chord_stay() -> None:
    s = cp("by the river we sing\nthe radio tuning in\n\n[G]paper boats\n")
    done = rules.apply(s)
    assert done.removed == []
    assert lyric_lines(s)[:2] == ["by the river we sing", "the radio tuning in"]


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
