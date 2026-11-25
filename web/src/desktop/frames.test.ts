import { describe, expect, it } from "vitest";
import { frameStats, isSoftware, refreshOf } from "./frames";

describe("frameStats", () => {
  it("counts frames per second and the missed refreshes", () => {
    const gaps = [...Array<number>(118).fill(8.33), 25, 30];
    const s = frameStats(gaps, 8.33);
    expect(s.slow).toBe(2);
    expect(s.worstMs).toBe(30);
    expect(s.fps).toBeGreaterThan(100);
    expect(s.fps).toBeLessThan(120);
  });

  it("is empty without frames", () => {
    expect(frameStats([], 8.33)).toEqual({ fps: 0, worstMs: 0, p99Ms: 0, slow: 0 });
  });
});

describe("refreshOf", () => {
  it("rounds the median gap to a common rate", () => {
    expect(refreshOf([8.3, 8.4, 8.3, 30])).toBe(120);
    expect(refreshOf([16.7, 16.6, 16.8])).toBe(60);
    expect(refreshOf([6.9, 7.0, 6.95])).toBe(144);
  });
});

describe("isSoftware", () => {
  it("knows the software rasterisers", () => {
    expect(isSoftware("ANGLE (Google, Vulkan 1.3.0 (SwiftShader Device (Subzero)), SwiftShader driver)")).toBe(true);
    expect(isSoftware("ANGLE (Microsoft, Microsoft Basic Render Driver Direct3D11)")).toBe(true);
    expect(isSoftware("ANGLE (AMD, AMD Radeon RX 9070 XT Direct3D11 vs_5_0 ps_5_0, D3D11)")).toBe(false);
  });
});
