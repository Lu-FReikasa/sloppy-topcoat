"""Clean the stored chord sheets with the rules that now apply to every import (sheets/rules.py,
#145, #152): chords written as text become chords, tab goes (a sheet is lyrics and chords),
header lines (title, artist, source, tuning) become facts or go. Runs inside the hub container:
    docker cp scripts/dev/sheets_retidy.py topcoat-hub-1:/tmp/sheets_retidy.py
    docker exec topcoat-hub-1 /app/hub/.venv/bin/python /tmp/sheets_retidy.py          # dry run
    docker exec topcoat-hub-1 /app/hub/.venv/bin/python /tmp/sheets_retidy.py --apply

Only the newest version of each song is read, and a changed one is written as a NEW version (same
title, artist, instrument, capo view, sources and report; a version note says why), so the old one
stays and the person picks in the reader. The output names titles and counts only, never a line of
a sheet (song lyrics stay out of logs).
"""

import argparse
import asyncio
from collections import defaultdict

from sqlalchemy import select

from topcoat_hub.config import Settings
from topcoat_hub.db import make_engine, make_sessionmaker
from topcoat_hub.models import SheetRow
from topcoat_hub.sheets import chordpro, rules
from topcoat_hub.sheets.store import SheetError, SheetStore

NOTE = "tidy rules (#145): chords as text, tab blocks, header"  # kept: the written versions carry it


def _plain(text: str) -> str:
    return " ".join("".join(c if c.isalnum() else " " for c in text.lower()).split())


def _shape(sheet: chordpro.Sheet) -> tuple[int, int, int]:
    """Chords, lines and tab parts: what the report compares."""
    lines = sum(len(s.lines) for s in sheet.sections)
    tabs = sum(1 for s in sheet.sections if s.kind == "tab")
    return len(sheet.chords()), lines, tabs


async def retidy(db, user_id, apply: bool, out: defaultdict) -> None:
    rows = (
        await db.scalars(select(SheetRow).where(SheetRow.user_id == user_id).order_by(SheetRow.created_at))
    ).all()
    newest: dict[str, SheetRow] = {}
    for r in rows:  # oldest first: the last one per song wins
        newest[f"{_plain(r.title)}|{_plain(r.artist)}"] = r
    store = SheetStore(db, user_id)
    for row in sorted(newest.values(), key=lambda r: r.title.lower()):
        sheet = chordpro.parse(row.chordpro)
        before = _shape(sheet)
        done = rules.apply(sheet)
        text = chordpro.write(sheet)
        if text == row.chordpro:
            out["unchanged"] += 1
            continue
        after = _shape(sheet)
        print(
            f"  {row.title[:40]:40} chords {before[0]:3}->{after[0]:3}  lines {before[1]:3}->{after[1]:3}"
            f"  tab parts {before[2]}->{after[2]}  chord lines {done.chord_lines:2}"
            f"  header lines {len(done.removed)}"
        )
        out["changed"] += 1
        out["chord_lines"] += done.chord_lines
        out["tab_blocks"] += done.tabs
        out["header_lines"] += len(done.removed)
        if not apply:
            continue
        try:
            if row.version_note == NOTE:  # a version this script wrote: cleaned again in place
                await store.update(row.id, text=text, title=row.title, artist=row.artist)
                out["updated"] += 1
                continue
            new = await store.create(
                row.chordpro,  # read() applies the same rules again
                instrument=row.instrument,
                title=row.title,
                artist=row.artist,
                version_note=NOTE,
                sources=list(row.sources or []),
                report_id=row.report_id,
            )
            new.capo_view = row.capo_view
            out["written"] += 1
        except SheetError as e:
            print(f"    not written: {e.message}")
            out["refused"] += 1


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true", help="write new versions (default: dry run)")
    args = parser.parse_args()
    sessionmaker = make_sessionmaker(make_engine(Settings().database_url))
    out: defaultdict = defaultdict(int)
    async with sessionmaker() as db:
        users = (await db.scalars(select(SheetRow.user_id).distinct())).all()
    for user_id in users:
        print(f"user {user_id}")
        async with sessionmaker() as db:
            await retidy(db, user_id, args.apply, out)
            if args.apply:
                await db.commit()
    print("\nsummary:", dict(sorted(out.items())), "(applied)" if args.apply else "(dry run)")


if __name__ == "__main__":
    asyncio.run(main())
