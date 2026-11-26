import { describe, expect, it } from "vitest";
import { neighbour } from "./fit";
import { moveItem, resolveSetlist, setlistGroups } from "./setlist";
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
  it("plays a pinned version as it is and a newest item as the newest version of its song", () => {
    const got = resolveSetlist(
      [
        { sheet_id: "a1", newest: false },
        { sheet_id: "a1", newest: true },
        { sheet_id: "gone", newest: false },
      ],
      LIST,
    );
    expect(got.map((e) => [e.sheet?.id ?? null, e.version, e.versions])).toEqual([
      ["a1", 1, 2],
      ["a2", 2, 2],
      [null, 0, 0],
    ]);
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
