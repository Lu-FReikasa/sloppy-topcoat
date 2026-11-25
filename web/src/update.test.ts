import { describe, expect, it } from "vitest";
import { makeNode } from "./test/fixtures";
import type { NodeUpdate } from "./types";
import { updateState } from "./update";

const ready: NodeUpdate = {
  channel: "stable",
  capable: true,
  updater: "ok",
  key_ids: ["0123456789abcdef"],
  available: "0.3.0",
  newer: true,
  action_allowed: true,
};

const state = (patch: Partial<NodeUpdate> | null) =>
  updateState(makeNode(patch === null ? {} : { update: { ...ready, ...patch } }));

describe("updateState", () => {
  it("offers the update when everything is in place", () => {
    expect(state({})).toBe("available");
  });

  it("knows nothing without the hub's update section", () => {
    expect(state(null)).toBe("unknown");
  });

  it("reports no release, or one that is not newer", () => {
    expect(state({ available: null, newer: false })).toBe("no_release");
    expect(state({ newer: false })).toBe("current");
  });

  it("names the first thing that is missing", () => {
    expect(state({ capable: false })).toBe("not_capable");
    expect(state({ action_allowed: false })).toBe("not_allowed");
    expect(state({ updater: "missing" })).toBe("updater_missing");
    expect(state({ updater: null })).toBe("updater_missing");
    expect(state({ updater: "stale" })).toBe("updater_stale");
    // a build without key beats the missing permission: the permission would not help
    expect(state({ capable: false, action_allowed: false })).toBe("not_capable");
  });
});
