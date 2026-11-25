"""The key of a list of chords, and a chord as a degree of a key.

The estimate scores all 24 keys by how many chords are diatonic to them, with a bonus when the
tonic chord opens (more) or closes the song. Relative keys (G and Em) share their chords: the
opening and closing chords decide between them, a tie goes to the major key. It is an estimate for
sheets that do not name their key; a named key wins.
"""

from collections.abc import Sequence

from topcoat_hub.sheets.chords import Chord, Key

# Diatonic triads: semitones above the tonic -> family
_MAJOR = {0: "maj", 2: "min", 4: "min", 5: "maj", 7: "maj", 9: "min", 11: "dim"}
_MINOR = {0: "min", 2: "dim", 3: "maj", 5: "min", 7: "min", 8: "maj", 10: "maj"}
_MINOR_DOMINANT = {7: "maj"}  # harmonic minor: E in A minor
_DEGREES = ("I", "bII", "II", "bIII", "III", "IV", "#IV", "V", "bVI", "VI", "bVII", "VII")


def _fits(chord: Chord, key: Key) -> bool:
    step = (chord.root - key.tonic) % 12
    family = {"sus2": "maj", "sus4": "maj", "5": "maj"}.get(chord.triad, chord.triad)
    table = _MINOR if key.minor else _MAJOR
    if table.get(step) == family:
        return True
    if chord.triad in ("sus2", "sus4", "5") and step in table:
        return True
    return key.minor and _MINOR_DOMINANT.get(step) == family


def _is_tonic(chord: Chord, key: Key) -> bool:
    return chord.root == key.tonic and (chord.triad == "min") == key.minor


def estimate(chords: Sequence[Chord]) -> tuple[Key | None, float]:
    """The likeliest key and the share of chords that fit it (0..1)."""
    if not chords:
        return None, 0.0
    best: tuple[float, Key] | None = None
    for tonic in range(12):
        for minor in (False, True):
            key = Key(tonic, minor)
            score = sum(1.0 for c in chords if _fits(c, key))
            score += 0.5 * sum(1 for c in chords if _is_tonic(c, key))
            if _is_tonic(chords[0], key):
                # a song (or a fragment of one) opens on its tonic more often than it ends there
                score += 2.5
            if _is_tonic(chords[-1], key):
                score += 2.0
            if minor:
                score -= 0.25  # a tie goes to major
            if best is None or score > best[0]:
                best = (score, key)
    if best is None:  # pragma: no cover - the loop always runs
        return None, 0.0
    key = best[1]
    return key, sum(1 for c in chords if _fits(c, key)) / len(chords)


def degree(chord: Chord, key: Key) -> str:
    """A chord as a degree (Roman numeral): ``vi``, ``IV``, ``bVII7``, ``V/vii``… lower case for
    minor and diminished chords."""
    step = (chord.root - key.tonic) % 12
    name = _DEGREES[step]
    if chord.triad in ("min", "dim"):
        name = name.lower()
    tail = chord.suffix
    for head in ("min", "m"):
        if chord.triad == "min" and tail.startswith(head) and not tail.startswith("maj"):
            tail = tail[len(head) :]
            break
    if chord.triad == "dim":
        tail = tail.replace("dim", "°", 1) if tail.startswith("dim") else tail
    out = name + tail
    if chord.bass is not None:
        out += "/" + _DEGREES[(chord.bass - key.tonic) % 12]
    return out
