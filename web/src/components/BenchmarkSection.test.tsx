import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { ApiClient } from "../api/client";
import { makeDetail } from "../test/fixtures";
import { jsonResponse, mockFetch } from "../test/mockFetch";
import type { BenchmarkRow, NodeDetail } from "../types";
import { BenchmarkSection } from "./BenchmarkSection";

function row(overrides: Partial<BenchmarkRow>): BenchmarkRow {
  return {
    benchmark_id: "b1",
    model: "qwen-7b",
    backend: "cpu",
    threads: 2,
    n_gpu_layers: 0,
    pp_tps: 85.5,
    tg_tps: 14.2,
    ts: "2026-01-01T12:00:00Z",
    ...overrides,
  };
}

function setup(detail: NodeDetail, actionRoute: unknown = { ok: true, result: { started: true } }) {
  const mock = mockFetch({ "POST /api/nodes/n1/actions": actionRoute });
  const api = new ApiClient({ token: "t", fetchImpl: mock.fetch });
  render(<BenchmarkSection api={api} node={detail} />);
  return mock;
}

describe("BenchmarkSection", () => {
  it("renders results and highlights the fastest generation per model", () => {
    setup(
      makeDetail({
        benchmarks: [
          row({ threads: 12, tg_tps: 18.4, ts: "2026-01-01T12:02:00Z" }),
          row({ backend: "vulkan", threads: 6, n_gpu_layers: 99, pp_tps: 400, tg_tps: 30.25 }),
          row({}),
        ],
      }),
    );
    const table = screen.getByRole("table");
    expect(within(table).getByText("Generierung t/s (tg128)")).toBeTruthy();
    expect(within(table).getByText("Prompt t/s (pp512)")).toBeTruthy();
    expect(within(table).getByText("Vulkan")).toBeTruthy();
    expect(within(table).getByText("400,0")).toBeTruthy();
    const best = table.querySelectorAll("tr.best");
    expect(best).toHaveLength(1);
    expect(best[0]?.textContent).toContain("30,3");
    expect(within(best[0] as HTMLElement).getByRole("img", { name: /Schnellste/ })).toBeTruthy();
  });

  it("shows an empty hint without results", () => {
    setup(makeDetail());
    expect(screen.getByText("Noch keine Benchmark-Ergebnisse.")).toBeTruthy();
  });

  it("starts a benchmark with the selected models", async () => {
    const mock = setup(makeDetail());
    fireEvent.click(screen.getByRole("button", { name: "Benchmark starten" }));
    const dialog = screen.getByRole("dialog");
    const run = within(dialog).getByRole("button", { name: "Starten" }) as HTMLButtonElement;
    expect(run.disabled).toBe(true);
    expect(within(dialog).getByText("Mindestens ein Modell auswählen.")).toBeTruthy();
    fireEvent.click(within(dialog).getByRole("checkbox", { name: "llama-8b" }));
    fireEvent.click(within(dialog).getByRole("checkbox", { name: "qwen-7b" }));
    fireEvent.click(within(dialog).getByRole("checkbox", { name: "Schnell" }));
    expect(run.disabled).toBe(false);
    fireEvent.click(run);
    await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
    expect(mock.calls[0]?.body).toEqual({
      name: "node.benchmark",
      args: { models: ["qwen-7b", "llama-8b"], quick: true },
    });
  });

  it("shows a node error inside the dialog", async () => {
    setup(makeDetail(), {
      ok: false,
      error: { code: "busy", message: "benchmark already running" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Benchmark starten" }));
    fireEvent.click(screen.getByRole("checkbox", { name: "qwen-7b" }));
    fireEvent.click(screen.getByRole("button", { name: "Starten" }));
    expect((await screen.findByRole("alert")).textContent).toContain("benchmark already running");
    expect(screen.getByRole("dialog")).toBeTruthy();
  });

  it("shows the busy text when the hub itself answers 429 busy", async () => {
    setup(
      makeDetail(),
      jsonResponse({ detail: { code: "busy", message: "too many runs are going" } }, 429),
    );
    fireEvent.click(screen.getByRole("button", { name: "Benchmark starten" }));
    fireEvent.click(screen.getByRole("checkbox", { name: "qwen-7b" }));
    fireEvent.click(screen.getByRole("button", { name: "Starten" }));
    const alert = await screen.findByRole("alert");
    expect(alert.textContent).toContain("Alle Plätze für deine Programme sind belegt");
    expect(alert.textContent).not.toContain("too many runs");
    expect(screen.getByRole("dialog")).toBeTruthy();
  });

  it("is disabled while gaming or offline, with a hint", () => {
    const { unmount } = render(
      <BenchmarkSection
        api={new ApiClient({ token: "t" })}
        node={makeDetail({ state: "busy_gaming" })}
      />,
    );
    expect(
      (screen.getByRole("button", { name: "Benchmark starten" }) as HTMLButtonElement).disabled,
    ).toBe(true);
    expect(screen.getByText(/läuft ein Spiel/)).toBeTruthy();
    unmount();
    setup(makeDetail({ state: "offline", connected: false }));
    expect(
      (screen.getByRole("button", { name: "Benchmark starten" }) as HTMLButtonElement).disabled,
    ).toBe(true);
    expect(screen.getByText(/offline – kein Benchmark/)).toBeTruthy();
  });

  it("shows the running state from live events", () => {
    const events = [
      { id: 3, kind: "benchmark_result", data: {}, ts: "2026-01-01T12:00:02Z", received_at: "" },
      {
        id: 2,
        kind: "benchmark_started",
        data: { benchmark_id: "b2" },
        ts: new Date().toISOString(),
        received_at: "",
      },
    ];
    setup(makeDetail({ events }));
    expect(screen.getByRole("status").textContent).toBe("Benchmark läuft … (1 Messungen bisher)");
    expect(
      (screen.getByRole("button", { name: "Benchmark starten" }) as HTMLButtonElement).disabled,
    ).toBe(true);
  });

  it("reports an aborted run", () => {
    const events = [
      {
        id: 4,
        kind: "benchmark_finished",
        data: { benchmark_id: "b2", ok: false, error: "aborted: game detected" },
        ts: "2026-01-01T12:00:03Z",
        received_at: "",
      },
    ];
    setup(makeDetail({ events }));
    expect(
      screen.getByText("Letzter Benchmark fehlgeschlagen: aborted: game detected"),
    ).toBeTruthy();
    expect(
      (screen.getByRole("button", { name: "Benchmark starten" }) as HTMLButtonElement).disabled,
    ).toBe(false);
  });
  it("lists a failed backend although the other measurements went on", () => {
    const ts = "2026-01-01T12:00:03Z";
    const events = [
      {
        id: 6,
        kind: "benchmark_finished",
        data: { benchmark_id: "b3", ok: true, failed_runs: 1 },
        ts,
        received_at: "",
      },
      { id: 5, kind: "benchmark_result", data: { benchmark_id: "b3" }, ts, received_at: "" },
      {
        id: 4,
        kind: "benchmark_error",
        data: {
          benchmark_id: "b3",
          model: "qwen3-30b",
          backend: "vulkan",
          error: "ErrorOutOfDeviceMemory",
        },
        ts,
        received_at: "",
      },
      { id: 3, kind: "benchmark_started", data: { benchmark_id: "b3" }, ts, received_at: "" },
    ];
    setup(makeDetail({ events }));
    const list = screen.getByTestId("benchmark-failures");
    expect(list.textContent).toContain(
      "qwen3-30b mit vulkan ist fehlgeschlagen, die übrigen Messungen liefen weiter: ErrorOutOfDeviceMemory",
    );
    expect(screen.queryByText(/Letzter Benchmark fehlgeschlagen/)).toBeNull();
  });
});
