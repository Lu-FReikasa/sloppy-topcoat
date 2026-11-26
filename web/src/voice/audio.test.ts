import { afterEach, describe, expect, it, vi } from "vitest";
import { WebMicrophone, downsample, toPcm16 } from "./audio";

describe("audio helpers", () => {
  it("averages 48 kHz down to 16 kHz", () => {
    const out = downsample(new Float32Array([0.3, 0.3, 0.3, -0.6, -0.6, -0.6]), 48000, 16000);
    expect(Array.from(out).map((v) => Math.round(v * 10) / 10)).toEqual([0.3, -0.6]);
  });

  it("leaves a rate that is already low alone", () => {
    const input = new Float32Array([0.1, 0.2]);
    expect(downsample(input, 16000, 16000)).toBe(input);
  });

  it("writes clipped 16-bit little-endian samples", () => {
    const view = new DataView(toPcm16(new Float32Array([0, 1, -1, 2])));
    expect([0, 1, 2, 3].map((i) => view.getInt16(i * 2, true))).toEqual([0, 32767, -32767, 32767]);
  });
});

describe("the browser microphone", () => {
  afterEach(() => vi.unstubAllGlobals());

  it("closes a stream the browser grants after the recording was stopped", async () => {
    const track = { stop: vi.fn() };
    let grant: (s: unknown) => void = () => undefined;
    vi.stubGlobal("navigator", {
      mediaDevices: { getUserMedia: () => new Promise((resolve) => (grant = resolve)) },
    });
    const mic = new WebMicrophone();
    const started = mic.start(() => undefined);
    mic.stop(); // Esc while the browser still asks for the microphone
    grant({ getTracks: () => [track] });
    await expect(started).rejects.toThrow();
    expect(track.stop).toHaveBeenCalled();
  });
});
