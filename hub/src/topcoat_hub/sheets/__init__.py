"""Sheets: chord sheets as data (the application ``sheets``).

The music in a sheet is computed here, not guessed by a model: chords are parsed, transposed and
spelled by the key, a capo is a view of the sounding chords, and two sheets are compared after both
are brought into degrees of their key, so a transposed or capo version of the same song is not a
difference. A model only finds sources, reads what a page says about its version and writes the
report; the numbers come from this package.

``chords``      a chord, its pitch classes, transposition and spelling by key
``chordpro``    ChordPro read and written (stored: sounding pitch + ``x_capo``)
``plaintext``   a "chords over lyrics" page turned into a sheet (column positions kept)
``keys``        the key of a sheet, estimated from its chords when it does not name one
``compare``     two sheets: transposition, capo, form, chords and lyrics, line by line
"""
