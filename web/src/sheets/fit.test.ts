import { describe, expect, it } from "vitest";
import { balanceColumns, chooseFit, packColumns, isTabLine, maxColumns, MAX_FONT, MIN_FONT, neighbour, positionOf, sortGroups, visibleGroups } from "./fit";
import { groupSheets } from "./group";
import type { SheetSummary } from "./types";

/** A screen model: `lines` lines of `width` chars; a column holds (H / 1.3 font) lines of
 *  (W / columns / 0.6 font) chars, longer lines wrap. */
function screen(W: number, H: number, lines: number, width: number) {
  return (columns: number, font: number) => {
    const chars = Math.floor(W / columns / (0.6 * font));
    const rows = lines * Math.ceil(width / chars);
    return rows <= columns * Math.floor(H / (1.3 * font));
  };
}

describe("chooseFit", () => {
  it("picks the column count with the largest font", () => {
    const f = chooseFit(2, screen(1200, 1850, 160, 60));
    expect(f.fits).toBe(true);
    expect(f.columns).toBe(2);
    expect(f.font).toBeGreaterThanOrEqual(MIN_FONT);
    // the font found is the largest that fits
    expect(screen(1200, 1850, 160, 60)(2, f.font + 1)).toBe(false);
  });

  it("keeps one column with a large font for a short song", () => {
    const f = chooseFit(2, screen(1200, 1850, 30, 40));
    expect(f).toMatchObject({ columns: 1, fits: true });
    expect(f.font).toBeGreaterThan(20);
  });

  it("caps the font and prefers fewer columns on a tie", () => {
    expect(chooseFit(3, () => true)).toEqual({ columns: 1, font: MAX_FONT, fits: true });
  });

  it("gives up at the smallest font with the most columns", () => {
    expect(chooseFit(3, () => false)).toEqual({ columns: 3, font: MIN_FONT, fits: false });
  });

  it("lets a scaled laptop screen try several columns", () => {
    expect(maxColumns(1920)).toBe(4);
    expect(maxColumns(800)).toBe(3);
    expect(maxColumns(400)).toBe(1);
  });
});

const sheet = (id: string, title: string, artist: string, created: string): SheetSummary => ({
  id, title, artist, instrument: "guitar", key: null, capo: 0, capo_view: false, version_note: "",
  sources: 0, report_id: null, updated_at: null, created_at: created,
});
const LIST = [
  sheet("a", "Banana", "Zed", "2026-01-01"),
  sheet("b", "Apple", "Yak", "2026-03-01"),
  sheet("c", "Cherry", "Yak", "2026-02-01"),
  sheet("b2", "Apple", "Yak", "2026-04-01"),
];

describe("sorting and paging", () => {
  const ids = (mode: "title" | "artist" | "added") => sortGroups(groupSheets(LIST), mode).map((g) => g.title);

  it("sorts by title, by artist then title, and newest first", () => {
    expect(ids("title")).toEqual(["Apple", "Banana", "Cherry"]);
    expect(ids("artist")).toEqual(["Apple", "Cherry", "Banana"]);
    expect(ids("added")).toEqual(["Apple", "Cherry", "Banana"]);
  });

  it("pages to the newest version of the neighbour song in the shown order", () => {
    const groups = visibleGroups(LIST, "", "", "title");
    expect(neighbour(groups, "a", -1)).toBe("b2");
    expect(neighbour(groups, "b", 1)).toBe("a");
    expect(neighbour(groups, "c", 1)).toBeNull();
    // a song shown twice (a setlist's encore): the position decides where paging goes on
    const twice = [groups[0]!, groups[1]!, groups[0]!];
    expect(positionOf(twice, groups[0]!.versions[0]!.id, 2)).toBe(2);
    expect(positionOf(twice, groups[0]!.versions[0]!.id, 1)).toBe(0);
    expect(neighbour(twice, groups[0]!.versions[0]!.id, 1, 2)).toBeNull();
    expect(neighbour(twice, groups[0]!.versions[0]!.id, 1, 0)).toBe(groups[1]!.versions.at(-1)!.id);
    expect(neighbour(visibleGroups(LIST, "yak", "", "title"), "b", 1)).toBe("c");
    expect(neighbour(groups, "gone", 1)).toBeNull();
  });
});

describe("isTabLine", () => {
  it("spots tablature, not lyrics or chords", () => {
    expect(isTabLine("e|-----3---5---|")).toBe(true);
    expect(isTabLine("--0--2--3--2--0--")).toBe(true);
    expect(isTabLine("Walking down the road - again")).toBe(false);
    expect(isTabLine("C   G   Am   F")).toBe(false);
  });
});

describe("packColumns", () => {
  it("fills each column before the next and never breaks a part", () => {
    expect(packColumns([3, 3, 3, 3], 2, 7)).toEqual([[0, 1], [2, 3]]);
    expect(packColumns([3, 3, 3, 3], 2, 5)).toBeNull();
    expect(packColumns([8], 3, 7)).toBeNull();
    expect(packColumns([1, 1], 3, 10)).toEqual([[0, 1]]);
  });

  it("balances a song that does not fit over the columns", () => {
    expect(balanceColumns([4, 4, 4, 4, 4, 4], 3)).toEqual([[0, 1], [2, 3], [4, 5]]);
    expect(balanceColumns([10, 1, 1], 2)).toHaveLength(2);
  });
});
