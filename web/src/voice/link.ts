/**
 * The voice link to the hub (D127, docs/HUB-API.md "Voice"): one WebSocket over the person's TLS
 * connection. Audio goes up in chunks while the key is held, the transcript comes back; answer text
 * goes up sentence by sentence, one WAV per sentence comes back. Built for streaming (#60): the
 * recording is sent while it is made, speech is asked for while the answer is still written, and
 * everything can be cancelled at once (barge-in).
 */

export interface Transcript {
  text: string;
  language: string;
}

export class VoiceError extends Error {
  constructor(
    readonly code: string,
    message: string,
  ) {
    super(message);
  }
}

/** One recording: chunks go up while the key is held, `end` asks for its text. */
export interface Recording {
  readonly id: string;
  send(chunk: ArrayBuffer): void;
  end(): Promise<Transcript>;
  cancel(): void;
}

/** One text being spoken: `onAudio` gets each sentence's WAV in order; `done` settles at the end
 *  (or when it was cancelled). */
export interface Speech {
  readonly id: string;
  readonly done: Promise<void>;
  cancel(): void;
}

export interface SpeakOptions {
  conversationId?: string;
  incognito?: boolean;
}

export interface VoiceLink {
  listen(opts: { rate: number; language?: string; incognito?: boolean }): Recording;
  speak(text: string, opts: SpeakOptions, onAudio: (wav: ArrayBuffer, seq: number) => void): Speech;
  /** Stop every recording and everything that waits to be spoken (the person speaks again). */
  cancelAll(): void;
  close(): void;
}

/** What the link needs of a WebSocket (a fake in tests). */
export interface SocketLike {
  binaryType: BinaryType;
  readonly readyState: number;
  onopen: ((ev: Event) => void) | null;
  onmessage: ((ev: MessageEvent) => void) | null;
  onclose: ((ev: CloseEvent) => void) | null;
  onerror: ((ev: Event) => void) | null;
  send(data: string | ArrayBuffer): void;
  close(code?: number, reason?: string): void;
}

export type SocketFactory = (url: string) => SocketLike;

interface Waiting {
  resolve: (t: Transcript) => void;
  reject: (e: VoiceError) => void;
}

interface Speaking {
  onAudio: (wav: ArrayBuffer, seq: number) => void;
  resolve: () => void;
  reject: (e: VoiceError) => void;
}

type HubMessage =
  | { type: "ready"; transcribe: boolean; speak: boolean }
  | { type: "transcript"; id: string; text: string; language: string }
  | { type: "audio"; id: string; seq: number; format: string; bytes: number }
  | { type: "spoken"; id: string }
  | { type: "cancelled"; id: string }
  | { type: "error"; id: string | null; code: string; message: string }
  | { type: "pong" };

/** The hub's close code for a missing or wrong token; a normal close. */
const CLOSE_UNAUTHORIZED = 4401;
const CLOSE_NORMAL = 1000;

let counter = 0;
const nextId = (kind: string) => `${kind}${++counter}`;

export class WsVoiceLink implements VoiceLink {
  private socket: SocketLike | null = null;
  private ready = false;
  private outbox: (string | ArrayBuffer)[] = [];
  private transcripts = new Map<string, Waiting>();
  private speeches = new Map<string, Speaking>();
  /** the audio message whose WAV is the next binary frame */
  private expecting: { id: string; seq: number } | null = null;

  constructor(
    private readonly url: string,
    private readonly token: string,
    private readonly open: SocketFactory = (u) => new WebSocket(u),
  ) {}

  private connect(): void {
    if (this.socket) return;
    const ws = this.open(this.url);
    ws.binaryType = "arraybuffer";
    this.socket = ws;
    this.ready = false;
    ws.onopen = () => ws.send(JSON.stringify({ type: "auth", token: this.token }));
    ws.onmessage = (ev) => this.receive(ev.data);
    ws.onerror = () => undefined; // onclose follows and says what is lost
    ws.onclose = (ev) => {
      if (this.socket !== ws) return;
      this.socket = null;
      this.ready = false;
      this.outbox = [];
      this.expecting = null;
      const err =
        ev.code === CLOSE_UNAUTHORIZED
          ? new VoiceError("unauthorized", "the hub did not accept the sign-in")
          : new VoiceError("closed", "the connection to the hub ended");
      this.transcripts.forEach((w) => w.reject(err));
      this.speeches.forEach((s) => s.reject(err));
      this.transcripts.clear();
      this.speeches.clear();
    };
  }

