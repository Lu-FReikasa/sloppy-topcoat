"""A sheet as a view shows it: for one instrument, with or without the capo, transposed or not.

The stored sheet never changes for a view. The guitar view with ``capo_view`` shows the shapes
played with the capo and names their key; without it, or on the piano, it shows the chords that
sound (the piano has no capo). ``transpose`` moves what sounds; the capo stays where it is.
"""

from typing import Any

from topcoat_hub.sheets import chordpro, keys
from topcoat_hub.sheets.chordpro import Line, Sheet
from topcoat_hub.sheets.chords import Key

INSTRUMENTS = ("guitar", "piano")


def sounding_key(sheet: Sheet) -> tuple[Key | None, bool]:
    """The sheet's key and whether it was estimated (the sheet names none)."""
    if sheet.key is not None:
        return sheet.key, False
    return keys.estimate(sheet.chords())[0], True


def _line(line: Line, flats: bool) -> dict[str, Any]:
    if line.comment is not None:
        return {"comment": line.comment}
    return {
        "segments": [
            {
                "chord": g.chord.name(flats) if g.chord is not None else g.raw,
                "text": g.text,
            }
            for g in line.segments
        ]
    }


def render(
    sheet: Sheet, instrument: str, capo_view: bool = True, transpose: int = 0
) -> dict[str, Any]:
    if instrument not in INSTRUMENTS:
        raise ValueError(f"unknown instrument {instrument!r}")
    s = sheet.transpose(transpose) if transpose else sheet
    key, estimated = sounding_key(s)
    capo = s.capo if instrument == "guitar" else 0
    with_capo = instrument == "guitar" and capo_view and capo > 0
    shown = s.transpose(-capo) if with_capo else s
    shown_key = key.transpose(-capo) if (key and with_capo) else key
    flats = shown_key.flats if shown_key else False
    return {
        "title": s.title,
        "artist": s.artist,
        "instrument": instrument,
        "key": key.name if key else None,
        "key_estimated": estimated,
        "shown_key": shown_key.name if shown_key else None,
        "capo": capo,
        "capo_view": with_capo,
        "transpose": transpose,
        "flow": [
            {"index": i, "kind": sec.kind, "label": sec.label, "repeat_of": sec.repeat_of}
            for i, sec in enumerate(shown.sections)
        ],
        "sections": [
            {
                "index": i,
                "kind": sec.kind,
                "label": sec.label,
                "repeat_of": sec.repeat_of,
                "lines": [_line(ln, flats) for ln in sec.lines],
            }
            for i, sec in enumerate(shown.sections)
        ],
        "chordpro": chordpro.write(s, view="capo" if with_capo else "sounding"),
    }
