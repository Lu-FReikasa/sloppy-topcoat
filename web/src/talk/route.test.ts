import { describe, expect, it } from "vitest";
import {
  adminHash,
  isAdminView,
  nodeHref,
  overviewHref,
  parseRoute,
} from "../route";

describe("routes of the user area", () => {
  it("parses talk and more", () => {
    expect(parseRoute("#/talk")).toEqual({ view: "talk" });
    expect(parseRoute("#/more/")).toEqual({ view: "more" });
  });

  it("knows which pages are the admin area", () => {
    for (const hash of [
      "#/nodes",
      "#/nodes/n1",
      "#/models",
      "#/access",
      "#/inventory",
      "#/identity",
      "#/repos",
      "#/dev",
      "#/care",
    ])
      expect(isAdminView(parseRoute(hash).view)).toBe(true);
    for (const hash of [
      "#/",
      "#/talk",
      "#/more",
      "#/archive",
      "#/tasks",
      "#/library",
      "#/memory",
      "#/settings",
    ])
      expect(isAdminView(parseRoute(hash).view)).toBe(false);
  });

  it("the admin pages live under #/admin and still answer on their old addresses", () => {
    expect(parseRoute("#/admin/nodes")).toEqual({ view: "overview" });
    expect(parseRoute("#/admin")).toEqual({ view: "overview" });
    expect(parseRoute("#/admin/nodes/n1")).toEqual({ view: "node", id: "n1" });
    expect(parseRoute("#/admin/models/a%2Fb")).toEqual({
      view: "model",
      name: "a/b",
    });
    expect(parseRoute("#/admin/repos")).toEqual({ view: "repos" });
    expect(parseRoute("#/admin/scheduling")).toEqual({ view: "scheduling" });
    // a user page under /admin is not a way around the frame
    expect(parseRoute("#/admin/memory")).toEqual({ view: "overview" });
    expect(parseRoute("#/nodes/n1")).toEqual({ view: "node", id: "n1" });
    expect(overviewHref).toBe("#/admin/nodes");
    expect(nodeHref("n 1")).toBe("#/admin/nodes/n%201");
  });

  it("moves an old admin address to the new one, and nothing else", () => {
    expect(adminHash("#/nodes")).toBe("#/admin/nodes");
    expect(adminHash("#/models/qwen")).toBe("#/admin/models/qwen");
    expect(adminHash("#/admin/nodes")).toBeNull();
    expect(adminHash("#/tasks")).toBeNull();
    expect(adminHash("#/")).toBeNull();
  });
});
