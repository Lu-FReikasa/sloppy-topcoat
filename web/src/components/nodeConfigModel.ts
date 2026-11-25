/**
 * The node configuration as a form: pure conversion between the stored config and the form
 * state. Keys the form does not show are kept as they are, so saving never drops them.
 */

export type Backend = "auto" | "cuda" | "vulkan" | "cpu";
export type MemoryMode = "refuse" | "warn" | "off";
export type LayersMode = "fit" | "all" | "fixed";

/** Context lengths the slider steps through (powers of two, the node's own range). */
export const CONTEXT_STEPS = [
  512, 1024, 2048, 4096, 8192, 16384, 32768, 65536, 131072, 262144, 524288, 1048576,
];

/** GPU layers "all" (llama-server -ngl 999, as the node's own benchmark uses). */
export const ALL_LAYERS = 999;

export interface ContextEntry {
  model: string;
  length: number;
}

export interface BackendEntry {
  model: string;
  /** "" = the node's backend */
  backend: "" | "cuda" | "vulkan" | "cpu";
  layersMode: LayersMode;
  layers: number;
  /** null = off; a number = the expert weights of that many first layers stay in RAM */
  moeLayers: number | null;
  noHost: boolean;
}

export interface WatcherRule {
  kind: "process" | "path";
  value: string;
}

export interface NodeForm {
  backend: Backend;
  llamacppRelease: string;
  /** null = automatic (the node's own choice from its benchmark) */
  threads: number | null;
  /** null = the node's built-in default */
  defaultContext: number | null;
  /** null = the node's built-in maximum */
  maxContext: number | null;
  jinja: boolean;
  contexts: ContextEntry[];
  parallel: number;
  memoryMode: MemoryMode;
  /** 0 = automatic: the larger of 4096 MB and 12 % of the RAM */
  memoryReserveMb: number;
  backends: BackendEntry[];
  graceSeconds: number;
  cooldownSeconds: number;
  rules: WatcherRule[];
}

type Json = Record<string, unknown>;

function obj(value: unknown): Json {
  return value !== null && typeof value === "object" && !Array.isArray(value)
    ? (value as Json)
    : {};
}

function num(value: unknown): number | null {
  return typeof value === "number" && Number.isFinite(value) ? value : null;
}

const BACKENDS: Backend[] = ["auto", "cuda", "vulkan", "cpu"];

/** The form state for a stored config. Missing values take the node's documented defaults. */
export function formFromConfig(config: Json | null): NodeForm {
  const runner = obj(config?.runner);
  const watcher = obj(config?.watcher);
  const guard = obj(runner.memory_guard);
  const backend = BACKENDS.includes(runner.backend as Backend) ? (runner.backend as Backend) : "auto";
  const mode = guard.mode;
  return {
    backend,
    llamacppRelease: typeof runner.llamacpp_release === "string" ? runner.llamacpp_release : "",
    threads: num(runner.threads),
    defaultContext: num(runner.default_context_length),
    maxContext: num(runner.max_context_length),
    jinja: runner.jinja !== false,
    contexts: Object.entries(obj(runner.model_context_lengths)).map(([model, length]) => ({
      model,
      length: Number(length),
    })),
    parallel: num(runner.n_parallel) ?? 1,
    memoryMode: mode === "refuse" || mode === "warn" || mode === "off" ? mode : "refuse",
    memoryReserveMb: num(guard.reserve_mb) ?? 0,
    backends: Object.entries(obj(runner.model_backends)).map(([model, value]) => {
      const entry = obj(value);
      const layers = num(entry.gpu_layers);
      const layersMode: LayersMode =
        layers === null ? "fit" : layers >= ALL_LAYERS ? "all" : "fixed";
      return {
        model,
        backend:
          entry.backend === "cuda" || entry.backend === "vulkan" || entry.backend === "cpu"
            ? entry.backend
            : "",
        layersMode,
        layers: layers ?? 0,
        moeLayers: num(entry.n_cpu_moe),
        noHost: entry.no_host === true,
      };
    }),
    graceSeconds: num(watcher.grace_seconds) ?? 5,
    cooldownSeconds: num(watcher.cooldown_seconds) ?? 60,
    rules: (Array.isArray(watcher.rules) ? watcher.rules : []).flatMap((rule): WatcherRule[] => {
      const r = obj(rule);
      if (typeof r.process === "string") return [{ kind: "process", value: r.process }];
      if (typeof r.path_prefix === "string") return [{ kind: "path", value: r.path_prefix }];
      return [];
    }),
  };
}

