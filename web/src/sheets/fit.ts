// Fitting a song on one screen (the stage mode): CSS columns plus the largest font size at which
// nothing overflows. The search is pure; the caller measures the real element (fits()).

import { groupSheets, plain, type SheetGroup } from "./group";
import type { SheetSummary } from "./types";

export const MIN_FONT = 12;
export const MAX_FONT = 40;

export interface Fit {
  columns: number;
  font: number;
  /** false: even the smallest font with the most columns overflows; the page scrolls. */
  fits: boolean;
}

/** How many columns a screen of this width may use: a column wants about 560 px
 *  (1920 wide → 3, 1200 wide → 2), at least 1, at most 4. */
export function maxColumns(width: number): number {
  return Math.max(1, Math.min(4, Math.floor(width / 560)));
}

/** The largest font (whole px, MIN_FONT…MAX_FONT) at which fits(columns, font) holds, per column
 *  count 1…most; the column count with the largest font wins, fewer columns on a tie. fits must be
 *  monotonic in the font size (a smaller font never overflows where a larger one fits). */
export function chooseFit(most: number, fits: (columns: number, font: number) => boolean): Fit {
  let best: Fit | null = null;
  for (let columns = 1; columns <= most; columns++) {
    if (!fits(columns, MIN_FONT)) continue;
    let lo = MIN_FONT;
    let hi = MAX_FONT;
    while (lo < hi) {
      const mid = Math.ceil((lo + hi) / 2);
      if (fits(columns, mid)) lo = mid;
      else hi = mid - 1;
    }
    if (!best || lo > best.font) best = { columns, font: lo, fits: true };
  }
  return best ?? { columns: most, font: MIN_FONT, fits: false };
}

export type SortMode = "title" | "artist" | "added";
export const SORT_MODES: SortMode[] = ["title", "artist", "added"];

const newest = (g: SheetGroup) => {
  const last = g.versions[g.versions.length - 1];
  return last?.created_at ?? last?.updated_at ?? "";
};

/** The songs in the order the list shows: by title, by artist then title, or newest first. */
export function sortGroups(groups: SheetGroup[], mode: SortMode): SheetGroup[] {
  const byTitle = (a: SheetGroup, b: SheetGroup) => a.title.localeCompare(b.title);
  const out = [...groups];
  if (mode === "artist") out.sort((a, b) => a.artist.localeCompare(b.artist) || byTitle(a, b));
  else if (mode === "added") out.sort((a, b) => newest(b).localeCompare(newest(a)) || byTitle(a, b));
  else out.sort(byTitle);
  return out;
}

/** The songs the list shows: filtered by search and artist, sorted. */
export function visibleGroups(list: SheetSummary[], query: string, artist: string, mode: SortMode): SheetGroup[] {
  const q = plain(query);
  const shown = groupSheets(list).filter(
    (g) => (!artist || plain(g.artist) === plain(artist)) && (!q || plain(`${g.title} ${g.artist}`).includes(q)),
  );
  return sortGroups(shown, mode);
}

/** Where the sheet `id` is among the songs: `at` when that song holds it (a setlist may hold one
 *  song twice), else the first song that does; -1 when none does. */
export function positionOf(groups: SheetGroup[], id: string, at?: number): number {
  const holds = (g: SheetGroup | undefined) => !!g?.versions.some((s) => s.id === id);
  if (at !== undefined && holds(groups[at])) return at;
  return groups.findIndex(holds);
}

/** The sheet to open when paging from `id` by `step` songs through the shown songs: the newest
 *  version of the neighbour song. null at either end, or when `id` is not shown. `at` is where
 *  `id` is when the songs repeat (see positionOf). */
export function neighbour(groups: SheetGroup[], id: string, step: number, at?: number): string | null {
  const here = positionOf(groups, id, at);
  if (here < 0) return null;
  const g = groups[here + step];
  return g?.versions[g.versions.length - 1]?.id ?? null;
}

/** A line that is guitar tablature or a riff drawn in dashes ("e|--3--|", "--0--2--"). */
export function isTabLine(text: string): boolean {
  const t = text.trim();
  return /^[a-gA-G]?\s*[|:]?[-0-9hpbr/\\|x~.]{6,}/.test(t) && (t.match(/-/g)?.length ?? 0) >= 4;
}
