/**
 * Minimal Server-Sent Events support on top of fetch + ReadableStream.
 * EventSource cannot send an Authorization header, so we parse the stream
 * ourselves (subset of the WHATWG spec: event, data, id; comments ignored).
 */

export interface SseEvent {
  event: string;
  data: string;
  id?: string;
}

export class SseParser {
  private buffer = "";
  private eventName = "";
  private dataLines: string[] = [];
  private lastId: string | undefined;

  /** Feed a decoded text chunk; returns all events completed by it. */
  feed(chunk: string): SseEvent[] {
    this.buffer += chunk;
    const out: SseEvent[] = [];
    for (;;) {
      const cr = this.buffer.indexOf("\r");
      const lf = this.buffer.indexOf("\n");
      let end: number;
      if (cr === -1 && lf === -1) break;
      if (cr === -1) end = lf;
      else if (lf === -1) end = cr;
      else end = Math.min(cr, lf);

      let next = end + 1;
      if (this.buffer[end] === "\r") {
        // A trailing CR may be the first half of CRLF split across chunks.
        if (end === this.buffer.length - 1) break;
        if (this.buffer[end + 1] === "\n") next = end + 2;
      }
      const line = this.buffer.slice(0, end);
      this.buffer = this.buffer.slice(next);
      const ev = this.processLine(line);
      if (ev) out.push(ev);
    }
    return out;
  }

  private processLine(line: string): SseEvent | null {
    if (line === "") return this.dispatch();
    if (line.startsWith(":")) return null; // comment / keepalive
    const colon = line.indexOf(":");
    let field: string;
    let value: string;
    if (colon === -1) {
      field = line;
      value = "";
    } else {
      field = line.slice(0, colon);
      value = line.slice(colon + 1);
      if (value.startsWith(" ")) value = value.slice(1);
    }
    switch (field) {
      case "event":
        this.eventName = value;
        break;
      case "data":
        this.dataLines.push(value);
        break;
      case "id":
        if (!value.includes("\0")) this.lastId = value;
        break;
      default:
        break; // "retry" and unknown fields are ignored
    }
    return null;
  }

  private dispatch(): SseEvent | null {
    const name = this.eventName || "message";
    const hasData = this.dataLines.length > 0;
    const data = this.dataLines.join("\n");
    this.eventName = "";
    this.dataLines = [];
    if (!hasData) return null;
    const ev: SseEvent = { event: name, data };
    if (this.lastId !== undefined) ev.id = this.lastId;
    return ev;
  }
}

/** `lost`: the stream broke or went quiet; it reconnects on its own and turns `live` again. */
export type StreamStatus = "connecting" | "live" | "lost";

/** How long the stream may stay without a byte before the hub counts as lost. The hub sends a
 *  keepalive comment every 2 s (`sse_keepalive_seconds`), so two missed ones are tolerated. */
export const LOST_AFTER_MS = 5000;

export interface StreamOptions {
  url: string;
  token: string;
  onEvent: (ev: SseEvent) => void;
  /** Called every time a connection is (re)established. */
  onOpen?: () => void;
  onStatus?: (status: StreamStatus) => void;
  /** Called on HTTP 401; the stream then stops for good. */
  onUnauthorized?: () => void;
  fetchImpl?: typeof fetch;
  initialBackoffMs?: number;
  maxBackoffMs?: number;
  /** Reconnect (and report `lost`) when no bytes, not even keepalive comments, arrive for this
   *  long; a connection attempt that hangs this long counts the same. Default LOST_AFTER_MS. */
  idleTimeoutMs?: number;
  /** Injectable for tests. */
  sleep?: (ms: number, signal: AbortSignal) => Promise<void>;
}

export function backoffDelay(attempt: number, initialMs: number, maxMs: number): number {
  return Math.min(maxMs, initialMs * 2 ** attempt);
}

function defaultSleep(ms: number, signal: AbortSignal): Promise<void> {
  return new Promise((resolve) => {
    if (signal.aborted) return resolve();
    const timer = setTimeout(done, ms);
    function done() {
      clearTimeout(timer);
      signal.removeEventListener("abort", done);
      resolve();
    }
    signal.addEventListener("abort", done);
  });
}

/**
 * Connect to an SSE endpoint with an Authorization header and keep the
 * connection alive with exponential backoff. Returns a function that stops it.
 */
export function connectStream(opts: StreamOptions): () => void {
  const stop = new AbortController();
  const initial = opts.initialBackoffMs ?? 1000;
  const max = opts.maxBackoffMs ?? 30000;
  const idleMs = opts.idleTimeoutMs ?? LOST_AFTER_MS;
  const sleep = opts.sleep ?? defaultSleep;

  // report a status only when it changes: every failed attempt while the hub is away is one `lost`
  let last: StreamStatus | null = null;
  const report = (status: StreamStatus) => {
    if (status === last) return;
    last = status;
    opts.onStatus?.(status);
  };

  const run = async () => {
    let attempt = 0;
    while (!stop.signal.aborted) {
      const conn = new AbortController();
      const abortConn = () => conn.abort();
      stop.signal.addEventListener("abort", abortConn);
      let idleTimer: ReturnType<typeof setTimeout> | undefined;
      const armIdle = () => {
        clearTimeout(idleTimer);
        idleTimer = setTimeout(abortConn, idleMs);
      };
      try {
        const doFetch = opts.fetchImpl ?? globalThis.fetch;
        armIdle();
        const res = await doFetch(opts.url, {
          headers: {
            Authorization: `Bearer ${opts.token}`,
            Accept: "text/event-stream",
          },
          cache: "no-store",
          signal: conn.signal,
        });
        if (res.status === 401) {
          opts.onUnauthorized?.();
          return;
        }
        if (!res.ok || !res.body) throw new Error(`stream HTTP ${res.status}`);
        attempt = 0;
        report("live");
        opts.onOpen?.();
        const reader = res.body.getReader();
        const decoder = new TextDecoder();
        const parser = new SseParser();
        for (;;) {
          armIdle();
          const { done, value } = await reader.read();
          if (done) break;
          for (const ev of parser.feed(decoder.decode(value, { stream: true }))) {
            opts.onEvent(ev);
          }
        }
      } catch {
        // Network error, idle timeout or abort: fall through to reconnect.
      } finally {
        clearTimeout(idleTimer);
        stop.signal.removeEventListener("abort", abortConn);
      }
      if (stop.signal.aborted) return;
      report("lost");
      await sleep(backoffDelay(attempt, initial, max), stop.signal);
      attempt += 1;
    }
  };
  void run();
  return () => stop.abort();
}
