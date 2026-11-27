import { describe, expect, it } from "vitest";
import { VoiceError, WsVoiceLink, type SocketLike } from "./link";

class FakeSocket implements SocketLike {
  binaryType: BinaryType = "blob";
  readyState = 0;
  onopen: ((ev: Event) => void) | null = null;
  onmessage: ((ev: MessageEvent) => void) | null = null;
  onclose: ((ev: CloseEvent) => void) | null = null;
  onerror: ((ev: Event) => void) | null = null;
  sent: (string | ArrayBuffer)[] = [];
  constructor(readonly url: string) {}
  send(data: string | ArrayBuffer) {
    this.sent.push(data);
  }
  close() {
    this.readyState = 3;
  }
  // the hub's side
  opened() {
    this.readyState = 1;
    this.onopen?.(new Event("open"));
  }
  hub(message: object | ArrayBuffer) {
    const data = message instanceof ArrayBuffer ? message : JSON.stringify(message);
    this.onmessage?.(new MessageEvent("message", { data }));
  }
  json() {
    return this.sent.filter((f) => typeof f === "string").map((f) => JSON.parse(f as string));
  }
}

function linked() {
  const sockets: FakeSocket[] = [];
  const link = new WsVoiceLink("wss://hub.example.lan/api/voice/ws", "tok", (url) => {
    const s = new FakeSocket(url);
    sockets.push(s);
    return s;
  });
  return { link, sockets };
}

describe("WsVoiceLink", () => {
  it("signs in with the first frame and holds the rest until the hub is ready", async () => {
    const { link, sockets } = linked();
    const rec = link.listen({ rate: 16000, language: "de" });
    rec.send(new ArrayBuffer(8));
    const s = sockets[0]!;
    expect(s.binaryType).toBe("arraybuffer");
    s.opened();
    expect(s.json()).toEqual([{ type: "auth", token: "tok" }]);
    s.hub({ type: "ready", transcribe: true, speak: true });
    expect(s.json()[1]).toEqual({ type: "listen", id: rec.id, rate: 16000, language: "de", incognito: false });
    expect(s.sent[2]).toBeInstanceOf(ArrayBuffer);
    const text = rec.end();
    expect(s.json().at(-1)).toEqual({ type: "end", id: rec.id });
    s.hub({ type: "transcript", id: rec.id, text: "hallo", language: "de" });
    await expect(text).resolves.toEqual({ text: "hallo", language: "de" });
  });

  it("gives each WAV to its speech in order and settles when it is spoken", async () => {
    const { link, sockets } = linked();
    const got: [number, number][] = [];
    const speech = link.speak("Eins. Zwei.", { conversationId: "c1" }, (wav, seq) =>
      got.push([seq, wav.byteLength]),
    );
    const s = sockets[0]!;
    s.opened();
    s.hub({ type: "ready", transcribe: true, speak: true });
    expect(s.json()[1]).toMatchObject({ type: "speak", text: "Eins. Zwei.", conversation_id: "c1" });
    s.hub({ type: "audio", id: speech.id, seq: 0, format: "wav", bytes: 3 });
    s.hub(new ArrayBuffer(3));
    s.hub({ type: "audio", id: speech.id, seq: 1, format: "wav", bytes: 5 });
    s.hub(new ArrayBuffer(5));
    s.hub({ type: "spoken", id: speech.id });
    await speech.done;
    expect(got).toEqual([
      [0, 3],
      [1, 5],
    ]);
  });

  it("fails what waits with the hub's error, and everything when the socket closes", async () => {
    const { link, sockets } = linked();
    const rec = link.listen({ rate: 16000 });
    const text = rec.end();
    const speech = link.speak("Hallo.", {}, () => undefined);
    const s = sockets[0]!;
    s.opened();
    s.hub({ type: "ready", transcribe: true, speak: true });
    s.hub({ type: "error", id: rec.id, code: "too_long", message: "too long" });
    await expect(text).rejects.toMatchObject({ code: "too_long" });
    s.onclose?.(new CloseEvent("close", { code: 4401 }));
    await expect(speech.done).rejects.toBeInstanceOf(VoiceError);
    await expect(speech.done).rejects.toMatchObject({ code: "unauthorized" });
    // the next request opens a new socket
    link.speak("Noch einmal.", {}, () => undefined);
    expect(sockets).toHaveLength(2);
  });

  it("cancel without an id stops everything on the hub", () => {
    const { link, sockets } = linked();
    link.speak("Hallo.", {}, () => undefined);
    link.cancelAll();
    const s = sockets[0]!;
    s.opened();
    s.hub({ type: "ready", transcribe: true, speak: true });
    expect(s.json().at(-1)).toEqual({ type: "cancel" });
  });
});
