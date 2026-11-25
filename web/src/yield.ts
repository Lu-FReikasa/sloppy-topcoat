// Local-use ("ducken") and energy settings of a node config (E2-05 B and E, docs/PROTOCOL.md
// "Yielding to local use"). The defaults mirror the node's (protocol.EffectiveYieldFor).

export interface Yield {
  enabled: boolean;
  low_priority: boolean;
  poll_seconds: number;
  idle_seconds: number;
  cpu_percent: number;
  sustain_seconds: number;
  resume_seconds: number;
  pause_on_battery: boolean;
  unload_after_pause_seconds: number;
}

export const YIELD_DEFAULTS: Omit<Yield, "enabled"> = {
  low_priority: true,
  poll_seconds: 5,
  idle_seconds: 60,
  cpu_percent: 60,
  sustain_seconds: 5,
  resume_seconds: 30,
  pause_on_battery: true,
  unload_after_pause_seconds: 900,
};

export type PowerProfile = "performance" | "balanced" | "saver";

export interface Power {
  profile: PowerProfile;
  /** 0 = never. */
  sleep_after_idle_seconds: number;
}

function obj(v: unknown): Record<string, unknown> | null {
  return v && typeof v === "object" && !Array.isArray(v) ? (v as Record<string, unknown>) : null;
}

/**
 * The yield settings a node applies. Freezing for local use that the config leaves open
 * (`enabled` absent) is on for a node without a dedicated GPU — the laptop that must stay
 * usable — and off with one (the fast machine).
 */
export function effectiveYield(config: Record<string, unknown> | null, dedicatedGpu = true): Yield {
  const y = obj(config?.yield) ?? {};
  const out: Yield = { enabled: !dedicatedGpu, ...YIELD_DEFAULTS };
  for (const key of Object.keys(out) as (keyof Yield)[]) {
    const v = y[key];
    if (typeof v === typeof out[key]) (out[key] as Yield[keyof Yield]) = v as Yield[keyof Yield];
  }
  return out;
}

const PROFILES: PowerProfile[] = ["performance", "balanced", "saver"];

/** The energy profile a node applies (default: balanced, never sleeps by itself). */
export function effectivePower(config: Record<string, unknown> | null): Power {
  const p = obj(config?.power) ?? {};
  const profile = PROFILES.find((x) => x === p.profile) ?? "balanced";
  const sleep = typeof p.sleep_after_idle_seconds === "number" ? p.sleep_after_idle_seconds : 0;
  return { profile, sleep_after_idle_seconds: sleep };
}

export const POWER_PROFILES = PROFILES;
