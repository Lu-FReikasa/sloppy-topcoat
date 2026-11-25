// @vitest-environment node
import { describe, expect, it, vi } from "vitest";
import { backoffDelay, connectStream, LOST_AFTER_MS, SseParser, type SseEvent } from "./sse";

describe("SseParser", () => {
  it("parses a single event", () => {
    const p = new SseParser();
    expect(p.feed('event: node_updated\ndata: {"id":"n1"}\n\n')).toEqual([
      { event: "node_updated", data: '{"id":"n1"}' },
    ]);
  });

  it("handles chunk boundaries in the middle of lines and fields", () => {
    const p = new SseParser();
    const input = 'event: node_updated\ndata: {"id":"n1","name":"x"}\n\n';
    const out: SseEvent[] = [];
    for (let i = 0; i < input.length; i += 3) out.push(...p.feed(input.slice(i, i + 3)));
    expect(out).toEqual([{ event: "node_updated", data: '{"id":"n1","name":"x"}' }]);
  });

  it("returns nothing until the blank line arrives", () => {
    const p = new SseParser();
    expect(p.feed("event: node_event\ndata: {}")).toEqual([]);
    expect(p.feed("\n")).toEqual([]);
    expect(p.feed("\n")).toEqual([{ event: "node_event", data: "{}" }]);
  });

  it("parses multiple events in one chunk", () => {
    const p = new SseParser();
    const out = p.feed("event: a\ndata: 1\n\nevent: b\ndata: 2\n\ndata: 3\n\n");
    expect(out).toEqual([
      { event: "a", data: "1" },
      { event: "b", data: "2" },
      { event: "message", data: "3" },
    ]);
  });

  it("ignores comment lines used as keepalive", () => {
    const p = new SseParser();
    expect(p.feed(": keepalive\n\n")).toEqual([]);
    expect(p.feed(":ping\nevent: a\n: inner comment\ndata: x\n\n")).toEqual([
      { event: "a", data: "x" },
    ]);
  });

  it("joins multi-line data and supports CRLF split across chunks", () => {
    const p = new SseParser();
    const out = [...p.feed("event: a\r"), ...p.feed("\ndata: line1\r\ndata: line2\r\n\r"), ...p.feed("\n")];
    expect(out).toEqual([{ event: "a", data: "line1\nline2" }]);
  });

  it("does not reuse the event name for the next event", () => {
    const p = new SseParser();
    const out = p.feed("event: a\ndata: 1\n\ndata: 2\n\n");
    expect(out[1]).toEqual({ event: "message", data: "2" });
  });

  it("strips only one leading space from values", () => {
    const p = new SseParser();
    expect(p.feed("data:  two\n\n")).toEqual([{ event: "message", data: " two" }]);
  });
});

function streamResponse(chunks: string[], status = 200): Response {
  const enc = new TextEncoder();
  const body = new ReadableStream<Uint8Array>({
    start(controller) {
      for (const c of chunks) controller.enqueue(enc.encode(c));
      controller.close();
    },
  });
  return new Response(body, { status, headers: { "Content-Type": "text/event-stream" } });
}

