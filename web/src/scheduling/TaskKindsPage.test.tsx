import { describe, expect, it } from "vitest";
import type { ModelSwitches } from "../types";
import { loadsOf } from "./TaskKindsPage";

const view = (loads_by_work: Record<string, number>): ModelSwitches => ({
  days: 7,
  choice_mode: "alt",
  totals: {
    requests: 0,
    loads: 0,
    switches: 0,
    review_requests: 0,
    review_loads: 0,
    waited_seconds_on_loads: 0,
    loads_by_work,
    loads_by_reason: {},
  },
  review_load_share: null,
  cells: [],
});

describe("loadsOf", () => {
  it("counts the loads of a kind by its label, zero when there were none", () => {
    const v = view({ review: 4, chat: 1 });
    expect(loadsOf(v, "review")).toBe(4);
    expect(loadsOf(v, "background")).toBe(0);
  });

  it("gives no number for a kind that has no label of its own yet, instead of a guess", () => {
    expect(loadsOf(view({ chat: 1 }), null)).toBeNull();
  });
});
