/**
 * The microphone and the loudspeaker behind interfaces (CLAUDE.md "Hardware"): the controller and
 * its tests use fakes; the Web Audio parts below are UNVERIFIED on real hardware (a browser's
 * microphone permission, WebView2 in the desktop client).
 */

/** The rate the hub's speech to text wants (whisper: 16 kHz mono). */
export const SPEECH_RATE = 16000;
const SAMPLES_PER_CHUNK = 4096; // ~85 ms at 48 kHz: small enough to stream (#60)
const PCM_MAX = 0x7fff;
const BYTES_PER_SAMPLE = 2;

export interface Microphone {
  /** Start recording; every chunk is raw 16-bit little-endian mono PCM at the returned rate.
   *  Rejects when the person (or the browser) refuses the microphone. */
  start(onChunk: (pcm: ArrayBuffer) => void): Promise<number>;
  stop(): void;
}

export interface Player {
  /** Queue one WAV behind what plays; settles when it has been heard (or stopped). */
  play(wav: ArrayBuffer): Promise<void>;
  /** Stop at once and drop what waits (barge-in). */
  stop(): void;
}

/** Fewer samples by averaging (the voice band is far below 8 kHz: no filter needed for whisper). */
export function downsample(input: Float32Array, from: number, to: number): Float32Array {
  if (to >= from) return input;
  const ratio = from / to;
  const out = new Float32Array(Math.floor(input.length / ratio));
  return out.map((_, i) => {
    const start = Math.floor(i * ratio);
    const end = Math.min(input.length, Math.floor((i + 1) * ratio));
    const part = input.subarray(start, Math.max(end, start + 1));
    return part.reduce((a, b) => a + b, 0) / part.length;
  });
}

/** Float samples (-1..1) as 16-bit little-endian PCM. */
export function toPcm16(samples: Float32Array): ArrayBuffer {
  const view = new DataView(new ArrayBuffer(samples.length * BYTES_PER_SAMPLE));
  samples.forEach((v, i) => {
    const clipped = Math.max(-1, Math.min(1, v));
    view.setInt16(i * BYTES_PER_SAMPLE, Math.round(clipped * PCM_MAX), true);
  });
  return view.buffer;
}

/** The browser's microphone. UNVERIFIED on real hardware. A ScriptProcessor, not an AudioWorklet:
 *  a worklet needs a module URL, which the hub's CSP and the desktop client's web view would have
 *  to allow; the processor runs everywhere the GUI runs. */
export class WebMicrophone implements Microphone {
  private stream: MediaStream | null = null;
  private ctx: AudioContext | null = null;
  private node: ScriptProcessorNode | null = null;
  /** counts starts and stops: a stream granted after a stop (Esc while the browser still asked)
   *  is closed at once, so the microphone is never left on */
  private generation = 0;

  async start(onChunk: (pcm: ArrayBuffer) => void): Promise<number> {
    this.stop();
    const generation = this.generation;
    const stream = await navigator.mediaDevices.getUserMedia({
      audio: { channelCount: 1, echoCancellation: true, noiseSuppression: true, autoGainControl: true },
    });
    if (generation !== this.generation) {
      stream.getTracks().forEach((t) => t.stop());
      throw new DOMException("the recording was stopped before the microphone started", "AbortError");
    }
    const ctx = new AudioContext();
    const source = ctx.createMediaStreamSource(stream);
    const node = ctx.createScriptProcessor(SAMPLES_PER_CHUNK, 1, 1);
    node.onaudioprocess = (e) => {
      const samples = downsample(e.inputBuffer.getChannelData(0), ctx.sampleRate, SPEECH_RATE);
      onChunk(toPcm16(samples));
    };
    source.connect(node);
    node.connect(ctx.destination); // a processor runs only while connected; it writes silence
    this.stream = stream;
    this.ctx = ctx;
    this.node = node;
    return SPEECH_RATE;
  }

  stop(): void {
    this.generation++;
    this.node?.disconnect();
    this.stream?.getTracks().forEach((t) => t.stop());
    void closeQuietly(this.ctx);
    this.node = null;
    this.stream = null;
    this.ctx = null;
  }
}

async function closeQuietly(ctx: AudioContext | null): Promise<void> {
  try {
    await ctx?.close();
  } catch {
    /* already closed */
  }
}

/** The loudspeaker: WAVs one after the other without a gap. UNVERIFIED on real hardware. */
export class WebPlayer implements Player {
  private ctx: AudioContext | null = null;
  private endsAt = 0;
  private playing = new Set<AudioBufferSourceNode>();
  private generation = 0;

  async play(wav: ArrayBuffer): Promise<void> {
    const ctx = (this.ctx ??= new AudioContext());
    const generation = this.generation;
    const buffer = await ctx.decodeAudioData(wav.slice(0));
    if (generation !== this.generation) return; // stopped while it was decoded
    const source = ctx.createBufferSource();
    source.buffer = buffer;
    source.connect(ctx.destination);
    const at = Math.max(ctx.currentTime, this.endsAt);
    this.endsAt = at + buffer.duration;
    this.playing.add(source);
    await new Promise<void>((resolve) => {
      source.onended = () => {
        this.playing.delete(source);
        resolve();
      };
      source.start(at);
    });
  }

  stop(): void {
    this.generation++;
    this.playing.forEach((s) => {
      try {
        s.stop();
      } catch {
        /* not started yet */
      }
    });
    this.playing.clear();
    this.endsAt = 0;
  }
}
