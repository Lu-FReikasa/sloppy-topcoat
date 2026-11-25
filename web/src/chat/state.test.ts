// @vitest-environment node
import { describe, expect, it } from "vitest";
import type { ChatMessage, ConversationDetail } from "../types";
import { chatReducer, initialChatState, localMessage, type ChatState } from "./state";

const detail: ConversationDetail = {
  id: "c1",
  title: "Hello",
  model: null,
  node: null,
  created_at: "2026-01-01T00:00:00Z",
  updated_at: "2026-01-01T00:00:00Z",
  messages: [],
};

const answer: ChatMessage = {
  ...localMessage("assistant", "Hi there"),
  id: 2,
  model: "qwen-7b",
  node: "node-amd",
};

function withDetail(): ChatState {
  return chatReducer(initialChatState, { type: "detail_loaded", detail });
}

const meta = {
  type: "meta" as const,
  conversation_id: "c1",
  user_message: { ...localMessage("user", "Hi"), id: 1 },
  continues: null,
  memory: [],
  model: "qwen-7b",
  node: "node-amd",
  fallback_from: null,
  fallback_state: null,
  context: { length: 4096, estimated_prompt_tokens: 10, trimmed: 0 },
};

describe("chatReducer", () => {
  it("replaces the temporary user message with the stored one and streams the answer", () => {
    let s = withDetail();
    s = chatReducer(s, { type: "stream_start", id: "c1", userMessage: localMessage("user", "Hi") });
    expect(s.details.c1?.messages[0]?.id).toBeLessThan(0);
    s = chatReducer(s, { type: "stream_event", id: "c1", event: meta });
    expect(s.details.c1?.messages.map((m) => m.id)).toEqual([1]);
    s = chatReducer(s, { type: "stream_event", id: "c1", event: { type: "delta", text: "Hi " } });
    s = chatReducer(s, { type: "stream_event", id: "c1", event: { type: "delta", text: "there" } });
    expect(s.streams.c1).toMatchObject({ running: true, text: "Hi there" });
    s = chatReducer(s, {
      type: "stream_event",
      id: "c1",
      event: { type: "done", message: answer },
    });
    expect(s.streams.c1).toBeUndefined();
    expect(s.details.c1?.messages.map((m) => m.role)).toEqual(["user", "assistant"]);
  });

  it("keeps several conversations streaming independently", () => {
    let s = withDetail();
    s = chatReducer(s, { type: "detail_loaded", detail: { ...detail, id: "c2" } });
    s = chatReducer(s, { type: "stream_start", id: "c1", userMessage: null });
    s = chatReducer(s, { type: "stream_start", id: "c2", userMessage: null });
    s = chatReducer(s, { type: "stream_event", id: "c1", event: { type: "delta", text: "A" } });
    s = chatReducer(s, { type: "stream_event", id: "c2", event: { type: "delta", text: "B" } });
    expect([s.streams.c1?.text, s.streams.c2?.text]).toEqual(["A", "B"]);
  });

  it("does not overwrite a running conversation with a server copy", () => {
    let s = withDetail();
    s = chatReducer(s, { type: "stream_start", id: "c1", userMessage: localMessage("user", "x") });
    const after = chatReducer(s, { type: "detail_loaded", detail });
    expect(after).toBe(s);
  });

  it("keeps the partial text as a stopped answer", () => {
    let s = withDetail();
    s = chatReducer(s, { type: "stream_start", id: "c1", userMessage: null });
    s = chatReducer(s, { type: "stream_event", id: "c1", event: meta });
    s = chatReducer(s, { type: "stream_event", id: "c1", event: { type: "delta", text: "Par" } });
    s = chatReducer(s, { type: "stream_stopped", id: "c1" });
    const last = s.details.c1?.messages.at(-1);
    expect(last).toMatchObject({
      role: "assistant",
      content: "Par",
      status: "stopped",
      node: "node-amd",
    });
    expect(s.streams.c1).toBeUndefined();
  });

  it("adds nothing when stopped before the first token", () => {
    let s = withDetail();
    s = chatReducer(s, { type: "stream_start", id: "c1", userMessage: null });
    s = chatReducer(s, { type: "stream_stopped", id: "c1" });
    expect(s.details.c1?.messages).toEqual([]);
  });

  it("shows an error event and keeps the stored error row", () => {
    let s = withDetail();
    s = chatReducer(s, { type: "stream_start", id: "c1", userMessage: null });
    const row = { ...answer, status: "error" as const, error: "boom" };
    s = chatReducer(s, {
      type: "stream_event",
      id: "c1",
      event: { type: "error", code: "upstream", message: "boom", message_row: row },
    });
    expect(s.streams.c1).toMatchObject({
      running: false,
      error: { code: "upstream", message: "boom" },
    });
    expect(s.details.c1?.messages.at(-1)?.status).toBe("error");
  });

  it("drops only a trailing answer when regenerating", () => {
    let s = withDetail();
    s = chatReducer(s, {
      type: "stream_start",
      id: "c1",
      userMessage: { ...answer, id: 1, role: "user" },
    });
    s = chatReducer(s, { type: "drop_last_answer", id: "c1" });
    expect(s.details.c1?.messages).toHaveLength(1);
    s = chatReducer(s, {
      type: "stream_event",
      id: "c1",
      event: { type: "done", message: answer },
    });
    s = chatReducer(s, { type: "drop_last_answer", id: "c1" });
    expect(s.details.c1?.messages).toHaveLength(1);
  });

  it("removes a conversation with its cached state", () => {
    let s = withDetail();
    s = chatReducer(s, { type: "conversation_upserted", conversation: detail });
    s = chatReducer(s, { type: "conversation_removed", id: "c1" });
    expect(s.conversations).toEqual([]);
    expect(s.details.c1).toBeUndefined();
  });

  it("appends a continued answer to the same message, live and when stopped", () => {
    const cut: ChatMessage = { ...answer, status: "truncated", content: "Once upon" };
    let s = chatReducer(initialChatState, {
      type: "detail_loaded",
      detail: { ...detail, messages: [cut] },
    });
    s = chatReducer(s, { type: "stream_start", id: "c1", userMessage: null, continues: 2 });
    expect(s.streams.c1).toMatchObject({ running: true, continues: 2 });
    const stopped = chatReducer(
      chatReducer(s, { type: "stream_event", id: "c1", event: { type: "delta", text: " a time" } }),
      { type: "stream_stopped", id: "c1" },
    );
    expect(stopped.details.c1?.messages).toHaveLength(1);
    expect(stopped.details.c1?.messages[0]).toMatchObject({
      content: "Once upon a time",
      status: "stopped",
    });
    const merged = { ...cut, content: "Once upon a time.", status: "complete" as const };
    const done = chatReducer(s, {
      type: "stream_event",
      id: "c1",
      event: { type: "done", message: merged },
    });
    expect(done.details.c1?.messages).toEqual([merged]); // replaced, not appended
    expect(done.streams.c1).toBeUndefined();
  });

  it("turns corrections and withdrawals of a memory event into note lines", () => {
    let s = withDetail();
    s = chatReducer(s, {
      type: "stream_event",
      id: "c1",
      event: {
        type: "memory",
        message_id: 2,
        remembered: [],
        confirmed: [],
        corrected: [
          {
            id: "n9",
            text: "Lives in Musterstadt",
            evidence: "doc",
            replaced: [{ id: "n1", text: "Lives in Hamburg" }],
            withdrawn: true,
          },
        ],
        retracted: [{ id: "n3", text: "Lives in Kiel" }],
        blocked: 0,
        questions: 0,
      },
    });
    const items = s.notes.c1?.[2]?.items ?? [];
    expect(items.map((n) => [n.id, n.kind, n.isNew])).toEqual([
      ["n9", "corrected", true],
      ["n3", "retracted", false],
    ]);
    expect(items[0]?.replaced).toEqual([{ id: "n1", text: "Lives in Hamburg" }]);
    expect(items[0]?.withdrawn).toBe(true);
  });

  it("keeps the notes written for an answer, also after the stream is gone, and drops undone ones", () => {
    let s = withDetail();
    s = chatReducer(s, { type: "stream_start", id: "c1", userMessage: null });
    s = chatReducer(s, {
      type: "stream_event",
      id: "c1",
      event: { type: "done", message: answer },
    });
    expect(s.streams.c1).toBeUndefined();
    s = chatReducer(s, {
      type: "stream_event",
      id: "c1",
      event: {
        type: "memory",
        message_id: 2,
        remembered: [{ id: "n1", text: "Lives in Hamburg", evidence: "doc" }],
        confirmed: [{ id: "n2", text: "Is a carpenter", evidence: "doc" }],
        blocked: 1,
      },
    });
    const notes = s.notes.c1?.[2];
    expect(notes?.items.map((n) => [n.id, n.isNew])).toEqual([
      ["n1", true],
      ["n2", false],
    ]);
    expect(notes?.blocked).toBe(1);
    // a reload of the conversation brings the stored notes; what only this session knows stays
    s = chatReducer(s, {
      type: "notes_loaded",
      id: "c1",
      items: [
        {
          id: "n1",
          text: "Lives in Hamburg",
          status: "active",
          evidence: "doc",
          confidence: 0.9,
          source_type: "chat",
          source_ref: { message_id: 2 },
          version: 1,
          confirmed_count: 0,
          last_confirmed_at: null,
          disputed_with: null,
          superseded_by: null,
          embedder: null,
          created_at: null,
          updated_at: null,
        },
      ],
    });
    expect(s.notes.c1?.[2]?.items.map((n) => n.id)).toEqual(["n1", "n2"]);
    s = chatReducer(s, { type: "note_removed", id: "c1", noteId: "n1" });
    expect(s.notes.c1?.[2]?.items.map((n) => n.id)).toEqual(["n2"]);
  });

  it("shows that the node paused an answer and clears it when it goes on", () => {
    let s = chatReducer(withDetail(), { type: "stream_start", id: "c1", userMessage: null });
    s = chatReducer(s, { type: "stream_event", id: "c1", event: { type: "delta", text: "Hi " } });
    s = chatReducer(s, {
      type: "stream_event",
      id: "c1",
      event: { type: "status", state: "paused", reasons: ["input"], node: "n" },
    });
    expect(s.streams.c1?.pause).toEqual(["input"]);
    expect(s.streams.c1?.text).toBe("Hi ");
    s = chatReducer(s, {
      type: "stream_event",
      id: "c1",
      event: { type: "status", state: "running", node: "n" },
    });
    expect(s.streams.c1?.pause).toBeUndefined();
  });
});
