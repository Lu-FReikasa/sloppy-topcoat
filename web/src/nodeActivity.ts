// Live activity of a node derived from its event list (newest first), so the hub keeps no
// extra state: benchmark runs and model downloads (docs/PROTOCOL.md "Long-running actions").
import type { BenchmarkRow, NodeEvent } from "./types";

export type DownloadStatus = "running" | "done" | "failed";

export interface DownloadInfo {
  model: string;
  status: DownloadStatus;
  downloadedBytes: number | null;
  totalBytes: number | null;
  error?: string;
  ts: string;
}

/** One benchmark run that failed while the others went on (event benchmark_error). */
export interface BenchmarkFailure {
  model: string;
  backend: string;
  error: string;
}

export interface BenchmarkActivity {
  running: boolean;
  benchmarkId: string | null;
  /** benchmark_result events of the running (or last) run so far. */
  results: number;
  /** Runs of the running (or last) benchmark that failed although the benchmark went on. */
  failures: BenchmarkFailure[];
  /** Last finished run, if it failed. */
  error?: string;
  /** The newest benchmark_started is older than a benchmark may take: the node lost it (restart). */
  stale?: boolean;
}

/** The node ends a benchmark after 2 h (background.go); a "running" mark older than this is stale. */
export const BENCHMARK_STALE_MS = 2.5 * 60 * 60 * 1000;

function str(v: unknown): string | undefined {
  return typeof v === "string" ? v : undefined;
}

function num(v: unknown): number | null {
  return typeof v === "number" && Number.isFinite(v) ? v : null;
}

/** Latest download_progress per model, newest first. */
export function downloadsFromEvents(events: NodeEvent[]): DownloadInfo[] {
  const seen = new Map<string, DownloadInfo>();
  for (const e of events) {
    if (e.kind !== "download_progress") continue;
    const model = str(e.data.model);
    const status = str(e.data.status);
    if (!model || seen.has(model)) continue;
    if (status !== "running" && status !== "done" && status !== "failed") continue;
    const info: DownloadInfo = {
      model,
      status,
      downloadedBytes: num(e.data.downloaded_bytes),
      totalBytes: num(e.data.total_bytes),
      ts: e.ts,
    };
    const error = str(e.data.error);
    if (error) info.error = error;
    seen.set(model, info);
  }
  return [...seen.values()];
}

/** Whether a benchmark is running, from the newest benchmark_started/finished event. */
export function benchmarkFromEvents(events: NodeEvent[], now?: number): BenchmarkActivity {
  let results = 0;
  let finished: BenchmarkActivity | null = null;
  const failures: BenchmarkFailure[] = [];
  const idOf = (e: NodeEvent) => str(e.data.benchmark_id) ?? null;
  for (const e of events) {
    if (finished && idOf(e) !== finished.benchmarkId) {
      // events of an older run: the last run's are complete
      if (e.kind === "benchmark_started" || e.kind === "benchmark_finished") break;
      continue;
    }
    if (e.kind === "benchmark_result") {
      results += 1;
    } else if (e.kind === "benchmark_error") {
      failures.unshift({
        model: str(e.data.model) ?? "",
        backend: str(e.data.backend) ?? "",
        error: str(e.data.error) ?? "",
      });
    } else if (e.kind === "benchmark_finished" && !finished) {
      finished = { running: false, benchmarkId: idOf(e), results: 0, failures };
      if (e.data.ok === false) finished.error = str(e.data.error) ?? "";
    } else if (e.kind === "benchmark_started") {
      if (finished) break;
      if (now !== undefined && now - Date.parse(e.ts) > BENCHMARK_STALE_MS) {
        return { running: false, benchmarkId: idOf(e), results, failures, stale: true };
      }
      return { running: true, benchmarkId: idOf(e), results, failures };
    }
  }
  return finished ?? { running: false, benchmarkId: null, results: 0, failures: [] };
}

/** Id of the newest benchmark_result/benchmark_finished event (triggers a refetch). */
export function latestBenchmarkEventId(events: NodeEvent[]): number | null {
  const e = events.find(
    (ev) =>
      ev.kind === "benchmark_result" ||
      ev.kind === "benchmark_error" ||
      ev.kind === "benchmark_finished",
  );
  return e ? e.id : null;
}

/** Id of the newest config_changed event: the config in the open detail is stale then. */
export function latestConfigEventId(events: NodeEvent[]): number | null {
  const e = events.find((ev) => ev.kind === "config_changed");
  return e ? e.id : null;
}

/** Short German-catalogue key describing an event beyond its kind, if there is one. */
export function eventDetailKey(e: NodeEvent): string | null {
  const reason = str(e.data.reason);
  if ((e.kind === "model_loaded" || e.kind === "model_unloaded") && reason) {
    return `eventReason.${reason}`;
  }
  const source = str(e.data.source);
  if (e.kind === "config_changed" && source) {
    if (source === "residents") {
      return e.data.resident ? "configSource.residentOn" : "configSource.residentOff";
    }
    return `configSource.${source.startsWith("migration") ? "migration" : source}`;
  }
  return null;
}

/**
 * The readable text of a failed / retried load or a late action result: the node's own
 * message (it names the allocation that failed), with the model and the plan that follows.
 */
export function loadEventText(e: NodeEvent): string | null {
  const model = str(e.data.model);
  const withModel = (text: string | undefined): string | null =>
    text ? (model ? `${model}: ${text}` : text) : null;
  switch (e.kind) {
    case "model_load_failed":
      return withModel(str(e.data.message));
    case "model_load_retry": {
      const plan = str(e.data.plan);
      const error = str(e.data.error);
      return withModel(plan ? (error ? `${plan} – ${error}` : plan) : error);
    }
    case "action_late_result": {
      const name = str(e.data.name);
      const err = e.data.error as { message?: unknown } | null | undefined;
      const message = err && typeof err.message === "string" ? err.message : undefined;
      if (!name) return message ?? null;
      return `${name}: ${message ?? "ok"}`;
    }
    case "update_failed": {
      const version = str(e.data.version);
      const reason = str(e.data.reason);
      return reason ? (version ? `${version}: ${reason}` : reason) : null;
    }
    case "update_staging":
    case "update_staged":
    case "update_applied":
      return str(e.data.version) ?? null;
    default:
      return null;
  }
}

/** Key of the fastest generation (tg) row per model, for highlighting. */
export function rowKey(row: BenchmarkRow): string {
  return `${row.benchmark_id}|${row.model}|${row.backend}|${row.threads}|${row.ts}`;
}

export function bestTgPerModel(rows: BenchmarkRow[]): Set<string> {
  const best = new Map<string, BenchmarkRow>();
  for (const r of rows) {
    const cur = best.get(r.model);
    if (!cur || r.tg_tps > cur.tg_tps) best.set(r.model, r);
  }
  return new Set([...best.values()].map(rowKey));
}
