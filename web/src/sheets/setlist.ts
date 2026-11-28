// Setlists: the order of songs for one evening. The hub stores each song as one of its sheets (and a
// `newest` flag from the time a setlist could pin a version, #159). A song has one version in use,
// its newest (group.ts groups the sheets into songs), and every item plays that one, whichever
// version it was added with: a new version shows up in every setlist at once.

import { groupSheets, inUse, matchesSearch, type SheetGroup } from "./group";
import type { SetlistItem, SheetSummary } from "./types";

export interface SetlistEntry {
  item: SetlistItem;
  /** The sheet played for this item, the version in use of its song; null when the sheet the
   *  item names is not in the list (deleted meanwhile). */
  sheet: SheetSummary | null;
}

/** The items as they are played, each resolved to the sheet it opens. */
export function resolveSetlist(items: SetlistItem[], list: SheetSummary[]): SetlistEntry[] {
  const groups = groupSheets(list);
  const songOf = new Map<string, SheetGroup>();
  for (const g of groups) for (const s of g.versions) songOf.set(s.id, g);
  return items.map((item) => {
    const g = songOf.get(item.sheet_id);
    return { item, sheet: (g && inUse(g)) ?? null };
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

/** How a setlist is looked at: in the order it is played, or as an index by title or by artist
 *  then title. Only the view changes; the stored order stays. */
export type SetlistView = "setlist" | "title" | "artist";
export const SETLIST_VIEWS: SetlistView[] = ["setlist", "title", "artist"];

/** The positions (indexes into `entries`) in the order the view shows them, keeping only the
 *  songs the search finds (same matching as the library). Each row keeps its position, so the
 *  title view reads like an index of the set. Deleted sheets come last in a sorted view and are
 *  never found by a search; equal songs keep their setlist order. */
export function viewOrder(entries: SetlistEntry[], view: SetlistView, query: string): number[] {
  const shown = entries
    .map((e, i) => ({ e, i }))
    .filter(({ e }) => !query.trim() || (!!e.sheet && matchesSearch(e.sheet.title, e.sheet.artist, query)));
  if (view !== "setlist") {
    const byTitle = (a: SetlistEntry, b: SetlistEntry) => a.sheet!.title.localeCompare(b.sheet!.title);
    shown.sort((x, y) => {
      const a = x.e;
      const b = y.e;
      if (!a.sheet || !b.sheet) return (a.sheet ? 0 : 1) - (b.sheet ? 0 : 1) || x.i - y.i;
      const order = view === "artist" ? a.sheet.artist.localeCompare(b.sheet.artist) || byTitle(a, b) : byTitle(a, b);
      return order || x.i - y.i;
    });
  }
  return shown.map(({ i }) => i);
}

/** Whether the rows may be reordered: only in setlist order without a search, where a moved row
 *  lands where it is seen. */
export function canReorder(view: SetlistView, query: string): boolean {
  return view === "setlist" && !query.trim();
}
