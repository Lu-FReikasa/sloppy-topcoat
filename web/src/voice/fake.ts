/** Fakes of the voice link, the microphone and the loudspeaker (tests and the fake hub). */

import { SPEECH_RATE, type Microphone, type Player } from "./audio";
import { VoiceError, type Recording, type Speech, type SpeakOptions, type Transcript, type VoiceLink } from "./link";

const FAKE_WAV = new TextEncoder().encode("RIFF").buffer;
/** 100 ms of 16 kHz 16-bit mono */
const FAKE_CHUNK_BYTES = SPEECH_RATE / 5;

export class FakeVoiceLink implements VoiceLink {
  /** what the next recordings are heard as, in order ("" = nothing heard) */
  transcripts: string[] = [];
  /** the error the next `end` fails with */
  failNext: VoiceError | null = null;
  /** recorded bytes per recording */
  received: number[] = [];
  spoken: { text: string; opts: SpeakOptions }[] = [];
  cancels = 0;
  /** while true, speech waits for `release()` (to test stopping it) */
  hold = false;
  private waiting: (() => void)[] = [];

  listen(): Recording {
    const id = `u${this.received.length}`;
    const at = this.received.push(0) - 1;
    return {
      id,
      send: (chunk) => {
        this.received[at] = (this.received[at] ?? 0) + chunk.byteLength;
      },
      end: async (): Promise<Transcript> => {
        const fail = this.failNext;
        this.failNext = null;
        if (fail) throw fail;
        return { text: this.transcripts.shift() ?? "", language: "de" };
      },
      cancel: () => undefined,
    };
  }

  speak(text: string, opts: SpeakOptions, onAudio: (wav: ArrayBuffer, seq: number) => void): Speech {
    this.spoken.push({ text, opts });
    const done = new Promise<void>((resolve) => {
      const go = () => {
        onAudio(FAKE_WAV, 0);
        resolve();
      };
      if (this.hold) this.waiting.push(go);
      else queueMicrotask(go);
    });
    return { id: `s${this.spoken.length}`, done, cancel: () => undefined };
  }

  /** Let the held speech come. */
  release(): void {
    const go = this.waiting;
    this.waiting = [];
    go.forEach((f) => f());
  }

  cancelAll(): void {
    this.cancels++;
    this.waiting = [];
  }

  close(): void {
    this.waiting = [];
  }
}

export class FakeMicrophone implements Microphone {
  denied = false;
  on = false;
  async start(onChunk: (pcm: ArrayBuffer) => void): Promise<number> {
    if (this.denied) throw new DOMException("denied", "NotAllowedError");
    this.on = true;
    onChunk(new ArrayBuffer(FAKE_CHUNK_BYTES));
    return SPEECH_RATE;
  }
  stop(): void {
    this.on = false;
  }
}

export class FakePlayer implements Player {
  played = 0;
  stops = 0;
  async play(): Promise<void> {
    this.played++;
  }
  stop(): void {
    this.stops++;
  }
}
