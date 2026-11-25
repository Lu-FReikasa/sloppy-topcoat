// Resident models and VRAM pressure settings of a node config (E1-06, docs/PROTOCOL.md
// "VRAM pressure and resident models"). The defaults mirror the node's.
import type { Resident } from "./types";

export const DEFAULT_RESIDENT_PRIORITY = 50;

export interface RamPressure {
  enabled: boolean;
  unload_below_free_mb: number;
  margin_mb: number;
  reload_headroom_mb: number;
}

export interface Pressure {
  enabled: boolean;
  poll_seconds: number;
  sustain_seconds: number;
  unload_below_free_mb: number;
  margin_mb: number;
  reload_after_seconds: number;
  reload_headroom_mb: number;
  reload_max_foreign_gpu_util: number;
  min_off_seconds: number;
  reload_while_rule_matches: boolean;
  foreign_shared_trigger_mb: number;
  ram: RamPressure;
}

export const PRESSURE_DEFAULTS: Pressure = {
  enabled: true,
  poll_seconds: 2,
  sustain_seconds: 3,
  unload_below_free_mb: 1024,
  margin_mb: 1024,
  reload_after_seconds: 60,
  reload_headroom_mb: 2048,
  reload_max_foreign_gpu_util: 30,
  min_off_seconds: 120,
  reload_while_rule_matches: false,
  foreign_shared_trigger_mb: 1024,
  ram: { enabled: false, unload_below_free_mb: 2048, margin_mb: 1024, reload_headroom_mb: 1024 },
};

function obj(v: unknown): Record<string, unknown> | null {
  return v && typeof v === "object" && !Array.isArray(v) ? (v as Record<string, unknown>) : null;
}

function pick<T>(src: Record<string, unknown>, defaults: T): T {
  const out = { ...defaults };
  for (const key of Object.keys(defaults as object) as (keyof T)[]) {
    const v = src[key as string];
    if (typeof v === typeof defaults[key]) out[key] = v as T[keyof T];
  }
  return out;
}

/**
 * The pressure settings a node applies. Like the node: without a `pressure` object nothing
 * is watched; a present object enables VRAM pressure unless it says otherwise. RAM handling
 * that the config leaves open (`ram.enabled` absent) is on for a node without a dedicated GPU
 * (E2-05, D27): its models live in system memory.
 */
export function effectivePressure(
  config: Record<string, unknown> | null,
  dedicatedGpu = true,
): Pressure {
  const p = obj(config?.pressure);
  if (!p) {
    return {
      ...PRESSURE_DEFAULTS,
      enabled: false,
      ram: { ...PRESSURE_DEFAULTS.ram, enabled: false },
    };
  }
  const { ram: _ram, ...flat } = PRESSURE_DEFAULTS;
  void _ram;
  const ramConfig = obj(p.ram) ?? {};
  const ram = pick(ramConfig, PRESSURE_DEFAULTS.ram);
  if (typeof ramConfig.enabled !== "boolean") ram.enabled = !dedicatedGpu;
  return { ...pick(p, flat), ram };
}

/** The config's resident entries (malformed entries are skipped). */
export function residentsOf(config: Record<string, unknown> | null): Resident[] {
  const list = config?.residents;
  if (!Array.isArray(list)) return [];
  const out: Resident[] = [];
  for (const item of list) {
    const r = obj(item);
    if (!r || typeof r.model !== "string") continue;
    const entry: Resident = { model: r.model };
    if (typeof r.priority === "number") entry.priority = r.priority;
    if (typeof r.context_length === "number") entry.context_length = r.context_length;
    if (typeof r.auto_reload === "boolean") entry.auto_reload = r.auto_reload;
    out.push(entry);
  }
  return out;
}
