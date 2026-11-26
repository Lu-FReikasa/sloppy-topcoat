import { describe, expect, it } from "vitest";
import { PushToTalk } from "./controller";
import { FakeMicrophone, FakePlayer, FakeVoiceLink } from "./fake";
import { VoiceError } from "./link";

const flush = () => new Promise((r) => setTimeout(r, 0));

function talk(speak = true) {
  const link = new FakeVoiceLink();
  const mic = new FakeMicrophone();
  const player = new FakePlayer();
  const sent: string[] = [];
  const ptt = new PushToTalk({ link, mic, player }, { conversationId: "c1", speak }, (t) => sent.push(t));
  return { ptt, link, mic, player, sent };
}

describe("PushToTalk", () => {
  it("records while held and sends the transcript as the message", async () => {
    const { ptt, link, mic, sent } = talk();
    link.transcripts.push(" wie spät ist es ");
    ptt.press();
    expect(ptt.state.state).toBe("listening");
    await flush();
    expect(mic.on).toBe(true);
    await ptt.release();
    expect(mic.on).toBe(false);
    expect(link.received[0]).toBeGreaterThan(0);
    expect(sent).toEqual(["wie spät ist es"]);
    expect(ptt.state.state).toBe("thinking"); // waiting for the answer
  });

  it("reads the answer out sentence by sentence while it is written, then is idle", async () => {
    const { ptt, link, player } = talk();
    link.transcripts.push("Hallo");
    ptt.press();
    await ptt.release();
    ptt.answer("Guten Abend, schön dich zu hören. Wie", false);
    expect(link.spoken.map((s) => s.text)).toEqual(["Guten Abend, schön dich zu hören."]);
    expect(link.spoken[0]!.opts).toEqual({ conversationId: "c1", incognito: undefined });
    await flush();
    expect(ptt.state.state).toBe("speaking");
    ptt.answer("Guten Abend, schön dich zu hören. Wie geht es dir?", true);
    expect(link.spoken.map((s) => s.text)).toEqual([
      "Guten Abend, schön dich zu hören.",
      "Wie geht es dir?",
    ]);
    await flush();
    await flush();
    expect(player.played).toBe(2);
    expect(ptt.state.state).toBe("idle");
  });

  it("pressing again while it speaks stops the speech at once (half duplex)", async () => {
    const { ptt, link, player } = talk();
    link.transcripts.push("Erzähl was", "Stopp");
    link.hold = true;
    ptt.press();
    await ptt.release();
    ptt.answer("Es war einmal ein langer Satz. Und noch einer.", true);
    const before = player.stops;
    ptt.press();
    expect(player.stops).toBe(before + 1);
    expect(link.cancels).toBeGreaterThan(0);
    link.release(); // nothing of the old answer is played after the stop
    await flush();
    expect(player.played).toBe(0);
    expect(ptt.state.state).toBe("listening");
  });

  it("without text to speech it only sends and goes idle", async () => {
    const { ptt, link, sent } = talk(false);
    link.transcripts.push("Hallo");
    ptt.press();
    await ptt.release();
    expect(sent).toEqual(["Hallo"]);
    expect(ptt.state.state).toBe("idle");
    ptt.answer("Hallo zurück, wie geht es?", true);
    expect(link.spoken).toEqual([]);
  });

  it("says why when the microphone is refused, nothing was heard or the hub refuses", async () => {
    const a = talk();
    a.mic.denied = true;
    a.ptt.press();
    await a.ptt.release();
    expect(a.ptt.state).toEqual({ state: "idle", problem: "micDenied" });
    expect(a.sent).toEqual([]);

    const b = talk();
    b.ptt.press();
    await b.ptt.release();
    expect(b.ptt.state).toEqual({ state: "idle", problem: "nothingHeard" });

    const c = talk();
    c.link.failNext = new VoiceError("too_long", "too long");
    c.ptt.press();
    await c.ptt.release();
    expect(c.ptt.state).toEqual({ state: "idle", problem: "tooLong" });
  });

  it("a spoken yes is only a message: it is sent, never anything else", async () => {
    // D15/D83: the controller has one way out, `send`; approval cards stay click or key
    const { ptt, link, sent } = talk();
    link.transcripts.push("Ja, mach das");
    ptt.press();
    await ptt.release();
    expect(sent).toEqual(["Ja, mach das"]);
  });

  it("Esc (cancel) drops the recording without sending it", async () => {
    const { ptt, link, mic, sent } = talk();
    link.transcripts.push("Nicht senden");
    ptt.press();
    await flush();
    ptt.cancel();
    expect(mic.on).toBe(false);
    await ptt.release();
    expect(sent).toEqual([]);
    expect(ptt.state.state).toBe("idle");
  });
});
