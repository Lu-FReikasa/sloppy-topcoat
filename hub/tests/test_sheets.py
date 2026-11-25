"""Sheets: chords, ChordPro, plain chord pages, keys and the comparison of two sheets."""

import pytest

from topcoat_hub.sheets import chordpro, compare, keys, plaintext
from topcoat_hub.sheets.chords import Chord, Key, note_pc, parse_chord, uses_german_names

# --- chords ---------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("name", "root", "suffix", "bass", "triad"),
    [
        ("C", 0, "", None, "maj"),
        ("Am", 9, "m", None, "min"),
        ("F#m7", 6, "m7", None, "min"),
        ("Bbmaj7", 10, "maj7", None, "maj"),
        ("CMaj7", 0, "maj7", None, "maj"),
        ("CM7", 0, "maj7", None, "maj"),
        ("Amin", 9, "m", None, "min"),
        ("D/F#", 2, "", 6, "maj"),
        ("Dsus4", 2, "sus4", None, "sus4"),
        ("Esus2", 4, "sus2", None, "sus2"),
        ("Bdim", 11, "dim", None, "dim"),
        ("Bm7b5", 11, "m7b5", None, "dim"),
        ("G5", 7, "5", None, "5"),
        ("Caug", 0, "aug", None, "aug"),
        ("Cadd9", 0, "add9", None, "maj"),
        ("C7\N{MINUS SIGN}9", 0, "7-9", None, "maj"),
    ],
)
def test_parse_chord(name, root, suffix, bass, triad):
    c = parse_chord(name)
    assert c == Chord(root, suffix, bass)
    assert c.triad == triad


@pytest.mark.parametrize("text", ["Chorus", "Am I", "N.C.", "x2", "", "Hello", "Ending"])
def test_words_are_no_chords(text):
    assert parse_chord(text) is None


def test_german_names():
    assert note_pc("H", german=True) == 11
    assert note_pc("B", german=True) == 10
    assert note_pc("B") == 11
    assert uses_german_names(["G", "H7", "Em"])
    assert not uses_german_names(["G", "Bm", "Em"])


def test_spelling_follows_the_key():
    a_sharp = Chord(10)
    assert a_sharp.in_key(Key(5)) == "Bb"  # F major
    assert a_sharp.in_key(Key(11)) == "A#"  # B major
    assert Chord(3, "m").in_key(Key(1, minor=True)) == "D#m"  # C#m: relative E major, sharps
    assert Chord(3).in_key(Key(0)) == "Eb"  # C: pop charts write Eb, Bb, Ab


def test_transpose_keeps_quality_and_bass():
    assert parse_chord("D/F#").transpose(2).name() == "E/G#"
    assert parse_chord("Am7").transpose(-2).name(flats=False) == "Gm7"


@pytest.mark.parametrize(
    ("text", "key"),
    [
        ("A", Key(9)),
        ("F#m", Key(6, True)),
        ("Bb minor", Key(10, True)),
        ("E-Dur", Key(4)),
        ("c-moll", Key(0, True)),
        ("nonsense", None),
    ],
)
def test_key_parse(text, key):
    assert Key.parse(text) == key


# --- keys -----------------------------------------------------------------------------------------


def _chords(names: str) -> list[Chord]:
    return [parse_chord(n) for n in names.split()]


def test_estimate_major():
    key, fit = keys.estimate(_chords("G D Em C G D C G"))
    assert key == Key(7)
    assert fit == 1.0


def test_estimate_minor_by_its_first_and_last_chord():
    key, _ = keys.estimate(_chords("Am F C G Am F E Am"))
    assert key == Key(9, minor=True)


def test_degree():
    k = Key(7)
    assert [keys.degree(c, k) for c in _chords("G Em7 C D/F# F")] == [
        "I",
        "vi7",
        "IV",
        "V/VII",
        "bVII",
    ]


# --- ChordPro -------------------------------------------------------------------------------------

