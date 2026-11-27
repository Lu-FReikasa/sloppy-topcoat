// @vitest-environment node
import { readdirSync, readFileSync, statSync } from "node:fs";
import { join } from "node:path";
import { describe, expect, it } from "vitest";
import de from "./de.json";
import en from "./en.json";

const SRC = join(__dirname, "..");

function sourceFiles(dir: string): string[] {
  const out: string[] = [];
  for (const entry of readdirSync(dir)) {
    const p = join(dir, entry);
    if (statSync(p).isDirectory()) {
      if (entry !== "test") out.push(...sourceFiles(p));
    } else if (/\.(ts|tsx)$/.test(entry) && !/\.test\.(ts|tsx)$/.test(entry)) {
      out.push(p);
    }
  }
  return out;
}

function flatKeys(obj: Record<string, unknown>, prefix = ""): string[] {
  return Object.entries(obj).flatMap(([k, v]) =>
    v && typeof v === "object"
      ? flatKeys(v as Record<string, unknown>, `${prefix}${k}.`)
      : [prefix + k],
  );
}

const catalogues = { en, de };

function hasKey(obj: unknown, key: string): boolean {
  let cur: unknown = obj;
  for (const part of key.split(".")) {
    if (!cur || typeof cur !== "object" || !(part in cur)) return false;
    cur = (cur as Record<string, unknown>)[part];
  }
  return typeof cur === "string";
}

describe("i18n catalogue", () => {
  it("has the same keys in English and German", () => {
    const enKeys = flatKeys(en).sort();
    const deKeys = flatKeys(de).sort();
    expect(enKeys.filter((k) => !deKeys.includes(k))).toEqual([]);
    expect(deKeys.filter((k) => !enKeys.includes(k))).toEqual([]);
  });

  it("keeps placeholders identical in both languages", () => {
    const vars = (s: string) => [...s.matchAll(/{{(\w+)}}/g)].map((m) => m[1]).sort();
    const mismatched = flatKeys(en).filter((k) => {
      const get = (o: unknown) =>
        k.split(".").reduce<unknown>((c, p) => (c as Record<string, unknown>)[p], o);
      return JSON.stringify(vars(String(get(en)))) !== JSON.stringify(vars(String(get(de))));
    });
    expect(mismatched).toEqual([]);
  });

  it("contains every literal t('...') key used in src", () => {
    const keyRe = /\bt\(\s*["'`]([A-Za-z0-9_.]+)["'`]/g;
    const used = new Set<string>();
    for (const file of sourceFiles(SRC)) {
      for (const m of readFileSync(file, "utf8").matchAll(keyRe)) {
        if (m[1]) used.add(m[1]);
      }
    }
    expect(used.size).toBeGreaterThan(30);
    for (const [lang, cat] of Object.entries(catalogues)) {
      const missing = [...used].filter((k) => !hasKey(cat, k));
      expect(missing, lang).toEqual([]);
    }
  });

  it("contains keys that are built dynamically", () => {
    const dynamic = [
      ...["available", "busy_gaming", "degraded", "yielded", "offline"].map((s) => `state.${s}`),
      ...["connecting", "live", "lost"].map((s) => `connection.${s}`),
      ...["llamaserver", "fake"].map((s) => `runnerType.${s}`),
      ...["models", "history", "rights", "maintenance"].map((s) => `nodeTabs.${s}`),
      ...[
        "model_loaded",
        "model_unloaded",
        "game_detected",
        "game_ended",
        "download_progress",
        "state_changed",
        "error",
        "connected",
        "disconnected",
        "paired",
        "action",
        "benchmark_started",
        "benchmark_result",
        "benchmark_error",
        "benchmark_finished",
        "runtime_installed",
        "config_changed",
        "auto_reload_paused",
        "model_load_failed",
        "model_load_retry",
        "model_load_cancelled",
        "action_late_result",
        "paused",
        "resumed",
        "sleeping",
        "wake_sent",
      ].map((k) => `eventKind.${k}`),
      // Every reason a node sends with model_loaded / model_unloaded.
      ...[
        "action",
        "idle",
        "game",
        "deleted",
        "vram_pressure",
        "ram_pressure",
        "resident",
        "pressure_cleared",
        "pause_timeout",
      ].map((r) => `eventReason.${r}`),
      ...["input", "cpu_load", "battery"].map((r) => `pauseReason.${r}`),
      ...["unload", "unload_all", "pause_timeout"].map((r) => `cancelReason.${r}`),
      ...["low_free", "demotion", "unmet_demand"].map((s) => `eventSignal.${s}`),
      ...[
        "residentOn",
        "residentOff",
        "manual_unload",
        "manual_load",
        "benchmark",
        "migration",
      ].map((s) => `configSource.${s}`),
      ...[
        "unload_below_free_mb",
        "margin_mb",
        "foreign_shared_trigger_mb",
        "sustain_seconds",
        "reload_after_seconds",
        "reload_headroom_mb",
        "reload_max_foreign_gpu_util",
        "min_off_seconds",
        "poll_seconds",
        "ram_unload_below_free_mb",
        "ram_margin_mb",
        "ram_reload_headroom_mb",
      ].map((f) => `pressure.${f}`),
      ...[
        "idle_seconds",
        "cpu_percent",
        "sustain_seconds",
        "resume_seconds",
        "unload_after_pause_seconds",
        "poll_seconds",
      ].map((f) => `yield.${f}`),
      ...["performance", "balanced", "saver"].flatMap((p) => [
        `power.profiles.${p}`,
        `power.summary.${p}`,
      ]),
      ...["cuda", "vulkan", "cpu"].map((b) => `backend.${b}`),
      ...["remembered", "confirmed", "corrected", "retracted", "withdrawnNote", "replaces"].map(
        (k) => `memory.${k}`,
      ),
      ...["residence", "birthday", "age", "name", "work", "pet", "family", "other"].map(
        (k) => `memory.question.topic.${k}`,
      ),
      ...["running", "done", "failed"].map((s) => `download.status.${s}`),
      ...["node_offline", "upgrade_required", "timeout", "not_found", "network", "generic"].map(
        (c) => `errors.${c}`,
      ),
    ];
    for (const [lang, cat] of Object.entries(catalogues)) {
      expect(
        dynamic.filter((k) => !hasKey(cat, k)),
        lang,
      ).toEqual([]);
    }
  });
});
