// @vitest-environment node
import { describe, expect, it } from "vitest";
import { formatBytes } from "./format";
import {
  benchmarkFromEvents,
  bestTgPerModel,
  downloadsFromEvents,
  eventDetailKey,
  loadEventText,
  latestBenchmarkEventId,
  latestConfigEventId,
  rowKey,
} from "./nodeActivity";
import type { BenchmarkRow, NodeEvent } from "./types";

let nextId = 100;
function ev(kind: string, data: Record<string, unknown>): NodeEvent {
  nextId -= 1;
  return { id: nextId, kind, data, ts: "2026-01-01T12:00:00Z", received_at: "" };
}

function row(model: string, backend: string, threads: number, tg: number): BenchmarkRow {
  return {
    benchmark_id: "b1",
    model,
    backend,
    threads,
    n_gpu_layers: 0,
    pp_tps: 50,
    tg_tps: tg,
    ts: "2026-01-01T12:00:00Z",
  };
}

describe("downloadsFromEvents", () => {
  it("keeps the newest progress per model and ignores malformed events", () => {
    const events = [
      ev("download_progress", {
        model: "a",
        status: "running",
        downloaded_bytes: 5,
        total_bytes: 10,
      }),
      ev("model_loaded", { model: "x" }),
      ev("download_progress", {
        model: "b",
        status: "failed",
        downloaded_bytes: 0,
        error: "sha mismatch",
      }),
      ev("download_progress", {
        model: "a",
        status: "running",
        downloaded_bytes: 1,
        total_bytes: 10,
      }),
      ev("download_progress", { model: "c", status: "weird" }),
      ev("download_progress", { status: "done" }),
    ];
    const downloads = downloadsFromEvents(events);
    expect(downloads.map((d) => [d.model, d.status, d.downloadedBytes])).toEqual([
      ["a", "running", 5],
      ["b", "failed", 0],
    ]);
    expect(downloads[1]?.error).toBe("sha mismatch");
    expect(downloads[1]?.totalBytes).toBeNull();
  });
});

describe("benchmarkFromEvents", () => {
  it("is running after benchmark_started and counts results", () => {
    const events = [
      ev("benchmark_result", { model: "m" }),
      ev("benchmark_result", { model: "m" }),
      ev("benchmark_started", { benchmark_id: "b2" }),
      ev("benchmark_finished", { benchmark_id: "b1", ok: true }),
    ];
    expect(benchmarkFromEvents(events)).toEqual({
      running: true,
      benchmarkId: "b2",
      results: 2,
      failures: [],
    });
  });

  it("is idle after benchmark_finished and reports failures", () => {
    const events = [
      ev("benchmark_finished", { benchmark_id: "b1", ok: false, error: "aborted: game detected" }),
      ev("benchmark_started", { benchmark_id: "b1" }),
    ];
    expect(benchmarkFromEvents(events)).toEqual({
      running: false,
      benchmarkId: "b1",
      results: 0,
      failures: [],
      error: "aborted: game detected",
    });
    expect(benchmarkFromEvents([])).toEqual({
      running: false,
      benchmarkId: null,
      results: 0,
      failures: [],
    });
  });

  it("lists the runs that failed while the benchmark went on, oldest first, of the last run only", () => {
    const failure = (id: string, backend: string, error: string) =>
      ev("benchmark_error", { benchmark_id: id, model: "big", backend, error });
    const events = [
      ev("benchmark_finished", { benchmark_id: "b2", ok: true, failed_runs: 2 }),
      failure("b2", "cpu", "second"),
      ev("benchmark_result", { benchmark_id: "b2", model: "big" }),
      failure("b2", "vulkan", "ErrorOutOfDeviceMemory"),
      ev("benchmark_started", { benchmark_id: "b2" }),
      ev("benchmark_finished", { benchmark_id: "b1", ok: true }),
      failure("b1", "cuda", "an older run"),
      ev("benchmark_started", { benchmark_id: "b1" }),
    ];
    const activity = benchmarkFromEvents(events);
    expect(activity.running).toBe(false);
    expect(activity.error).toBeUndefined();
    expect(activity.failures.map((f) => [f.backend, f.error])).toEqual([
      ["vulkan", "ErrorOutOfDeviceMemory"],
      ["cpu", "second"],
    ]);
    const running = benchmarkFromEvents(events.slice(1, 5));
    expect(running.running).toBe(true);
    expect(running.results).toBe(1);
    expect(running.failures.map((f) => f.backend)).toEqual(["vulkan", "cpu"]);
  });

  it("finds the newest benchmark result/finished event id", () => {
    const events = [ev("connected", {}), ev("benchmark_result", {}), ev("benchmark_started", {})];
    expect(latestBenchmarkEventId(events)).toBe(events[1]?.id);
    expect(latestBenchmarkEventId([ev("benchmark_started", {})])).toBeNull();
    expect(latestBenchmarkEventId([ev("benchmark_error", {})])).not.toBeNull();
  });
});

