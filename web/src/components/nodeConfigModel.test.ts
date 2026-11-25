import { describe, expect, it } from "vitest";
import { applyForm, formFromConfig, formProblems, restartNeeded } from "./nodeConfigModel";

const CONFIG = {
  roles: ["gpu"],
  allowed_actions: ["models.load"],
  heartbeat_interval_seconds: 30,
  idle_ttl_seconds: 0,
  capabilities: [],
  runner: {
    type: "llamaserver",
    backend: "auto",
    model_dirs: ["D:\\models"],
    memory_guard: { mode: "warn" },
    model_context_lengths: { "Qwen2.5-7B": 32768 },
  },
  watcher: { grace_seconds: 5, cooldown_seconds: 60, rules: [{ process: "league.exe" }] },
};

describe("formFromConfig", () => {
  it("takes the documented defaults for what the config leaves out", () => {
    const form = formFromConfig({ runner: { type: "llamaserver" }, watcher: { rules: [] } });
    expect(form.backend).toBe("auto");
    expect(form.threads).toBeNull();
    expect(form.jinja).toBe(true);
    expect(form.parallel).toBe(1);
    expect(form.memoryMode).toBe("refuse");
    expect(form.memoryReserveMb).toBe(0);
    expect(form.backends).toEqual([]);
  });

  it("reads the rules and the per-model placement", () => {
    const form = formFromConfig({
      ...CONFIG,
      runner: {
        ...CONFIG.runner,
        model_backends: {
          "Qwen3-30B": { backend: "cpu", gpu_layers: 0 },
          "gemma-12B": { gpu_layers: 999, n_cpu_moe: 12 },
        },
      },
      watcher: { rules: [{ process: "league.exe" }, { path_prefix: "D:\\SteamLibrary\\" }] },
    });
    expect(form.rules).toEqual([
      { kind: "process", value: "league.exe" },
      { kind: "path", value: "D:\\SteamLibrary\\" },
    ]);
    expect(form.backends).toEqual([
      { model: "Qwen3-30B", backend: "cpu", layersMode: "fixed", layers: 0, moeLayers: null, noHost: false },
      { model: "gemma-12B", backend: "", layersMode: "all", layers: 999, moeLayers: 12, noHost: false },
    ]);
  });
});

describe("applyForm", () => {
  it("keeps every key the form does not show", () => {
    const form = formFromConfig(CONFIG);
    const next = applyForm(CONFIG, form);
    expect(next.roles).toEqual(["gpu"]);
    expect(next.allowed_actions).toEqual(["models.load"]);
    expect((next.runner as Record<string, unknown>).type).toBe("llamaserver");
    expect((next.runner as Record<string, unknown>).model_dirs).toEqual(["D:\\models"]);
  });

  it("writes a changed backend and leaves an automatic threads value out", () => {
    const form = { ...formFromConfig(CONFIG), backend: "vulkan" as const };
    const runner = applyForm(CONFIG, form).runner as Record<string, unknown>;
    expect(runner.backend).toBe("vulkan");
    expect("threads" in runner).toBe(false);
  });

  it("writes the placement per model: all layers as 999, fit as no gpu_layers", () => {
    const form = formFromConfig(CONFIG);
    form.backends = [
      { model: "a", backend: "vulkan", layersMode: "all", layers: 0, moeLayers: null, noHost: false },
      { model: "b", backend: "", layersMode: "fit", layers: 0, moeLayers: 8, noHost: true },
    ];
    const runner = applyForm(CONFIG, form).runner as Record<string, unknown>;
    expect(runner.model_backends).toEqual({
      a: { backend: "vulkan", gpu_layers: 999 },
      b: { n_cpu_moe: 8, no_host: true },
    });
  });

  it("drops a per-model entry that has nothing to say", () => {
    const form = formFromConfig(CONFIG);
    form.backends = [
      { model: "empty", backend: "", layersMode: "fit", layers: 0, moeLayers: null, noHost: false },
    ];
    const runner = applyForm(CONFIG, form).runner as Record<string, unknown>;
    expect(runner.model_backends).toEqual({});
  });

  it("removes reserve_mb when the reserve is automatic", () => {
    const withReserve = { ...CONFIG, runner: { ...CONFIG.runner, memory_guard: { mode: "warn", reserve_mb: 8192 } } };
    const auto = { ...formFromConfig(withReserve), memoryReserveMb: 0 };
    const guard = (applyForm(withReserve, auto).runner as Record<string, unknown>).memory_guard as Record<string, unknown>;
    expect(guard).toEqual({ mode: "warn" });
  });

  it("writes the watched programs back in their two forms", () => {
    const form = formFromConfig(CONFIG);
    form.rules = [
      { kind: "process", value: " spiel.exe " },
      { kind: "path", value: "C:\\Games\\" },
      { kind: "path", value: "   " },
    ];
    const watcher = applyForm(CONFIG, form).watcher as Record<string, unknown>;
    expect(watcher.rules).toEqual([{ process: "spiel.exe" }, { path_prefix: "C:\\Games\\" }]);
  });
});

describe("formProblems", () => {
  it("accepts an empty form and a release of the form bNNNN", () => {
    const form = formFromConfig(CONFIG);
    expect(formProblems(form)).toEqual([]);
    expect(formProblems({ ...form, llamacppRelease: "b11223" })).toEqual([]);
  });

  it("flags a malformed release, a model named twice and an empty rule", () => {
    const form = formFromConfig(CONFIG);
    form.llamacppRelease = "11223";
    form.rules = [{ kind: "process", value: "" }];
    form.backends = [
      { model: "same", backend: "", layersMode: "fit", layers: 0, moeLayers: null, noHost: false },
      { model: "same", backend: "cpu", layersMode: "fit", layers: 0, moeLayers: null, noHost: false },
    ];
    expect(formProblems(form)).toEqual(["release", "duplicate:same", "emptyRule"]);
  });
});

describe("restartNeeded", () => {
  it("is true for the backend, the placement or the memory guard", () => {
    const before = CONFIG;
    expect(restartNeeded(before, applyForm(CONFIG, { ...formFromConfig(CONFIG), backend: "cpu" }))).toBe(true);
    expect(
      restartNeeded(
        before,
        applyForm(CONFIG, {
          ...formFromConfig(CONFIG),
          memoryMode: "off",
        }),
      ),
    ).toBe(true);
  });

  it("is false for a change that the runner picks up without a restart", () => {
    expect(restartNeeded(CONFIG, applyForm(CONFIG, { ...formFromConfig(CONFIG), threads: 8 }))).toBe(false);
  });
});
