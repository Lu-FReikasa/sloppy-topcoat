import { describe, expect, it } from "vitest";
import { neighbour } from "./fit";
import { canReorder, moveItem, resolveSetlist, setlistGroups, viewOrder } from "./setlist";
import type { SheetSummary } from "./types";

const sheet = (id: string, title: string, created: string): SheetSummary => ({
  id,
  title,
  artist: "Example Band",
  instrument: "guitar",
  key: "A",
  capo: 0,
  capo_view: true,
  version_note: "",
  sources: 0,
  report_id: null,
  updated_at: created,
  created_at: created,
});

// "Song A" in two versions (a1 older, a2 newer), "Song B" once
const LIST = [
  sheet("a2", "Song A", "2026-11-21T10:00:00Z"),
  sheet("b", "Song B", "2026-11-20T09:00:00Z"),
  sheet("a1", "Song A", "2026-11-20T10:00:00Z"),
];

describe("setlists", () => {
  it("plays the version in use of its song, whichever version an item names and whatever its flag", () => {
    const got = resolveSetlist(
      [
        { sheet_id: "a1", newest: false },
        { sheet_id: "a1", newest: true },
        { sheet_id: "a2", newest: false },
        { sheet_id: "b", newest: false },
        { sheet_id: "gone", newest: true },
      ],
      LIST,
    );
    expect(got.map((e) => e.sheet?.id ?? null)).toEqual(["a2", "a2", "a2", "b", null]);
  });

  it("pages in setlist order, past a deleted sheet, and through a song played twice", () => {
    const items = [
      { sheet_id: "b", newest: false },
      { sheet_id: "gone", newest: false },
      { sheet_id: "a1", newest: true },
      { sheet_id: "b", newest: false },
    ];
    const songs = setlistGroups(items, LIST);
    expect(songs.map((g) => [g.key, g.versions[0]!.id])).toEqual([
      ["0", "b"],
      ["2", "a2"],
      ["3", "b"],
    ]);
    // by title the library pages a2 → b and stops; the setlist goes b → a2 → b and ends there
    expect(neighbour(songs, "b", 1, 0)).toBe("a2");
    expect(neighbour(songs, "a2", 1, 1)).toBe("b");
    expect(neighbour(songs, "b", 1, 2)).toBeNull();
    expect(neighbour(songs, "b", -1, 2)).toBe("a2");
    expect(neighbour(songs, "b", -1, 0)).toBeNull();
  });

  it("moves an item and clamps at both ends", () => {
    expect(moveItem(["x", "y", "z"], 0, 2)).toEqual(["y", "z", "x"]);
    expect(moveItem(["x", "y", "z"], 2, 0)).toEqual(["z", "x", "y"]);
    expect(moveItem(["x", "y", "z"], 1, 5)).toEqual(["x", "z", "y"]);
    const same = ["x", "y"];
    expect(moveItem(same, 0, -1)).toBe(same);
  });
});

describe("setlist views", () => {
  const song = (id: string, title: string, artist: string): SheetSummary => ({ ...sheet(id, title, "2026-11-20T09:00:00Z"), artist });
  // the evening's order: 1 Zebra (Alpha Band), 2 Moon (Beta Band), 3 deleted, 4 Apple (Beta Band), 5 Zebra again
  const list = [song("z", "Zebra Song", "Alpha Band"), song("m", "Moon Song", "Beta Band"), song("p", "Apple Song", "Beta Band")];
  const items = ["z", "m", "gone", "p", "z"].map((sheet_id) => ({ sheet_id, newest: false }));
  const entries = resolveSetlist(items, list);

  it("keeps the setlist order in the setlist view", () => {
    expect(viewOrder(entries, "setlist", "")).toEqual([0, 1, 2, 3, 4]);
  });

  it("sorts by title as an index, equal songs in setlist order, deleted sheets last", () => {
    expect(viewOrder(entries, "title", "")).toEqual([3, 1, 0, 4, 2]);
  });

  it("sorts by artist, then title", () => {
    expect(viewOrder(entries, "artist", "")).toEqual([0, 4, 3, 1, 2]);
  });

  it("finds songs by title or artist like the library, and never a deleted sheet", () => {
    expect(viewOrder(entries, "setlist", "beta")).toEqual([1, 3]);
    expect(viewOrder(entries, "title", "  ZEBRA-song ")).toEqual([0, 4]);
    expect(viewOrder(entries, "artist", "band")).toEqual([0, 4, 3, 1]);
    expect(viewOrder(entries, "setlist", "nothing like it")).toEqual([]);
  });

  it("does not touch the stored order", () => {
    const before = JSON.stringify(items);
    viewOrder(entries, "title", "song");
    expect(JSON.stringify(items)).toBe(before);
  });

  it("reorders only in setlist order without a search", () => {
    expect(canReorder("setlist", "")).toBe(true);
    expect(canReorder("setlist", "  ")).toBe(true);
    expect(canReorder("setlist", "moon")).toBe(false);
    expect(canReorder("title", "")).toBe(false);
    expect(canReorder("artist", "")).toBe(false);
  });
});