describe("connectStream", () => {
  it("computes exponential backoff with a cap", () => {
    expect([0, 1, 2, 3, 10].map((a) => backoffDelay(a, 1000, 30000))).toEqual([
      1000, 2000, 4000, 8000, 30000,
    ]);
  });

  it("delivers events, reconnects with backoff and calls onOpen each time", async () => {
    const responses = [
      () => streamResponse(["event: node_updated\ndata: {\"id\":", "\"n1\"}\n\n: ka\n"]),
      () => Promise.reject(new TypeError("network down")),
      () => streamResponse(["event: node_updated\ndata: {\"id\":\"n2\"}\n\n"]),
    ];
    let call = 0;
    const fetchImpl = vi.fn((_url: string, _init?: RequestInit) => {
      const next = responses[call++];
      if (!next) return new Promise<Response>(() => undefined); // hang forever
      return Promise.resolve(next());
    });
    const delays: number[] = [];
    const events: SseEvent[] = [];
    const statuses: string[] = [];
    const onOpen = vi.fn();
    const stop = connectStream({
      url: "/api/stream",
      token: "tok",
      fetchImpl: fetchImpl as unknown as typeof fetch,
      onEvent: (e) => events.push(e),
      onOpen,
      onStatus: (s) => statuses.push(s),
      initialBackoffMs: 100,
      maxBackoffMs: 1000,
      sleep: async (ms) => {
        delays.push(ms);
      },
    });
    await vi.waitFor(() => expect(fetchImpl).toHaveBeenCalledTimes(4));
    stop();

    expect(events).toEqual([
      { event: "node_updated", data: '{"id":"n1"}' },
      { event: "node_updated", data: '{"id":"n2"}' },
    ]);
    expect(onOpen).toHaveBeenCalledTimes(2);
    // First close -> attempt 0; network error -> attempt 1; after the
    // successful reconnect the backoff resets to attempt 0.
    expect(delays).toEqual([100, 200, 100]);
    // one `lost` while it is away, however many attempts fail
    expect(statuses).toEqual(["live", "lost", "live", "lost"]);
    const init = fetchImpl.mock.calls[0]?.[1];
    expect(new Headers(init?.headers).get("authorization")).toBe("Bearer tok");
  });

  it("marks the hub lost within the set time when it stops answering, and live again", async () => {
    vi.useFakeTimers();
    try {
      const enc = new TextEncoder();
      // a stream that sends one keepalive and then nothing: the hub stopped answering, the
      // connection stays open (a proxy in between, a dead network); it ends only on abort
      const silent = (init?: RequestInit) =>
        new Response(
          new ReadableStream<Uint8Array>({
            start(controller) {
              controller.enqueue(enc.encode(": ping\n\n"));
              init?.signal?.addEventListener("abort", () => controller.error(new Error("aborted")));
            },
          }),
          { status: 200 },
        );
      const answers = [
        (init?: RequestInit) => Promise.resolve(silent(init)),
        () => Promise.reject(new TypeError("Failed to fetch")),
        () => Promise.reject(new TypeError("Failed to fetch")),
        (init?: RequestInit) => Promise.resolve(silent(init)),
      ];
      let call = 0;
      const fetchImpl = vi.fn((_url: string, init?: RequestInit) => {
        const next = answers[call++];
        return next ? next(init) : new Promise<Response>(() => undefined);
      });
      const statuses: string[] = [];
      const stop = connectStream({
        url: "/api/stream",
        token: "tok",
        fetchImpl: fetchImpl as unknown as typeof fetch,
        onEvent: () => undefined,
        onStatus: (s) => statuses.push(s),
        sleep: async () => undefined,
      });
      await vi.advanceTimersByTimeAsync(0);
      expect(statuses).toEqual(["live"]);
      await vi.advanceTimersByTimeAsync(LOST_AFTER_MS - 100);
      expect(statuses).toEqual(["live"]);
      expect(LOST_AFTER_MS).toBeLessThanOrEqual(5000);
      // the silence passes the set time: lost; the failed attempts that follow add nothing; the
      // fourth attempt answers again: live
      await vi.advanceTimersByTimeAsync(100);
      expect(statuses).toEqual(["live", "lost", "live"]);
      expect(fetchImpl).toHaveBeenCalledTimes(4);
      stop();
    } finally {
      vi.useRealTimers();
    }
  });

  it("stops on 401 and reports it", async () => {
    const fetchImpl = vi.fn(() => Promise.resolve(new Response("", { status: 401 })));
    const onUnauthorized = vi.fn();
    const sleep = vi.fn(async () => undefined);
    connectStream({
      url: "/api/stream",
      token: "bad",
      fetchImpl: fetchImpl as unknown as typeof fetch,
      onEvent: () => undefined,
      onUnauthorized,
      sleep,
    });
    await vi.waitFor(() => expect(onUnauthorized).toHaveBeenCalledTimes(1));
    expect(fetchImpl).toHaveBeenCalledTimes(1);
    expect(sleep).not.toHaveBeenCalled();
  });
});