describe("bestTgPerModel", () => {
  it("marks the fastest generation per model", () => {
    const rows = [
      row("a", "cpu", 2, 10),
      row("a", "cpu", 12, 14),
      row("a", "vulkan", 6, 12),
      row("b", "cpu", 2, 3),
    ];
    const best = bestTgPerModel(rows);
    expect(best.size).toBe(2);
    expect(best.has(rowKey(rows[1] as BenchmarkRow))).toBe(true);
    expect(best.has(rowKey(rows[3] as BenchmarkRow))).toBe(true);
  });
});

describe("formatBytes", () => {
  it("uses MB below 1 GB and GB above", () => {
    expect(formatBytes(850_000_000, "de")).toBe("850 MB");
    expect(formatBytes(1_117_320_736, "de")).toBe("1,1 GB");
  });
});

describe("config events", () => {
  it("finds the newest config_changed event", () => {
    const events = [ev("model_loaded", {}), ev("config_changed", { source: "residents" })];
    events.reverse(); // newest first, like the hub sends them
    expect(latestConfigEventId(events)).toBe(events.find((e) => e.kind === "config_changed")?.id);
    expect(latestConfigEventId([ev("model_loaded", {})])).toBeNull();
  });

  it("describes reasons and config sources", () => {
    expect(eventDetailKey(ev("model_unloaded", { model: "m", reason: "vram_pressure" }))).toBe(
      "eventReason.vram_pressure",
    );
    expect(eventDetailKey(ev("model_loaded", { model: "m", reason: "pressure_cleared" }))).toBe(
      "eventReason.pressure_cleared",
    );
    expect(eventDetailKey(ev("model_loaded", { model: "m" }))).toBeNull();
    expect(
      eventDetailKey(ev("config_changed", { source: "residents", resident: { model: "m" } })),
    ).toBe("configSource.residentOn");
    expect(eventDetailKey(ev("config_changed", { source: "residents", resident: null }))).toBe(
      "configSource.residentOff",
    );
    expect(eventDetailKey(ev("config_changed", { source: "manual_unload", models: ["m"] }))).toBe(
      "configSource.manual_unload",
    );
    expect(eventDetailKey(ev("config_changed", { source: "migration 0005", added: [] }))).toBe(
      "configSource.migration",
    );
    expect(eventDetailKey(ev("config_changed", { user_id: "u" }))).toBeNull();
    expect(eventDetailKey(ev("game_detected", { reason: "x" }))).toBeNull();
  });
});

describe("loadEventText", () => {
  it("names the model and the reason of failed and retried loads and late results", () => {
    expect(
      loadEventText(
        ev("model_load_failed", {
          model: "big",
          code: "runner_error",
          message: "load big: failed to allocate",
        }),
      ),
    ).toBe("big: load big: failed to allocate");
    expect(
      loadEventText(
        ev("model_load_retry", {
          model: "big",
          attempt: 2,
          plan: "cpu",
          error: "failed to allocate",
        }),
      ),
    ).toBe("big: cpu – failed to allocate");
    expect(
      loadEventText(
        ev("action_late_result", {
          name: "models.load",
          ok: false,
          error: { code: "runner_error", message: "boom" },
        }),
      ),
    ).toBe("models.load: boom");
    expect(
      loadEventText(ev("action_late_result", { name: "models.load", ok: true, error: null })),
    ).toBe("models.load: ok");
    expect(loadEventText(ev("model_loaded", { model: "big" }))).toBeNull();
  });
});

describe("loadEventText for update events", () => {
  it("shows the version of a step and the reason of a failure", () => {
    expect(loadEventText(ev("update_staged", { version: "0.3.0" }))).toBe("0.3.0");
    expect(loadEventText(ev("update_failed", { version: "0.3.0", reason: "signature: wrong key" }))).toBe(
      "0.3.0: signature: wrong key",
    );
    expect(loadEventText(ev("update_failed", { reason: "no release" }))).toBe("no release");
    expect(loadEventText(ev("update_failed", {}))).toBeNull();
  });
});

describe("benchmarkFromEvents, a run the node lost", () => {
  const started = (ts: string): NodeEvent => ({
    id: 1,
    kind: "benchmark_started",
    data: { benchmark_id: "b9" },
    ts,
    received_at: "",
  });
  const noon = Date.parse("2026-01-01T12:00:00Z");

  it("is running while the start is younger than a benchmark may take", () => {
    const soon = noon + 60 * 60 * 1000;
    expect(benchmarkFromEvents([started("2026-01-01T12:00:00Z")], soon).running).toBe(true);
  });

  it("is stale, not running, when the start is older and no end ever came", () => {
    const late = noon + 3 * 60 * 60 * 1000;
    expect(benchmarkFromEvents([started("2026-01-01T12:00:00Z")], late)).toEqual({
      running: false,
      benchmarkId: "b9",
      results: 0,
      failures: [],
      stale: true,
    });
  });

  it("does not judge the age when no clock is given", () => {
    expect(benchmarkFromEvents([started("2020-01-01T00:00:00Z")]).running).toBe(true);
  });
});
