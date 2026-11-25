/** Test helpers: a hub-style SSE response that can stay open until the client aborts. */

export type SseEvent = [event: string, data: unknown];

export function sseText(events: SseEvent[], firstId?: number): string {
  return events
    .map(
      ([e, d], i) =>
        `${firstId === undefined ? "" : `id: ${firstId + i}\n`}event: ${e}\ndata: ${JSON.stringify(d)}\n\n`,
    )
    .join("");
}

/**
 * Streams `events`; with `hold` the stream then stays open until `signal` aborts (the
 * stop button), which errors it the way a real aborted fetch does.
 */
export function sseResponse(
  events: SseEvent[],
  opts: {
    hold?: boolean;
    signal?: AbortSignal | null;
    status?: number;
    /** Number the events like the hub does (E3-07): the first one gets this id. */
    firstId?: number;
  } = {},
): Response {
  const encoder = new TextEncoder();
  const stream = new ReadableStream<Uint8Array>({
    start(controller) {
      events.forEach(([e, d], i) =>
        controller.enqueue(
          encoder.encode(
            sseText([[e, d]], opts.firstId === undefined ? undefined : opts.firstId + i),
          ),
        ),
      );
      if (!opts.hold) {
        controller.close();
        return;
      }
      opts.signal?.addEventListener("abort", () =>
        controller.error(new DOMException("aborted", "AbortError")),
      );
    },
  });
  return new Response(stream, {
    status: opts.status ?? 200,
    headers: { "Content-Type": "text/event-stream" },
  });
}

/** A stream the test feeds by hand: `push` sends an event, `close` ends it (or `fail` breaks it,
 *  like a lost connection). */
export function controlledSse(): {
  response: Response;
  push: (event: string, data: unknown, id?: number) => void;
  close: () => void;
  fail: () => void;
} {
  const encoder = new TextEncoder();
  let ctrl!: ReadableStreamDefaultController<Uint8Array>;
  const stream = new ReadableStream<Uint8Array>({
    start(c) {
      ctrl = c;
    },
  });
  return {
    response: new Response(stream, {
      status: 200,
      headers: { "Content-Type": "text/event-stream" },
    }),
    push: (event, data, id) => ctrl.enqueue(encoder.encode(sseText([[event, data]], id))),
    close: () => ctrl.close(),
    fail: () => ctrl.error(new TypeError("network error")),
  };
}
