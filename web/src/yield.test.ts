import { describe, expect, it } from "vitest";
import { effectivePower, effectiveYield, YIELD_DEFAULTS } from "./yield";

describe("effectiveYield", () => {
  it("freezes by default on a node without a dedicated GPU, not with one", () => {
    expect(effectiveYield({}, false)).toEqual({ enabled: true, ...YIELD_DEFAULTS });
    expect(effectiveYield({}, true).enabled).toBe(false);
    expect(effectiveYield(null, false).idle_seconds).toBe(60);
  });

  it("takes what the config says and ignores malformed values", () => {
    const y = effectiveYield(
      { yield: { enabled: false, idle_seconds: 10, cpu_percent: "x", pause_on_battery: false } },
      false,
    );
    expect(y.enabled).toBe(false);
    expect(y.idle_seconds).toBe(10);
    expect(y.cpu_percent).toBe(60);
    expect(y.pause_on_battery).toBe(false);
  });
});

describe("effectivePower", () => {
  it("defaults to balanced and no sleep", () => {
    expect(effectivePower({})).toEqual({ profile: "balanced", sleep_after_idle_seconds: 0 });
    expect(effectivePower({ power: { profile: "turbo" } }).profile).toBe("balanced");
    expect(effectivePower({ power: { profile: "saver", sleep_after_idle_seconds: 600 } })).toEqual({
      profile: "saver",
      sleep_after_idle_seconds: 600,
    });
  });
});
