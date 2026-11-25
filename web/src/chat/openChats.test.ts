import { describe, expect, it } from "vitest";
import type { Conversation } from "../types";
import { openGroups } from "./openChats";

const c = (id: string, over: Partial<Conversation>): Conversation =>
  ({ id, title: id, model: null, node: null, created_at: null, updated_at: "2026-11-13T10:00:00Z", ...over }) as Conversation;

describe("Start's open conversations", () => {
  it("are the open and running ones, by application in the navigation's order, pinned first", () => {
    const groups = openGroups([
      c("old", { application: "research", open: false }),
      c("r1", { application: "research", open: true, updated_at: "2026-11-13T12:00:00Z" }),
      c("r2", { application: "research", open: true, pinned: true, updated_at: "2026-11-01T12:00:00Z" }),
      c("s1", { application: null, open: true }),
      c("d1", { application: "dev", open: false, running: true }),
    ]);
    expect(groups.map((g) => [g.application, g.chats.map((x) => x.id)])).toEqual([
      ["chat", ["s1"]],
      ["research", ["r2", "r1"]],
      ["dev", ["d1"]],
    ]);
  });
});
