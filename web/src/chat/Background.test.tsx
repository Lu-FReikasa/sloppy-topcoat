import {
  act,
  cleanup,
  fireEvent,
  render,
  screen,
} from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { ApiClient } from "../api/client";
import { mockFetch, type Handler } from "../test/mockFetch";
import { controlledSse, sseResponse } from "../test/sse";
import type { ChatMessage, Conversation, ConversationDetail } from "../types";
import { TalkPage } from "../talk/TalkPage";

function msg(
  id: number,
  role: "user" | "assistant",
  content: string,
  status: ChatMessage["status"] = "complete",
): ChatMessage {
  return {
    id,
    role,
    content,
    model: role === "assistant" ? "qwen-7b" : null,
    node: role === "assistant" ? "node-amd" : null,
    fallback_from: null,
    status,
    error: null,
    prompt_tokens: null,
    completion_tokens: null,
    first_token_ms: null,
    duration_ms: null,
    context_trimmed: 0,
    created_at: "2026-01-01T10:00:00Z",
  };
}

const CONV: Conversation = {
  id: "c1",
  title: "Hintergrund",
  model: null,
  node: null,
  running: true,
  created_at: "2026-01-01T10:00:00Z",
  updated_at: "2026-01-01T10:00:00Z",
  message_count: 2,
};
const META = {
  conversation_id: "c1",
  user_message: msg(1, "user", "Zähle bis drei"),
  continues: null,
  memory: [],
  message_id: 2,
  model: "qwen-7b",
  node: "node-amd",
  fallback_from: null,
  fallback_state: null,
  context: { length: 4096, estimated_prompt_tokens: 40, trimmed: 0 },
};
const SETTINGS = {
  user: {
    system_prompt: null,
    temperature: null,
    address: "du",
    memory_enabled: true,
    quality: "balanced",
    internet: true,
  },
  hub: {},
  effective_model: "qwen-7b",
};

function detail(messages: ChatMessage[], running: boolean): ConversationDetail {
  return { ...CONV, running, messages };
}

function setup(
  extra: Record<string, Handler | unknown>,
  conversationId: string | undefined = "c1",
) {
  const mock = mockFetch({
    "GET /api/chat/models": [],
    "GET /api/chat/settings": SETTINGS,
    "GET /api/chat/tools": { groups: [], nodes: [], internet: { node: null } },
    ...extra,
  });
  const api = new ApiClient({ token: "t", fetchImpl: mock.fetch });
  render(<TalkPage api={api} conversationId={conversationId} />);
  return mock;
}

beforeEach(() => {
  window.location.hash = "";
});
afterEach(() => {
  cleanup();
  vi.useRealTimers();
  vi.restoreAllMocks();
});

describe("an answer that runs in the hub", () => {
  it("is followed from its start when the user comes back to the conversation", async () => {
    const mock = setup({
      "GET /api/conversations": [CONV],
      "GET /api/conversations/c1": detail(
        [
          msg(1, "user", "Zähle bis drei"),
          msg(2, "assistant", "Eins", "running"),
        ],
        true,
      ),
      "GET /api/chat/messages/2/stream?offset=0": () =>
        sseResponse(
          [
            ["meta", META],
            ["delta", { text: "Eins, " }],
            ["delta", { text: "zwei, drei" }],
            ["done", { message: msg(2, "assistant", "Eins, zwei, drei") }],
          ],
          { firstId: 0 },
        ),
    });
    expect(await screen.findByText("Eins, zwei, drei")).toBeTruthy();
    expect(
      mock.calls.some((c) => c.url === "/api/chat/messages/2/stream?offset=0"),
    ).toBe(true);
    // the question is shown once, the answer once (the stored half answer is not a second bubble)
    expect(screen.getAllByText("Zähle bis drei")).toHaveLength(1);
    expect(screen.getAllByText("Eins, zwei, drei")).toHaveLength(1);
    expect(screen.queryByTestId("streaming")).toBeNull();
    expect(screen.queryByTestId("running-note")).toBeNull();
  });

  it("attaches again from where it got when the connection drops", async () => {
    const first = controlledSse();
    let second: string | null = null;
    setup({
      "GET /api/conversations": [CONV],
      "GET /api/conversations/c1": detail(
        [msg(1, "user", "Zähle bis drei"), msg(2, "assistant", "", "running")],
        true,
      ),
      "GET /api/chat/messages/2/stream?offset=0": () => first.response,
      "GET /api/chat/messages/2/stream?offset=3": () => {
        second = "offset=3";
        return sseResponse(
          [
            ["delta", { text: "zwei, drei" }],
            ["done", { message: msg(2, "assistant", "Eins, zwei, drei") }],
          ],
          { firstId: 3 },
        );
      },
    });
    first.push("meta", META, 0);
    first.push("delta", { text: "Eins, " }, 1);
    first.push("status", { state: "running", node: "node-amd" }, 2);
    expect(await screen.findByText("Eins,")).toBeTruthy();
    vi.useFakeTimers({ shouldAdvanceTime: true });
    first.fail(); // the connection is gone, the answer is not
    await act(async () => {
      await vi.advanceTimersByTimeAsync(1500);
    });
    vi.useRealTimers();
    expect(await screen.findByText("Eins, zwei, drei")).toBeTruthy();
    expect(second).toBe("offset=3"); // asked for what it had not seen, not for everything
  });

  it("offers to continue an answer the hub's restart interrupted", async () => {
    const mock = setup({
      "GET /api/conversations": [{ ...CONV, running: false }],
      "GET /api/conversations/c1": detail(
        [
          msg(1, "user", "Zähle bis drei"),
          msg(2, "assistant", "Eins, zw", "interrupted"),
        ],
        false,
      ),
      "POST /api/conversations/c1/continue": () =>
        sseResponse([
          ["meta", { ...META, continues: 2 }],
          ["delta", { text: "ei, drei" }],
          ["done", { message: msg(2, "assistant", "Eins, zwei, drei") }],
        ]),
    });
    // the calm view says it in one sentence and offers to go on
    fireEvent.click(await screen.findByRole("button", { name: "Fortsetzen" }));
    expect(await screen.findByText("Eins, zwei, drei")).toBeTruthy();
    expect(
      mock.calls.some((c) => c.url === "/api/conversations/c1/continue"),
    ).toBe(true);
  });
});