/** The stored config with the form's values written in. Other keys are kept as they are. */
export function applyForm(config: Json, form: NodeForm): Json {
  const runner: Json = { ...obj(config.runner) };
  runner.backend = form.backend;
  setOrDelete(runner, "llamacpp_release", form.llamacppRelease.trim() || null);
  setOrDelete(runner, "threads", form.threads);
  setOrDelete(runner, "default_context_length", form.defaultContext);
  setOrDelete(runner, "max_context_length", form.maxContext);
  runner.jinja = form.jinja;
  runner.n_parallel = form.parallel;
  runner.model_context_lengths = Object.fromEntries(
    form.contexts.filter((c) => c.model.trim()).map((c) => [c.model.trim(), c.length]),
  );
  const guard: Json = { ...obj(runner.memory_guard), mode: form.memoryMode };
  setOrDelete(guard, "reserve_mb", form.memoryReserveMb > 0 ? form.memoryReserveMb : null);
  runner.memory_guard = guard;
  runner.model_backends = Object.fromEntries(
    form.backends
      .filter((b) => b.model.trim())
      .map((b) => [b.model.trim(), backendEntry(b)] as const)
      .filter(([, entry]) => Object.keys(entry).length > 0),
  );

  const watcher: Json = { ...obj(config.watcher) };
  watcher.grace_seconds = form.graceSeconds;
  watcher.cooldown_seconds = form.cooldownSeconds;
  watcher.rules = form.rules
    .filter((r) => r.value.trim())
    .map((r) => (r.kind === "process" ? { process: r.value.trim() } : { path_prefix: r.value.trim() }));

  return { ...config, runner, watcher };
}

function backendEntry(b: BackendEntry): Json {
  const entry: Json = {};
  if (b.backend) entry.backend = b.backend;
  if (b.layersMode === "all") entry.gpu_layers = ALL_LAYERS;
  if (b.layersMode === "fixed") entry.gpu_layers = b.layers;
  if (b.moeLayers !== null) entry.n_cpu_moe = b.moeLayers;
  if (b.noHost) entry.no_host = true;
  return entry;
}

function setOrDelete(target: Json, key: string, value: unknown): void {
  if (value === null || value === undefined) delete target[key];
  else target[key] = value;
}

/**
 * Problems the form can see before the hub does. An empty list means the form may be saved;
 * the hub still validates against the schema.
 */
export function formProblems(form: NodeForm): string[] {
  const problems: string[] = [];
  if (form.llamacppRelease.trim() && !/^b[0-9]+$/.test(form.llamacppRelease.trim())) {
    problems.push("release");
  }
  const names = [...form.contexts.map((c) => c.model.trim()), ...form.backends.map((b) => b.model.trim())];
  const seen = new Set<string>();
  for (const name of names) {
    if (!name) continue;
    if (seen.has(name)) problems.push(`duplicate:${name}`);
    seen.add(name);
  }
  for (const b of form.backends) {
    if (!b.model.trim()) problems.push("emptyModel");
  }
  for (const r of form.rules) {
    if (!r.value.trim()) problems.push("emptyRule");
  }
  return problems;
}

/**
 * True when the change makes the node restart its runner (the models are unloaded and loaded
 * again): the backend, the placement per model or the memory guard changed.
 */
export function restartNeeded(before: Json | null, after: Json): boolean {
  return placementKey(obj(before?.runner)) !== placementKey(obj(after.runner));
}

/** What a runner restart depends on, with the node's defaults for what is missing. */
function placementKey(runner: Json): string {
  const guard = obj(runner.memory_guard);
  return JSON.stringify([
    runner.backend ?? "auto",
    obj(runner.model_backends),
    guard.mode ?? "refuse",
    guard.reserve_mb ?? null,
  ]);
}