  private post(message: object | ArrayBuffer): void {
    this.connect();
    const frame = message instanceof ArrayBuffer ? message : JSON.stringify(message);
    if (this.ready && this.socket) this.socket.send(frame);
    else this.outbox.push(frame);
  }

  private receive(data: unknown): void {
    if (data instanceof ArrayBuffer) {
      const at = this.expecting;
      this.expecting = null;
      if (at) this.speeches.get(at.id)?.onAudio(data, at.seq);
      return;
    }
    let msg: HubMessage;
    try {
      msg = JSON.parse(String(data)) as HubMessage;
    } catch {
      return;
    }
    this.handlers[msg.type]?.(msg as never);
  }

  /** What each message of the hub does here. */
  private readonly handlers: { [K in HubMessage["type"]]?: (m: Extract<HubMessage, { type: K }>) => void } = {
    ready: () => {
      this.ready = true;
      const queued = this.outbox;
      this.outbox = [];
      queued.forEach((frame) => this.socket?.send(frame));
    },
    transcript: (m) => {
      const w = this.transcripts.get(m.id);
      this.transcripts.delete(m.id);
      w?.resolve({ text: m.text, language: m.language });
    },
    audio: (m) => {
      this.expecting = { id: m.id, seq: m.seq };
    },
    spoken: (m) => this.settle(m.id),
    cancelled: (m) => this.settle(m.id),
    error: (m) => {
      if (!m.id) return;
      const err = new VoiceError(m.code, m.message);
      this.transcripts.get(m.id)?.reject(err);
      this.transcripts.delete(m.id);
      this.speeches.get(m.id)?.reject(err);
      this.speeches.delete(m.id);
    },
  };

  /** A speech that ended (spoken or cancelled), or a recording that was cancelled. */
  private settle(id: string): void {
    const s = this.speeches.get(id);
    this.speeches.delete(id);
    s?.resolve();
    const w = this.transcripts.get(id);
    this.transcripts.delete(id);
    w?.reject(new VoiceError("cancelled", "the recording was cancelled"));
  }

  listen(opts: { rate: number; language?: string; incognito?: boolean }): Recording {
    const id = nextId("u");
    this.post({
      type: "listen",
      id,
      rate: opts.rate,
      language: opts.language ?? "auto",
      incognito: opts.incognito === true,
    });
    let ended = false;
    return {
      id,
      send: (chunk) => {
        if (!ended) this.post(chunk);
      },
      end: () => {
        ended = true;
        return new Promise<Transcript>((resolve, reject) => {
          this.transcripts.set(id, { resolve, reject });
          this.post({ type: "end", id });
        });
      },
      cancel: () => {
        ended = true;
        this.post({ type: "cancel", id });
      },
    };
  }

  speak(text: string, opts: SpeakOptions, onAudio: (wav: ArrayBuffer, seq: number) => void): Speech {
    const id = nextId("s");
    const done = new Promise<void>((resolve, reject) => {
      this.speeches.set(id, { onAudio, resolve, reject });
    });
    this.post({
      type: "speak",
      id,
      text,
      ...(opts.conversationId ? { conversation_id: opts.conversationId } : {}),
      incognito: opts.incognito === true,
    });
    return { id, done, cancel: () => this.post({ type: "cancel", id }) };
  }

  cancelAll(): void {
    if (!this.socket) return;
    this.post({ type: "cancel" });
  }

  close(): void {
    const ws = this.socket;
    if (!ws) return;
    ws.close(CLOSE_NORMAL, "done");
    ws.onclose?.(new CloseEvent("close", { code: CLOSE_NORMAL }));
  }
}