SONG = """\
{title: Example Song}
{artist: Example Band}
{key: A}
{x_capo: 2}

{start_of_verse: Verse 1}
[A]Walking down the [E]road
[F#m]Nothing on my [D]mind
{end_of_verse}

{start_of_chorus}
[D]Sing it [E]loud
{end_of_chorus}

{start_of_verse: Verse 2}
[A]Second verse [E]here
{end_of_verse}

{chorus}
"""


def test_parse_chordpro():
    s = chordpro.parse(SONG)
    assert (s.title, s.artist, s.key, s.capo) == ("Example Song", "Example Band", Key(9), 2)
    assert s.flow() == [
        ("verse", "Verse 1"),
        ("chorus", "Chorus"),
        ("verse", "Verse 2"),
        ("chorus", "Chorus"),
    ]
    first = s.sections[0].lines[0]
    assert first.lyrics == "Walking down the road"
    assert [c.name() for c in first.chords] == ["A", "E"]
    assert s.sections[3].repeat_of == 1


def test_capo_view_writes_shapes_and_reads_back_the_same_sound():
    s = chordpro.parse(SONG)
    capo_text = chordpro.write(s, view="capo")
    assert "{capo: 2}" in capo_text
    assert "[G]Walking down the [D]road" in capo_text
    assert "[Em]Nothing" in capo_text
    back = chordpro.parse(capo_text)
    assert back.key == Key(9)
    assert [c.name() for c in back.chords()] == [c.name() for c in s.chords()]


def test_stored_form_round_trips():
    s = chordpro.parse(SONG)
    text = chordpro.write(s)
    assert "{x_capo: 2}" in text
    assert chordpro.write(chordpro.parse(text)) == text


def test_capo_key_naming_the_shapes_is_understood():
    # {key: G} with G shapes under capo 2: the key named the shapes, it sounds in A
    s = chordpro.parse("{key: G}\n{capo: 2}\n[G]one [D]two [Em]three [C]four [G]end\n")
    assert s.key == Key(9)
    assert s.chords()[0].name() == "A"


def test_comment_headings_make_parts():
    s = chordpro.parse(
        "{c: Intro}\n[G] [D]\n\n{c: Strophe 1}\n[G]Hallo [D]Welt\n\n{c: Refrain}\n[C]la\n"
    )
    assert [k for k, _ in s.flow()] == ["intro", "verse", "chorus"]


def test_annotations_and_no_chord_are_kept_raw():
    ln = chordpro.parse_line("[N.C.]Stop [G]go")
    assert ln.segments[0].raw == "N.C." and ln.segments[0].chord is None
    assert ln.lyrics == "Stop go"


def test_transpose_moves_key_and_spells_by_it():
    s = chordpro.parse(SONG).transpose(1)  # A -> Bb
    assert s.key.name == "Bb"
    assert "[Bb]Walking" in chordpro.write(s)


def test_german_source_is_read_as_international():
    s = chordpro.parse("[H]eins [B]zwei [E]drei\n")
    assert [c.name() for c in s.chords()] == ["B", "A#", "E"]


# --- plain chord pages ----------------------------------------------------------------------------

PAGE = """\
Capo 2
Key: A

[Verse 1]
G            D
Walking down the road
Em          C
Nothing on my mind

[Chorus]
C      D
Sing it loud

Intro: G D Em C
"""


def test_plain_page_keeps_chord_columns():
    text = plaintext.to_chordpro(PAGE, title="Example Song")
    assert "{capo: 2}" in text
    assert "[G]Walking down [D]the road" in text
    assert "[C]Sing it [D]loud" in text  # over the gap: the next word
    s = chordpro.parse(text)
    assert s.key == Key(9) and s.capo == 2
    assert [c.name() for c in s.sections[0].lines[0].chords] == ["A", "E"]  # sounding
    assert [k for k, _ in s.flow()] == ["verse", "chorus", "intro"]


