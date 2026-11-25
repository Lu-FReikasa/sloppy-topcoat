import { describe, expect, it } from "vitest";
import type { ChatMessage, MemoryRef } from "../types";
import { evidenceOf, hasMark } from "./evidence";

function answer(extra: Partial<ChatMessage> = {}): ChatMessage {
  return {
    id: 2,
    role: "assistant",
    content: "x",
    model: "gemma",
    node: "node-amd",
    fallback_from: null,
    status: "complete",
    error: null,
    prompt_tokens: null,
    completion_tokens: null,
    first_token_ms: null,
    duration_ms: null,
    context_trimmed: 0,
    created_at: null,
    ...extra,
  };
}
const ref = (extra: Partial<MemoryRef> = {}): MemoryRef => ({
  id: "n1",
  text: "Laptop stays on",
  evidence: "doc",
  status: "active",
  ...extra,
});

describe("evidenceOf", () => {
  it("shows no mark for an answer without a light and without a stand-in node", () => {
    const e = evidenceOf(answer());
    expect(e.refs).toEqual([]);
    expect(hasMark(e, null)).toBe(false);
  });

  it("shows a mark as soon as the answer has a traffic light", () => {
    const e = evidenceOf(answer());
    for (const light of ["waiting", "ok", "hint", "error"] as const)
      expect(hasMark(e, light)).toBe(true);
  });

  it("lists the notes the model saw, without the deleted ones", () => {
    const e = evidenceOf(
      answer({ memory_refs: [ref(), ref({ id: "n2", status: "deleted" })] }),
    );
    expect(e.refs.map((r) => r.id)).toEqual(["n1"]);
  });

  it("lists what the older per-answer check found wrong", () => {
    const e = evidenceOf(
      answer({
        verification: {
          ok: false,
          issues: ["The node name is wrong"],
          model: "big",
        },
      }),
    );
    expect(e.issues).toEqual(["The node name is wrong"]);
  });

  it("always names the node when another one than expected answered", () => {
    const e = evidenceOf(
      answer({ node: "node-laptop", fallback_from: "node-amd" }),
    );
    expect(e.via).toBe("node-laptop");
    expect(e.viaFrom).toBe("node-amd");
    expect(hasMark(e, null)).toBe(true);
  });
});
