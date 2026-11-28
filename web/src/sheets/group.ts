// The list of sheets as songs: versions of one song (same title and artist) grouped together.

import type { SheetSummary } from "./types";

/** A name as it compares: "The Beatles" and "the beatles" are one artist. */
export function plain(text: string): string {
  return text.toLowerCase().replace(/[^\p{L}\p{N}]+/gu, " ").trim();
}

/** Whether a song is found by a search: the plain words of the query anywhere in "title artist".
 *  The library and the setlists match the same way. */
export function matchesSearch(title: string, artist: string, query: string): boolean {
  const q = plain(query);
  return !q || plain(`${title} ${artist}`).includes(q);
}

export interface SheetGroup {
  key: string;
  title: string;
  artist: string;
  /** The versions of the song, oldest first: v1, v2 … */
  versions: SheetSummary[];
}

/** The version of a song in use: its newest. The library, the setlists, the stage and print open
 *  this one; older versions are only looked at in the open sheet's "Versions" tab (#159). */
export function inUse(g: SheetGroup): SheetSummary | undefined {
  return g.versions[g.versions.length - 1];
}

/** The song a sheet is a version of, or undefined when the sheet is not in the list. */
export function songOf(groups: SheetGroup[], id: string): SheetGroup | undefined {
  return groups.find((g) => g.versions.some((s) => s.id === id));
}

/** The sheets grouped by song (title and artist), the songs by title. */
export function groupSheets(list: SheetSummary[]): SheetGroup[] {
  const groups = new Map<string, SheetGroup>();
  for (const s of list) {
    const key = `${plain(s.title)}|${plain(s.artist)}`;
    const g = groups.get(key) ?? { key, title: s.title, artist: s.artist, versions: [] };
    g.versions.push(s);
    groups.set(key, g);
  }
  const when = (s: SheetSummary) => s.created_at ?? s.updated_at ?? "";
  for (const g of groups.values()) g.versions.sort((a, b) => when(a).localeCompare(when(b)));
  return [...groups.values()].sort((a, b) => a.title.localeCompare(b.title));
}

/** The artists of the list, one spelling each, by name. */
export function artistsOf(list: SheetSummary[]): string[] {
  const seen = new Map<string, string>();
  for (const s of list) if (s.artist && !seen.has(plain(s.artist))) seen.set(plain(s.artist), s.artist);
  return [...seen.values()].sort((a, b) => a.localeCompare(b));
}