def test_chord_on_word_by_column():
    text = plaintext.to_chordpro("G     C\nHello world\n")
    assert text.strip() == "[G]Hello [C]world"


def test_lyric_line_with_a_chord_word_stays_lyrics():
    text = plaintext.to_chordpro("Am I the one\n")
    assert text.strip() == "Am I the one"


def test_tab_lines_are_comments():
    text = plaintext.to_chordpro("e|---0---3---|\nB|---1---0---|\n")
    assert text.count("{comment:") == 2


# --- comparison -----------------------------------------------------------------------------------


def test_transposed_copy_has_only_a_key_difference():
    a = chordpro.parse(SONG)
    b = a.transpose(-2)
    c = compare.compare(a, b)
    assert c.shift == -2
    assert [d.kind for d in c.differences] == ["key"]
    assert c.chords_similarity == 1.0 and c.lyrics_similarity == 1.0


def test_capo_version_sounds_the_same():
    a = chordpro.parse(SONG)
    b = chordpro.parse(chordpro.write(a, view="capo").replace("{capo: 2}", "{capo: 2}"))
    c = compare.compare(a, b)
    assert c.shift == 0
    assert c.differences == []


def test_capo_difference_is_named():
    a = chordpro.parse(SONG)
    b = chordpro.parse(chordpro.write(a).replace("{x_capo: 2}", "{x_capo: 0}"))
    c = compare.compare(a, b)
    assert [d.kind for d in c.differences] == ["capo"]
    assert c.differences[0].note == "same sound"


def test_exact_differences():
    a = chordpro.parse(SONG)
    b_text = (
        chordpro.write(a)
        .replace("[F#m]Nothing on my [D]mind", "[F#m]Nothing in my [Dmaj7]mind")
        .replace("[D]Sing it [E]loud", "[Bm]Sing it [E]loud")
        .replace("[A]Second verse [E]here", "[A]Second verse [E]here\n[D]an extra line")
    )
    c = compare.compare(a, chordpro.parse(b_text))
    got = {(d.kind, d.where) for d in c.differences}
    assert ("lyrics", "Verse 1, line 2") in got
    assert ("chord_detail", "Verse 1, line 2") in got
    assert ("chords", "Chorus, line 1") in got
    assert any(d.kind == "lyrics" and d.note == "only in second" for d in c.differences)
    detail = next(d for d in c.differences if d.kind == "chord_detail")
    assert (detail.a, detail.b, detail.note) == ("F#m D", "F#m Dmaj7", "second adds detail")


def test_form_difference():
    a = chordpro.parse(SONG)
    b = chordpro.parse(SONG.replace("{chorus}\n", "{chorus}\n\n{chorus}\n"))
    c = compare.compare(a, b)
    assert [d.kind for d in c.differences] == ["form"]


def test_chord_position_difference():
    a = chordpro.parse("[G]Hello [C]world out there\n")
    b = chordpro.parse("[G]Hello world out [C]there\n")
    c = compare.compare(a, b)
    assert [d.kind for d in c.differences] == ["chord_position"]


def test_different_songs_score_low():
    a = chordpro.parse(SONG)
    b = chordpro.parse("[Cm]Something [Ab]else [Bb]entirely [Fm]different [G7]now\n")
    c = compare.compare(a, b)
    assert c.lyrics_similarity < 0.2
    assert c.to_dict()["counts"]


def test_a_line_broken_elsewhere_is_no_difference():
    a = chordpro.parse(
        "[F#m]I don't believe that [A]anybody [E]feels the way I [B]do\nAbout you now\n"
    )
    b = chordpro.parse(
        "[F#m]I don't believe that [A]anybody\n[E]Feels the way I [B]do about you now\n"
    )
    assert compare.compare(a, b).differences == []


def test_a_chord_in_the_middle_of_a_word_belongs_to_the_word():
    a = chordpro.parse("Rea[E]lised what you gotta [B]do\n")
    b = chordpro.parse("[E]Realised what you gotta [B]do\n")
    assert compare.compare(a, b).differences == []


