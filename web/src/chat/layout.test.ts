import { describe, expect, it } from "vitest";
import type { ChatMessage } from "../types";
import { arrange } from "./layout";

function m(id: number, role: string, kind: string, extra: Partial<ChatMessage> = {}) {
  return { id, role, kind, content: "", status: "complete", ...extra } as ChatMessage;
}

describe("arrange", () => {
  it("hides the looking-now bubble and attaches the card to its answer", () => {
    const rows = arrange([
      m(1, "user", "say"),
      m(2, "assistant", "say"),
      m(3, "assistant", "report", { agent_task_id: "t" }),
      m(4, "assistant", "say", { agent_task_id: "t" }),
    ]);
    expect(rows.map((r) => r.message.id)).toEqual([1, 4]);
    expect(rows[1]?.report?.id).toBe(3);
  });

  it("keeps a card out of its answer while the answer is still being written", () => {
    const rows = arrange([
      m(1, "user", "say"),
      m(2, "assistant", "say"),
      m(3, "assistant", "report", { agent_task_id: "t" }),
      m(4, "assistant", "say", { agent_task_id: "t", status: "running" }),
    ]);
    expect(rows.map((r) => r.message.id)).toEqual([1, 3, 4]);
    expect(rows.some((r) => r.report)).toBe(false);
  });

  it("attaches the card to a closing line the hub wrote (no task on it)", () => {
    const rows = arrange([
      m(1, "user", "say"),
      m(3, "assistant", "report", { agent_task_id: "t" }),
      m(4, "assistant", "say"),
    ]);
    expect(rows.map((r) => r.message.id)).toEqual([1, 4]);
    expect(rows[1]?.report?.id).toBe(3);
  });

  it("leaves plain conversations alone", () => {
    const list = [m(1, "user", "say"), m(2, "assistant", "say")];
    expect(arrange(list).map((r) => r.message.id)).toEqual([1, 2]);
  });
});
