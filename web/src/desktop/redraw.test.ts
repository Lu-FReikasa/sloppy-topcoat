import { describe, expect, it } from "vitest";
import type { ModuleCtx } from "../layout/modules/render";
import type { UiBootstrap } from "../layout/types";
import { sameBoot, sameCtx } from "./redraw";

const base = {
  api: {},
  me: null,
  isAdmin: true,
  nodes: [],
  nodesLoaded: true,
  nodesError: null,
  refreshNodes: () => undefined,
  tick: 0,
  inboxTick: 0,
  dispatch: () => undefined,
} as unknown as ModuleCtx;

describe("sameCtx", () => {
  it("an agent step redraws the pages that follow it, not the wiki or a chat", () => {
    const next = { ...base, tick: 1 };
    expect(sameCtx("page.wiki", base, next)).toBe(true);
    expect(sameCtx("page.chat.dev", base, next)).toBe(true);
    expect(sameCtx("page.tasks", base, next)).toBe(false);
  });

  it("a node report redraws the machines, not the inbox", () => {
    const next = { ...base, nodes: [] };
    expect(sameCtx("page.inbox", base, next)).toBe(true);
    expect(sameCtx("page.overview", base, next)).toBe(false);
  });

  it("a page it does not know reads everything", () => {
    expect(sameCtx("page.something-new", base, { ...base, inboxTick: 3 })).toBe(false);
  });
});

describe("sameBoot", () => {
  it("a new preference alone redraws no page but the rights page", () => {
    const a = { modules: [], workspaces: [], prefs: { hue: 1 } } as unknown as UiBootstrap;
    const b = { ...a, prefs: { hue: 2 } } as UiBootstrap;
    expect(sameBoot("page.overview", a, b)).toBe(true);
    expect(sameBoot("layout.workspaces", a, b)).toBe(false);
  });
});
