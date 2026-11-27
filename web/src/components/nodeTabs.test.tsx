import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { ApiClient } from "../api/client";
import { createHubEvents } from "../data/hubEvents";
import { makeDetail } from "../test/fixtures";
import { mockFetch } from "../test/mockFetch";
import type { BenchmarkRow, ModelInfo, NodeDetail, NodeEvent } from "../types";
import { NodeDetailView } from "./NodeDetail";
import { HISTORY_SHOWN, pickTab, splitModels, TAB_KEY } from "./nodeTabs";

// fixture figures
const DISK_TOTAL_MB = 953_000;
const DISK_FREE_MB = 412_000;
const EVENT_COUNT = 14;
const BENCH_COUNT = 12;
const TPS = 40;
const THREADS = 8;

afterEach(() => {
  vi.restoreAllMocks();
  window.localStorage.clear();
});

const model = (name: string, loaded = false): ModelInfo => ({
  name,
  loaded,
  vram_used_mb: null,
});

describe("pickTab", () => {
  it("opens the remembered tab when the node has it, else the default", () => {
    expect(pickTab(null, false)).toBe("models");
    expect(pickTab("maintenance", false)).toBe("maintenance");
    expect(pickTab("bogus", false)).toBe("models");
    // a tool node has no Models tab: it opens on what it offers
    expect(pickTab(null, true)).toBe("rights");
    expect(pickTab("models", true)).toBe("rights");
    expect(pickTab("history", true)).toBe("history");
  });
});

describe("splitModels", () => {
  const models = [model("b-rest"), model("a-kept"), model("c-loaded", true), model("d-rest")];

  it("puts loaded, then kept-ready models first and keeps the node's order otherwise", () => {
    const { top, rest, hidden } = splitModels(models, new Set(["a-kept"]), "");
    expect(top.map((m) => m.name)).toEqual(["c-loaded", "a-kept"]);
    expect(rest.map((m) => m.name)).toEqual(["b-rest", "d-rest"]);
    expect(hidden).toBe(0);
  });

  it("filters only the rest, ignoring case", () => {
    const { top, rest, hidden } = splitModels(models, new Set(["a-kept"]), " D-R ");
    expect(top.map((m) => m.name)).toEqual(["c-loaded", "a-kept"]);
    expect(rest.map((m) => m.name)).toEqual(["d-rest"]);
    expect(hidden).toBe(1);
  });
});

/** A compute node with every section of the page filled. */
function fullNode(): NodeDetail {
  const base = makeDetail();
  return makeDetail({
    disk_total_mb: DISK_TOTAL_MB,
    disk_free_mb: DISK_FREE_MB,
    network: [{ name: "eth0", mac: "02:00:00:00:00:01", kind: "ethernet" }],
    update: {
      channel: "stable",
      capable: true,
      updater: "ok",
      key_ids: [],
      available: "0.2.19",
      newer: true,
      action_allowed: true,
    },
    config: {
      ...(base.config ?? {}),
      roles: ["gpu"],
      capabilities: ["files.read"],
      residents: [{ model: "gone-model" }],
    },
  });
}

function setup(detail: NodeDetail) {
  const mock = mockFetch({
    "GET /api/nodes/n1": detail,
    "GET /api/nodes/n1/capabilities": {
      node: { id: "n1", name: "node-nvidia", roles: ["gpu"] },
      offered: [
        { name: "files.read", description: "Read files", read_only: true, input_schema: {} },
      ],
      enabled: ["files.read"],
      usable: ["files.read"],
      grants: [],
    },
    "GET /api/tool-calls?node_id=n1": [],
    "GET /api/model-transfers?node_id=n1": {
      transfers: [
        {
          id: "t1",
          user_id: "u1",
          source_node_id: "n1",
          target_node_id: "n2",
          model: "qwen-7b",
          target_name: null,
          size: null,
          sha256: null,
          move: false,
          status: "pending",
          error_code: null,
          error: null,
          created_at: "2026-01-01T12:00:00Z",
          updated_at: "2026-01-01T12:00:00Z",
        },
      ],
    },
    "GET /api/nodes": [],
  });
  const api = new ApiClient({ token: "t", fetchImpl: mock.fetch });
  render(<NodeDetailView api={api} nodeId="n1" events={createHubEvents()} />);
  return mock;
}

