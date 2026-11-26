// Fitting a song on one screen (the stage mode): CSS columns plus the largest font size at which
// nothing overflows. The search is pure; the caller measures the real element (fits()).

import { groupSheets, matchesSearch, plain, type SheetGroup } from "./group";
import type { SheetSummary } from "./types";

export const MIN_FONT = 12;
export const MAX_FONT = 40;

export interface Fit {
  columns: number;
  font: number;
  /** false: even the smallest font with the most columns overflows; the page scrolls. */
  fits: boolean;
}

/** How many columns a screen of this width may try: a column needs about 260 CSS px, at least 1,
 *  at most 4. The fit measures each try (long lines wrap) and keeps the one with the largest font,
 *  so a scaled laptop screen (1200 px at 150 % is 800 CSS px) still gets two columns. */
export function maxColumns(width: number): number {
  return Math.max(1, Math.min(4, Math.floor(width / 260)));
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
  const shown = groupSheets(list).filter(
    (g) => (!artist || plain(g.artist) === plain(artist)) && matchesSearch(g.title, g.artist, query),
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

/** The parts (by their heights, in order) placed into at most `columns` columns of height
 *  `capacity`, each column filled before the next; a part never breaks. The indexes per column,
 *  or null when they do not fit. Browsers differ in how CSS columns overflow, so the stage places
 *  the parts itself. */
export function packColumns(heights: number[], columns: number, capacity: number): number[][] | null {
  const out: number[][] = [[]];
  let used = 0;
  for (let i = 0; i < heights.length; i++) {
    const h = heights[i]!;
    if (h > capacity) return null;
    if (used + h > capacity && out[out.length - 1]!.length > 0) {
      if (out.length === columns) return null;
      out.push([]);
      used = 0;
    }
    out[out.length - 1]!.push(i);
    used += h;
  }
  return out;
}

/** The parts spread over `columns` columns as evenly as the order allows (for a song that does not
 *  fit one screen: the page then scrolls, the columns stay about equally long). */
export function balanceColumns(heights: number[], columns: number): number[][] {
  const total = heights.reduce((a, b) => a + b, 0);
  const tallest = Math.max(0, ...heights);
  let lo = Math.max(tallest, total / columns);
  let hi = total;
  while (hi - lo > 1) {
    const mid = (lo + hi) / 2;
    if (packColumns(heights, columns, mid)) hi = mid;
    else lo = mid;
  }
  return packColumns(heights, columns, hi) ?? [heights.map((_, i) => i)];
}
