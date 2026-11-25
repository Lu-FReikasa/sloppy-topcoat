import { describe, expect, it } from "vitest";
import type { Conversation } from "../types";
import { applicationCounts, inApplication } from "./applications";

const conv = (id: string, application?: string): Conversation =>
  ({
    id,
    title: id,
    model: null,
    node: null,
    created_at: "2026-10-27T10:00:00Z",
    updated_at: "2026-10-27T10:00:00Z",
    message_count: 0,
    application,
  }) as Conversation;

describe("applications of the chat list", () => {
  const all = [
    conv("a", "research"),
    conv("b"),
    conv("c", "project"),
    conv("d", "research"),
    conv("e", "chat"),
  ];

  it("counts the conversations per application in the navigation's order", () => {
    expect(applicationCounts(all)).toEqual([
      { application: "chat", count: 2 },
      { application: "research", count: 2 },
      { application: "project", count: 1 },
    ]);
  });

  it("treats a conversation without an application as Chat", () => {
    expect(inApplication(all, "chat").map((c) => c.id)).toEqual(["b", "e"]);
  });

  it("shows every conversation for 'all' and only one application otherwise", () => {
    expect(inApplication(all, "all")).toHaveLength(5);
    expect(inApplication(all, "research").map((c) => c.id)).toEqual(["a", "d"]);
    expect(inApplication(all, "admin")).toEqual([]);
  });

  it("keeps an application the navigation does not know, after the known ones", () => {
    const counts = applicationCounts([conv("x", "music"), conv("y", "chat")]);
    expect(counts.map((c) => c.application)).toEqual(["chat", "music"]);
  });
});