/** The panels that hold an element matching `find`. */
function panelsWith(find: (panel: HTMLElement) => boolean): string[] {
  return [...document.querySelectorAll<HTMLElement>("[role=tabpanel]")]
    .filter(find)
    .map((p) => p.id.replace("node-panel-", ""));
}

const heading = (text: string) => (panel: HTMLElement) =>
  [...panel.querySelectorAll("h2, h3")].some((h) => h.textContent === text);

describe("node page tabs", () => {
  it("puts every section of the page in exactly one tab, rare facts in a closed fold", async () => {
    setup(fullNode());
    await screen.findByTestId("node-header");
    // the transfer list and the capabilities arrive after the detail
    await waitFor(() => expect(panelsWith(heading("Modell-Übertragungen"))).toHaveLength(1));
    await waitFor(() => expect(panelsWith(heading("Fähigkeiten"))).toHaveLength(1));

    const expected: [string, (panel: HTMLElement) => boolean, string][] = [
      ["GPUs", heading("GPUs"), "models"],
      [
        "models table",
        (p) => p.querySelector("table.models:not(.benchmarks):not(.capabilities)") !== null,
        "models",
      ],
      ["download", (p) => within(p).queryByText("Modell herunterladen") !== null, "models"],
      ["missing residents", (p) => p.querySelector(".missing-residents") !== null, "models"],
      ["transfers", heading("Modell-Übertragungen"), "history"],
      ["benchmarks", heading("Benchmark"), "history"],
      ["events", heading("Letzte Ereignisse"), "history"],
      ["compute switch", (p) => p.querySelector(".node-kind") !== null, "rights"],
      ["capabilities", heading("Fähigkeiten"), "rights"],
      ["file read share", heading("Dateien auf diesem Rechner lesen"), "rights"],
      ["storage", heading("Speicherplatz"), "maintenance"],
      ["pressure", heading("Speicher freigeben"), "maintenance"],
      ["consideration", heading("Rücksicht auf lokale Nutzung"), "maintenance"],
      ["energy", heading("Energieprofil"), "maintenance"],
      ["config", heading("Konfiguration"), "maintenance"],
      ["update", heading("Update"), "maintenance"],
      ["rename", heading("Name"), "maintenance"],
    ];
    expect(expected.map(([what, find]) => [what, panelsWith(find)])).toEqual(
      expected.map(([what, , tab]) => [what, [tab]]),
    );

    // MACs, llama.cpp build and ports: one closed fold outside the tabs
    const rare = screen.getByTestId("node-rare-facts") as HTMLDetailsElement;
    expect(rare.open).toBe(false);
    expect(rare.closest("[role=tabpanel]")).toBeNull();
    expect(within(rare).getByTestId("node-network").textContent).toContain("02:00:00:00:00:01");
    expect(rare.textContent).toContain("llama.cpp");
    expect(rare.textContent).toContain("llama-8b"); // the loaded model's process details
  });

  it("shows one tab at a time and remembers the choice for the next node page", async () => {
    setup(fullNode());
    await screen.findByTestId("node-header");
    expect(screen.getByRole("tab", { name: "Modelle" }).getAttribute("aria-selected")).toBe("true");
    expect(screen.getByTestId("node-panel-maintenance").hidden).toBe(true);
    fireEvent.click(screen.getByRole("tab", { name: "Wartung" }));
    expect(screen.getByTestId("node-panel-maintenance").hidden).toBe(false);
    expect(screen.getByTestId("node-panel-models").hidden).toBe(true);
    expect(window.localStorage.getItem(TAB_KEY)).toBe("maintenance");

    cleanup();
    setup(fullNode());
    await screen.findByTestId("node-header");
    expect(screen.getByRole("tab", { name: "Wartung" }).getAttribute("aria-selected")).toBe("true");
  });

  it("keeps loaded models on top, filters the rest and loads through a row menu", async () => {
    const mock = setup(
      makeDetail({
        models: [model("zeta-3b"), model("alpha-1b"), model("llama-8b", true)],
        config: { roles: ["gpu"] },
      }),
    );
    await screen.findByTestId("node-header");
    const panel = screen.getByTestId("node-panel-models");
    const names = () =>
      [...panel.querySelectorAll("tbody td.model-name")].map((td) => td.textContent);
    expect(names()).toEqual(["llama-8b", "zeta-3b", "alpha-1b"]);
    fireEvent.change(within(panel).getByRole("searchbox", { name: "Modelle filtern" }), {
      target: { value: "alp" },
    });
    expect(names()).toEqual(["llama-8b", "alpha-1b"]);
    expect(within(panel).getByTestId("node-models-hidden").textContent).toContain("1");

    const menu = within(panel).getByLabelText("Aktionen für alpha-1b").closest("details");
    if (!menu) throw new Error("no row menu");
    fireEvent.click(within(menu).getByRole("button", { name: "Laden" }));
    await waitFor(() => expect(mock.calls.some((c) => c.method === "POST")).toBe(true));
    expect(mock.calls.find((c) => c.method === "POST")?.body).toEqual({
      name: "models.load",
      args: { model: "alpha-1b" },
    });
  });

  it("closes the row menu after its action and on Escape, focus back on its button", async () => {
    setup(makeDetail({ models: [model("alpha-1b")], config: { roles: ["gpu"] } }));
    const summary = await screen.findByLabelText("Aktionen für alpha-1b");
    const menu = summary.closest("details");
    if (!menu) throw new Error("no row menu");
    menu.open = true;
    const load = within(menu).getByRole("button", { name: "Laden" });
    load.focus();
    const esc = fireEvent.keyDown(load, { key: "Escape" });
    expect(esc).toBe(false); // taken: the windows shell must not minimise the window
    expect(menu.open).toBe(false);
    expect(document.activeElement).toBe(summary);
    // closed, Escape is not the menu's
    expect(fireEvent.keyDown(summary, { key: "Escape" })).toBe(true);

    menu.open = true;
    fireEvent.click(within(menu).getByRole("button", { name: "Laden" }));
    expect(menu.open).toBe(false);
  });

  it("shows the last ten events and benchmarks until asked for more", async () => {
    const events: NodeEvent[] = Array.from({ length: EVENT_COUNT }, (_, i) => ({
      id: i + 1,
      kind: "connected",
      data: {},
      ts: "2026-01-01T12:00:00Z",
      received_at: "",
    }));
    const benchmarks: BenchmarkRow[] = Array.from({ length: BENCH_COUNT }, (_, i) => ({
      model: `m${i}`,
      backend: "cuda",
      threads: THREADS,
      n_gpu_layers: null,
      pp_tps: TPS,
      tg_tps: TPS,
      ts: "2026-01-01T12:00:00Z",
    })) as BenchmarkRow[];
    setup(makeDetail({ events, benchmarks }));
    await screen.findByTestId("node-header");
    const panel = screen.getByTestId("node-panel-history");
    expect(panel.querySelectorAll("ol.events > li")).toHaveLength(HISTORY_SHOWN);
    expect(panel.querySelectorAll("table.benchmarks tbody tr")).toHaveLength(HISTORY_SHOWN);
    fireEvent.click(screen.getByRole("tab", { name: "Verlauf" }));
    fireEvent.click(
      within(panel).getByRole("button", { name: `Mehr zeigen (${EVENT_COUNT - HISTORY_SHOWN})` }),
    );
    expect(panel.querySelectorAll("ol.events > li")).toHaveLength(EVENT_COUNT);
    fireEvent.click(
      within(panel).getByRole("button", { name: `Mehr zeigen (${BENCH_COUNT - HISTORY_SHOWN})` }),
    );
    expect(panel.querySelectorAll("table.benchmarks tbody tr")).toHaveLength(BENCH_COUNT);
  });
});
