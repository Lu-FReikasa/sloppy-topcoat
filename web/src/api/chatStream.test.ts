// @vitest-environment node
import { describe, expect, it } from "vitest";
import { sseResponse } from "../test/sse";
import { jsonResponse, mockFetch } from "../test/mockFetch";
import { ApiClient, ApiError, type ChatEvent } from "./client";

async function collect(gen: AsyncGenerator<ChatEvent>): Promise<ChatEvent[]> {
  const out: ChatEvent[] = [];
  for await (const e of gen) out.push(e);
  return out;
}

describe("ApiClient chat", () => {
  it("parses the event stream and posts the message with the token", async () => {
    const { fetch, calls } = mockFetch({
      "POST /api/conversations/c1/messages": () =>
        sseResponse([
          ["meta", { model: "m", node: "n" }],
          ["delta", { text: "Hel" }],
          ["delta", { text: "lo" }],
          ["done", { message: { id: 2 } }],
        ]),
    });
    const api = new ApiClient({ token: "tok", fetchImpl: fetch });
    const events = await collect(api.sendMessage("c1", "Hi", new AbortController().signal));
    expect(events.map((e) => e.type)).toEqual(["meta", "delta", "delta", "done"]);
    expect(calls[0]?.headers.authorization).toBe("Bearer tok");
    expect(calls[0]?.body).toEqual({ content: "Hi", tools: "auto" });
  });

  it("maps a 503 with retry_after to an ApiError with the hint", async () => {
    const { fetch } = mockFetch({
      "POST /api/conversations/c1/messages": () =>
        jsonResponse(
          { detail: { code: "model_unavailable", message: "no node", retry_after: 30 } },
          503,
        ),
    });
    const api = new ApiClient({ token: "t", fetchImpl: fetch });
    const err = await collect(api.sendMessage("c1", "Hi", new AbortController().signal)).catch(
      (e: unknown) => e,
    );
    expect(err).toBeInstanceOf(ApiError);
    expect(err).toMatchObject({ status: 503, code: "model_unavailable", retryAfter: 30 });
  });

  it("ends quietly when the stream is aborted", async () => {
    const ctrl = new AbortController();
    const { fetch } = mockFetch({
      "POST /api/conversations/c1/regenerate": (init?: RequestInit) =>
        sseResponse([["delta", { text: "x" }]], { hold: true, signal: init?.signal }),
    });
    const api = new ApiClient({ token: "t", fetchImpl: fetch });
    const seen: string[] = [];
    for await (const e of api.regenerate("c1", ctrl.signal)) {
      seen.push(e.type);
      ctrl.abort();
    }
    expect(seen).toEqual(["delta"]);
  });

  it("ignores events it does not know", async () => {
    const { fetch } = mockFetch({
      "POST /api/conversations/c1/regenerate": () =>
        sseResponse([
          ["ping", {}],
          ["delta", { text: "a" }],
        ]),
    });
    const api = new ApiClient({ token: "t", fetchImpl: fetch });
    const events = await collect(api.regenerate("c1", new AbortController().signal));
    expect(events).toEqual([{ type: "delta", text: "a" }]);
  });
});
