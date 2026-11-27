/**
 * Push to talk (D127, issue #54): hold the key to record, let go to send. The transcript goes into
 * the conversation as the person's message through the chat's own send; the answer is read out
 * sentence by sentence as it is written. Half duplex: pressing the key again while it speaks stops
 * the speech at once (the first step of barge-in, #60).
 *
 * A transcript is only ever sent as a message: nothing here confirms a card or an action. A spoken
 * "yes" is the same as a typed one (D15, D83).
 */

import { SPEECH_RATE, type Microphone, type Player } from "./audio";
import { VoiceError, type Recording, type VoiceLink } from "./link";
import { SentenceCutter } from "./speech";

export type VoiceState = "idle" | "listening" | "thinking" | "speaking";

/** What went wrong, as an i18n key under `voice.problem`. */
export type VoiceProblem =
  | "micDenied"
  | "nothingHeard"
  | "tooLong"
  | "noNode"
  | "connection"
  | "failed";

export interface VoiceSnapshot {
  state: VoiceState;
  problem: VoiceProblem | null;
}

export interface VoiceDeps {
  link: VoiceLink;
  mic: Microphone;
  player: Player;
}

export interface TalkTarget {
  conversationId?: string;
  incognito?: boolean;
  language?: string;
  /** Whether the hub can read out answers (a node offers `voice.speak`). */
  speak: boolean;
}

const PROBLEMS: Record<string, VoiceProblem> = {
  too_long: "tooLong",
  no_voice_node: "noNode",
  not_granted: "noNode",
  node_offline: "noNode",
  closed: "connection",
  unauthorized: "connection",
};

function problemOf(err: unknown): VoiceProblem {
  if (err instanceof VoiceError) return PROBLEMS[err.code] ?? "failed";
  if (err instanceof DOMException && ["NotAllowedError", "SecurityError", "NotFoundError"].includes(err.name)) {
    return "micDenied";
  }
  return "failed";
}

export class PushToTalk {
  private snapshot: VoiceSnapshot = { state: "idle", problem: null };
  private listeners = new Set<() => void>();
  private recording: Recording | null = null;
  /** the key is down: a release that comes while the microphone still starts ends it at once */
  private held = false;
  private starting: Promise<void> | null = null;
  /** an answer to the last transcript is expected: it is read out */
  private awaiting = false;
  private answerDone = false;
  private cutter = new SentenceCutter();
  private outstanding = 0;
  private turn = 0;
  /** the chat the key went down in: the transcript belongs there */
  private saidIn: string | undefined;

  constructor(
    private readonly deps: VoiceDeps,
    private target: TalkTarget,
    /** the transcript, and the chat the key was pressed in */
    private readonly send: (text: string, conversationId?: string) => void,
  ) {}

  // --- the store the hook reads -----------------------------------------------------------------

  get state(): VoiceSnapshot {
    return this.snapshot;
  }

  subscribe = (fn: () => void): (() => void) => {
    this.listeners.add(fn);
    return () => this.listeners.delete(fn);
  };

  private set(state: VoiceState, problem: VoiceProblem | null = null): void {
    if (state === this.snapshot.state && problem === this.snapshot.problem) return;
    this.snapshot = { state, problem };
    this.listeners.forEach((fn) => fn());
  }

  retarget(target: TalkTarget): void {
    this.target = target;
  }

  // --- the key ----------------------------------------------------------------------------------

  /** The key went down: stop what is spoken, start recording. */
  press(): void {
    if (this.held) return;
    this.held = true;
    this.silence();
    const turn = ++this.turn;
    this.saidIn = this.target.conversationId;
    const recording = this.deps.link.listen({
      rate: SPEECH_RATE,
      language: this.target.language,
      incognito: this.target.incognito,
    });
    this.recording = recording;
    this.set("listening");
    this.starting = this.startMic(recording, turn);
  }

  private async startMic(recording: Recording, turn: number): Promise<void> {
    try {
      const rate = await this.deps.mic.start((pcm) => recording.send(pcm));
      if (rate !== SPEECH_RATE) throw new VoiceError("failed", `the microphone gave ${rate} Hz`);
    } catch (err) {
      if (turn !== this.turn) return;
      recording.cancel();
      this.recording = null;
      this.held = false;
      this.set("idle", problemOf(err));
    }
  }

  /** The key was let go: the recording is sent and its text becomes the message. */
  async release(): Promise<void> {
    if (!this.held) return;
    this.held = false;
    await this.starting;
    const recording = this.recording;
    if (!recording) return; // the microphone was refused: the problem is shown already
    this.recording = null;
    this.deps.mic.stop();
    const turn = this.turn;
    this.set("thinking");
    try {
      const { text } = await recording.end();
      if (turn !== this.turn) return;
      if (!text.trim()) {
        this.set("idle", "nothingHeard");
        return;
      }
      this.awaiting = this.target.speak;
      this.answerDone = false;
      this.cutter = new SentenceCutter();
      this.send(text.trim(), this.saidIn);
      if (!this.awaiting) this.set("idle");
    } catch (err) {
      if (turn !== this.turn) return;
      this.set("idle", problemOf(err));
    }
  }

  /** Stop everything: recording, what waits to be said, what is said. */
  cancel(): void {
    this.turn++;
    this.held = false;
    if (this.recording) {
      this.deps.mic.stop();
      this.recording.cancel();
      this.recording = null;
    }
    this.silence();
    this.set("idle");
  }

  private silence(): void {
    this.awaiting = false;
    this.outstanding = 0;
    this.deps.player.stop();
    this.deps.link.cancelAll();
  }

  // --- the answer -------------------------------------------------------------------------------

  /** The answer to the transcript as it is written (`text` is all of it so far); `done` once it
   *  is finished or failed (then `text` is what there is). The page hands only that answer. */
  answer(text: string, done: boolean): void {
    if (!this.awaiting || this.answerDone) return;
    this.answerDone = done;
    this.cutter.take(text, done).forEach((sentence) => this.say(sentence));
    this.settleIfQuiet();
  }

  private say(sentence: string): void {
    const turn = this.turn;
    this.outstanding++;
    const plays: Promise<void>[] = [];
    const speech = this.deps.link.speak(
      sentence,
      { conversationId: this.target.conversationId, incognito: this.target.incognito },
      (wav) => {
        if (turn !== this.turn || !this.awaiting) return;
        this.set("speaking");
        plays.push(this.deps.player.play(wav));
      },
    );
    void this.finish(speech.done, plays, turn);
  }

  private async finish(done: Promise<void>, plays: Promise<void>[], turn: number): Promise<void> {
    let failed: unknown = null;
    try {
      await done;
      await Promise.all(plays);
    } catch (err) {
      failed = err;
    }
    if (turn !== this.turn || !this.awaiting) return;
    this.outstanding--;
    if (failed) {
      this.silence();
      this.set("idle", problemOf(failed));
      return;
    }
    this.settleIfQuiet();
  }

  private settleIfQuiet(): void {
    if (this.answerDone && this.outstanding === 0 && this.awaiting) {
      this.awaiting = false;
      this.set("idle");
    } else if (this.awaiting && this.snapshot.state !== "speaking") {
      this.set("thinking");
    }
  }

  dispose(): void {
    this.cancel();
    this.deps.link.close();
    this.listeners.clear();
  }
}
