// @vitest-environment node
import { describe, expect, it } from "vitest";
import { effectivePressure, PRESSURE_DEFAULTS, residentsOf } from "./pressure";

describe("effectivePressure", () => {
  it("is off without a pressure object, like the node", () => {
    const p = effectivePressure({ roles: ["gpu"] });
    expect(p.enabled).toBe(false);
    expect(p.ram.enabled).toBe(false);
    expect(p.unload_below_free_mb).toBe(PRESSURE_DEFAULTS.unload_below_free_mb);
    expect(effectivePressure(null).enabled).toBe(false);
  });

  it("enables VRAM pressure for a present object and fills in defaults", () => {
    const p = effectivePressure({ pressure: { margin_mb: 512, ram: { enabled: true } } });
    expect(p.enabled).toBe(true);
    expect(p.margin_mb).toBe(512);
    expect(p.reload_after_seconds).toBe(60);
    expect(p.ram).toEqual({ ...PRESSURE_DEFAULTS.ram, enabled: true });
  });

  it("turns RAM handling on for a node without a dedicated GPU unless the config says otherwise", () => {
    const open = { pressure: { margin_mb: 512 } };
    expect(effectivePressure(open, true).ram.enabled).toBe(false);
    expect(effectivePressure(open, false).ram.enabled).toBe(true);
    expect(effectivePressure({ pressure: { ram: { enabled: false } } }, false).ram.enabled).toBe(
      false,
    );
    expect(effectivePressure({ pressure: { ram: { margin_mb: 5 } } }, false).ram.enabled).toBe(
      true,
    );
    expect(effectivePressure(null, false).ram.enabled).toBe(false); // nothing is watched at all
  });

  it("ignores values of the wrong type", () => {
    const p = effectivePressure({ pressure: { enabled: "yes", poll_seconds: "5", ram: [] } });
    expect(p.enabled).toBe(true);
    expect(p.poll_seconds).toBe(2);
    expect(p.ram.enabled).toBe(false);
  });
});

describe("residentsOf", () => {
  it("parses entries and skips malformed ones", () => {
    const config = {
      residents: [
        { model: "a", priority: 80, auto_reload: false, context_length: 8192 },
        { model: "b" },
        { priority: 10 },
        "c",
      ],
    };
    expect(residentsOf(config)).toEqual([
      { model: "a", priority: 80, auto_reload: false, context_length: 8192 },
      { model: "b" },
    ]);
    expect(residentsOf({ residents: "x" })).toEqual([]);
    expect(residentsOf(null)).toEqual([]);
  });
});
