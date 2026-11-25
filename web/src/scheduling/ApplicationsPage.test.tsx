import { describe, expect, it } from "vitest";
import type { Measurements, QualityCounters } from "../types";
import { rowsOf, tilesOf } from "./ApplicationsPage";

const measure = (rows: Measurements["rows"]): Measurements => ({ days: 7, rows });
const quality = (rows: QualityCounters["rows"]): QualityCounters => ({
  days: 7,
  min_looks: 5,
  floors: {},
  rows,
});

describe("tilesOf", () => {
  it("sums one application over its models and machines, and weights the waiting by requests", () => {
    const tiles = tilesOf(
      measure([
        { application: "chat", model: "big", node: "a", requests: 10, failed: 1, cancelled: 1, cut_off: 2, waited_seconds: 1, generated_tps: null, prompt_tps: null, cached_share: null },
        { application: "chat", model: "small", node: "b", requests: 30, failed: 0, cancelled: 0, cut_off: 0, waited_seconds: 3, generated_tps: null, prompt_tps: null, cached_share: null },
      ]),
      quality([
        { application: "chat", model: "big", looks: 10, ok: 8, hint: 1, error: 1, ok_share: 0.8, floor: null, below_floor: false },
      ]),
    );
    const chat = tiles.find((t) => t.application === "chat")!;
    expect(chat.requests).toBe(40);
    expect(chat.stopped).toBe(2);
    expect(chat.cutOff).toBe(2);
    expect(chat.nodes).toBe(2);
    expect(chat.topModel).toBe("small");
    expect(chat.waitedSeconds).toBeCloseTo((1 * 10 + 3 * 30) / 40);
    expect(chat.greenShare).toBe(0.8);
  });

  it("shows every known application even without figures, with dashes instead of numbers", () => {
    const tiles = tilesOf(measure([]), quality([]));
    const research = tiles.find((t) => t.application === "research")!;
    expect(research.requests).toBe(0);
    expect(research.waitedSeconds).toBeNull();
    expect(research.topModel).toBeNull();
    expect(research.greenShare).toBeNull();
  });
});

describe("rowsOf", () => {
  it("keeps only the rows of the application the detail page shows", () => {
    const base = { failed: 0, cancelled: 0, cut_off: 0, waited_seconds: 0, generated_tps: null, prompt_tps: null, cached_share: null };
    const rows = rowsOf(
      measure([
        { ...base, application: "chat", model: "big", node: "a", requests: 3 },
        { ...base, application: "dev", model: "coder", node: "b", requests: 9 },
      ]),
      "dev",
    );
    expect(rows).toHaveLength(1);
    expect(rows[0]?.model).toBe("coder");
  });
});
