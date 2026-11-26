import { describe, expect, it } from "vitest";
import { downsample, toPcm16 } from "./audio";

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