def test_lyrics_and_chords_on_changed_words_are_both_named():
    a = chordpro.parse("[G]Back beat, the [C]word is on the street\n")
    b = chordpro.parse("[G]Backbeat the [Cmaj7]word is on the street\n")
    got = [(d.kind, d.a, d.b) for d in compare.compare(a, b).differences]
    assert ("lyrics", "Back beat", "Backbeat") in got
    assert ("chord_detail", "G C", "G Cmaj7") in got


def test_chord_only_lines_are_compared_in_order():
    a = chordpro.parse("{soc}\n[G] [D] [Em] [C]\nla [G]la\n{eoc}\n")
    b = chordpro.parse("{soc}\n[G] [D] [Em] [D]\nla [G]la\n{eoc}\n")
    got = [(d.kind, d.where, d.a, d.b) for d in compare.compare(a, b).differences]
    assert got == [("chords", "Chorus, line 1", "G D Em C", "G D Em D")]


def test_tabber_headers_and_remarks_are_no_lyrics():
    text = plaintext.to_chordpro(
        "Artist: Oasis\nTab by: someone\n(NOTE: play softly)\nG      D\nToday is the day\n"
    )
    s = chordpro.parse(text)
    assert [ln.lyrics for sec in s.sections for ln in sec.lines if ln.comment is None] == [
        "Today is the day"
    ]
    assert "{comment: (NOTE: play softly)}" in text and "Oasis" not in text


def test_a_repeat_mark_repeats_a_line_of_chords():
    text = plaintext.to_chordpro("[Intro]\nEm7   G   Dsus4   A7sus4   x4\n")
    assert "[Em7] [G] [Dsus4] [A7sus4] x4" in text
    a = chordpro.parse(text)
    b = chordpro.parse("{c: Intro}\n" + "[Em7] [G] [Dsus4] [A7sus4]\n" * 4)
    assert compare.compare(a, b).differences == []


def test_verses_pair_by_their_words_not_their_place():
    a = chordpro.parse(
        "{sov: Verse 1}\n[C]I heard there was a [Am]secret chord\n{eov}\n"
        "{sov: Verse 2}\n[C]Your faith was strong but [Am]you needed proof\n{eov}\n"
    )
    b = chordpro.parse(
        "{sov: Verse 1}\n[C]Your faith was strong but [Am]you needed proof\n{eov}\n"
        "{sov: Verse 2}\n[C]I heard there was a [Am7]secret chord\n{eov}\n"
    )
    assert compare.pair_sections(a, b) == [(0, 1), (1, 0)]
    got = [(d.kind, d.where) for d in compare.compare(a, b).differences]
    assert got == [("chord_detail", "Verse 1, line 1")]


@pytest.mark.parametrize(
    ("a", "b", "note"),
    [
        ("[C]Let it [C/E]be", "[C]Let it [C]be", "second is simplified"),
        (
            "[Am]Whisper [Am/G]words [Fmaj7]of [F6]wisdom",
            "[Am]Whisper words [F]of wisdom",
            "second is simplified",
        ),
        ("[F]Let it be", "[Fmaj7]Let it [F6]be", "second adds detail"),
    ],
)
def test_movements_and_bass_notes_are_detail(a, b, note):
    got = compare.compare(chordpro.parse(a + "\n"), chordpro.parse(b + "\n")).differences
    assert [(d.kind, d.note) for d in got] == [("chord_detail", note)]


def test_a_chord_one_word_off_is_no_difference_two_words_are():
    a = chordpro.parse("[Am]Let it be let [F]it be\n")
    assert compare.compare(a, chordpro.parse("[Am]Let it be [F]let it be\n")).differences == []
    far = compare.compare(a, chordpro.parse("[Am]Let it [F]be let it be\n")).differences
    assert [d.kind for d in far] == ["chord_position"]


def test_a_held_chord_in_a_tab_is_no_chord():
    assert parse_chord("C------") is None
