import { describe, expect, it } from "vitest";
import type { Handover, HandoverStatus } from "../types";
import { groupHandovers } from "./groups";

const h = (id: string, status: string) =>
  ({ id, status: status as HandoverStatus }) as Handover;
const ids = (list: Handover[]) => list.map((x) => x.id);

describe("groupHandovers", () => {
  it("puts each status into its group and keeps the hub's order", () => {
    const g = groupHandovers([
      h("a", "done"),
      h("b", "proposed"),
      h("c", "sent"),
      h("d", "cancelled"),
      h("e", "failed"),
      h("f", "proposed"),
    ]);
    expect(ids(g.waiting)).toEqual(["b", "f"]);
    expect(ids(g.running)).toEqual(["c"]);
    expect(ids(g.done)).toEqual(["a", "e"]);
    expect(ids(g.withdrawn)).toEqual(["d"]);
  });

  it("keeps a hand-over with a status it does not know, under done", () => {
    expect(ids(groupHandovers([h("x", "archived")]).done)).toEqual(["x"]);
  });

  it("returns four empty groups for an empty list", () => {
    expect(groupHandovers([])).toEqual({
      waiting: [],
      running: [],
      done: [],
      withdrawn: [],
    });
  });
});
