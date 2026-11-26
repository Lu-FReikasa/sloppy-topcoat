// Setlists: the order of songs for one evening. The hub stores each song as a sheet plus "this
// version" or "the newest version"; which version is newest is decided here, where the sheets are
// grouped into songs (group.ts), so a new version shows up in every setlist that wants the newest.

import { groupSheets, type SheetGroup } from "./group";
import type { SetlistItem, SheetSummary } from "./types";

export interface SetlistEntry {
  item: SetlistItem;
  /** The sheet played for this item; null when it is not in the list (deleted meanwhile). */
  sheet: SheetSummary | null;
  /** Which version of its song the sheet is (1 = oldest), and how many there are. */
  version: number;
  versions: number;
}

/** The items as they are played, each resolved to the sheet it opens. */
export function resolveSetlist(items: SetlistItem[], list: SheetSummary[]): SetlistEntry[] {
  const groups = groupSheets(list);
  const songOf = new Map<string, SheetGroup>();
  for (const g of groups) for (const s of g.versions) songOf.set(s.id, g);
  return items.map((item) => {
    const g = songOf.get(item.sheet_id);
    if (!g) return { item, sheet: null, version: 0, versions: 0 };
    const sheet = item.newest ? g.versions[g.versions.length - 1]! : g.versions.find((s) => s.id === item.sheet_id)!;
    return { item, sheet, version: g.versions.indexOf(sheet) + 1, versions: g.versions.length };
  });
}

/** The setlist as songs for the stage's paging (fit.ts neighbour): one song per playable item,
 *  holding only the sheet it opens. A song's key is the item's index in the setlist. */
export function setlistGroups(items: SetlistItem[], list: SheetSummary[]): SheetGroup[] {
  const out: SheetGroup[] = [];
  resolveSetlist(items, list).forEach((e, i) => {
    if (e.sheet) out.push({ key: String(i), title: e.sheet.title, artist: e.sheet.artist, versions: [e.sheet] });
  });
  return out;
}

/** The items with the one at `from` moved to `to` (both clamped to the list). */
export function moveItem<T>(items: T[], from: number, to: number): T[] {
  if (from < 0 || from >= items.length) return items;
  const target = Math.max(0, Math.min(items.length - 1, to));
  if (target === from) return items;
  const out = [...items];
  const [moved] = out.splice(from, 1);
  out.splice(target, 0, moved!);
  return out;
}
