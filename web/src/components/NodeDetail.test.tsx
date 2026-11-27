import { act, cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { ApiClient } from "../api/client";
import { createHubEvents, type HubEvents } from "../data/hubEvents";
import { makeDetail, makeNode } from "../test/fixtures";
import { jsonResponse, mockFetch } from "../test/mockFetch";
import type { NodeDetail, NodeEvent, NodeSummary } from "../types";
import { NodeDetailView } from "./NodeDetail";

// the stream as the page sees it (web/src/data/hubEvents.ts)
let bus: HubEvents = createHubEvents();

function nodeUpdated(node: NodeSummary) {
  bus.publish({ event: "node_updated", data: JSON.stringify(node) });
}

function nodeEvent(nodeId: string, event: NodeEvent) {
  bus.publish({ event: "node_event", data: JSON.stringify({ node_id: nodeId, event }) });
}

function Harness({ api, nodeId }: { api: ApiClient; nodeId: string }) {
  return <NodeDetailView api={api} nodeId={nodeId} events={bus} />;
}

afterEach(() => {
  vi.restoreAllMocks();
  bus = createHubEvents();
  window.localStorage.clear(); // the page remembers the last tab
});

/** The node page shows one tab at a time (issue #176). */
async function openTab(name: string) {
  fireEvent.click(await screen.findByRole("tab", { name }));
}

function setup(detail: NodeDetail, actionRoute?: unknown, extra: Record<string, unknown> = {}) {
  const routes: Record<string, unknown> = { "GET /api/nodes/n1": detail, ...extra };
  if (actionRoute !== undefined) routes["POST /api/nodes/n1/actions"] = actionRoute;
  const mock = mockFetch(routes);
  const api = new ApiClient({ token: "t", fetchImpl: mock.fetch });
  render(<Harness api={api} nodeId="n1" />);
  return mock;
}

describe("NodeDetailView", () => {
  it("renders node info, models and events", async () => {
    setup(makeDetail());
    expect(await screen.findByRole("heading", { name: "node-nvidia" })).toBeTruthy();
    expect(screen.getByText("Fake CPU (8 Kerne)")).toBeTruthy();
    expect(screen.getByText(/Fake \(Test\) – verfügbar/)).toBeTruthy();
    expect(screen.getByRole("link", { name: "qwen-7b" })).toBeTruthy();
    expect(screen.getByText("4,5 GB")).toBeTruthy();
    expect(screen.getByText("Modell geladen")).toBeTruthy();
    expect(screen.getByRole("button", { name: "Laden" })).toBeTruthy();
    expect(screen.getByRole("button", { name: "Entladen" })).toBeTruthy();
    // copy and move live on the Models page now
    expect(screen.queryByRole("button", { name: /Kopieren nach/ })).toBeNull();
    expect(screen.queryByRole("button", { name: /Verschieben nach/ })).toBeNull();
  });

  it("opens with state, memory, GPU and loaded models and folds the inventory", async () => {
    setup(
      makeDetail({
        network: [{ name: "eth0", mac: "02:00:00:00:00:01", kind: "ethernet" }],
      }),
    );
    const glance = await screen.findByTestId("node-glance");
    expect(screen.getByText("Verfügbar")).toBeTruthy();
    expect(glance.textContent).toContain("8,0 / 32,0 GB");
    expect(screen.getByTestId("glance-gpus").textContent).toBe("Fake GPU: 8,0 / 24,0 GB VRAM");
    expect(screen.getByTestId("glance-loaded").textContent).toBe("llama-8b (5,9 GB VRAM)");
    // the inventory is one click away, not gone
    const more = document.querySelector("details.facts-more") as HTMLDetailsElement;
    expect(more.open).toBe(false);
    expect(more.querySelector("summary")?.textContent).toBe(
      "Details: System, CPU, Netzwerk, Versionen",
    );
    expect(within(more).getByTestId("node-network").textContent).toBe("eth0 02:00:00:00:00:01");
    expect(within(more).getByText("Fake CPU (8 Kerne)")).toBeTruthy();
    expect(glance.textContent).not.toContain("02:00:00:00:00:01");
  });

  it("says when nothing is loaded and leaves the figures of an offline node out", async () => {
    setup(makeDetail({ loaded: [], models: [] }));
    expect((await screen.findByTestId("glance-loaded")).textContent).toBe("keins");
    cleanup();
    setup(makeDetail({ state: "offline", connected: false }));
    const glance = await screen.findByTestId("node-glance");
    expect(glance.textContent).toBe("RAM–");
  });

  it("shows the free space of the model drive and warns when it is low", async () => {
    setup(makeDetail({ disk_total_mb: 953_000, disk_free_mb: 412_000 }));
    // under Maintenance; the header stays quiet while there is room
    const disk = await screen.findByTestId("node-disk-storage");
    expect(disk.textContent).toBe("402,3 GB frei von 930,7 GB");
    expect(disk.className).toBe("");
    expect(screen.queryByTestId("node-disk")).toBeNull();
    cleanup();
    setup(makeDetail({ disk_total_mb: 953_000, disk_free_mb: 15_000 }));
    const low = await screen.findByTestId("node-disk");
    expect(low.textContent).toContain("knapp: ein Download kann abgelehnt werden");
    expect(low.className).toBe("warn-text");
    expect(screen.getByTestId("node-disk-storage").className).toBe("warn-text");
    cleanup();
    setup(makeDetail()); // nothing reported: no row, Maintenance says so
    await screen.findByRole("heading", { name: "node-nvidia" });
    expect(screen.queryByTestId("node-disk")).toBeNull();
    expect(screen.queryByTestId("node-disk-storage")).toBeNull();
  });

  it("posts models.load when clicking Laden", async () => {
    const mock = setup(makeDetail(), { ok: true });
    fireEvent.click(await screen.findByRole("button", { name: "Laden" }));
    await waitFor(() => expect(mock.calls.some((c) => c.method === "POST")).toBe(true));
    const post = mock.calls.find((c) => c.method === "POST");
    expect(post?.url).toBe("/api/nodes/n1/actions");
    expect(post?.body).toEqual({ name: "models.load", args: { model: "qwen-7b" } });
    expect(post?.headers.authorization).toBe("Bearer t");
  });

  it("posts models.unload when clicking Entladen", async () => {
    const mock = setup(makeDetail(), { ok: true });
    fireEvent.click(await screen.findByRole("button", { name: "Entladen" }));
    await waitFor(() => expect(mock.calls.some((c) => c.method === "POST")).toBe(true));
    expect(mock.calls.find((c) => c.method === "POST")?.body).toEqual({
      name: "models.unload",
      args: { model: "llama-8b" },
    });
  });

  it("disables buttons while an action is pending", async () => {
    let release: (r: Response) => void = () => undefined;
    const pending = new Promise<Response>((resolve) => {
      release = resolve;
    });
    setup(makeDetail(), () => pending);
    const load = await screen.findByRole("button", { name: "Laden" });
    fireEvent.click(load);
    await waitFor(() => expect((load as HTMLButtonElement).disabled).toBe(true));
    expect((screen.getByRole("button", { name: "Entladen" }) as HTMLButtonElement).disabled).toBe(
      true,
    );
    await act(async () => {
      release(jsonResponse({ ok: true }));
      await pending;
    });
    await waitFor(() => expect((load as HTMLButtonElement).disabled).toBe(false));
  });

  it("disables buttons when the node is offline", async () => {
    setup(makeDetail({ state: "offline", connected: false }));
    const load = await screen.findByRole("button", { name: "Laden" });
    expect((load as HTMLButtonElement).disabled).toBe(true);
    expect((screen.getByRole("button", { name: "Entladen" }) as HTMLButtonElement).disabled).toBe(
      true,
    );
    expect(screen.getByText(/Der Node ist offline – Aktionen/)).toBeTruthy();
  });

  it("shows the error from an ok:false action result", async () => {
    setup(makeDetail(), { ok: false, error: { code: "load_failed", message: "out of memory" } });
    fireEvent.click(await screen.findByRole("button", { name: "Laden" }));
    expect((await screen.findByRole("alert")).textContent).toBe("Fehler: out of memory");
  });

  it("shows the node's own reason when it refuses a load as busy", async () => {
    setup(makeDetail(), {
      ok: false,
      error: { code: "busy", message: "node is busy gaming; models cannot be loaded now" },
    });
    fireEvent.click(await screen.findByRole("button", { name: "Laden" }));
    expect((await screen.findByRole("alert")).textContent).toBe(
      "Fehler: node is busy gaming; models cannot be loaded now",
    );
  });

  it("shows a translated error for HTTP 409 node_offline", async () => {
    setup(makeDetail(), () =>
      jsonResponse({ detail: { code: "node_offline", message: "offline" } }, 409),
    );
    fireEvent.click(await screen.findByRole("button", { name: "Laden" }));
    expect((await screen.findByRole("alert")).textContent).toBe("Der Node ist offline.");
  });

  it("shows a translated error for HTTP 504 timeout", async () => {
    setup(makeDetail(), () => jsonResponse({ detail: { code: "timeout", message: "t" } }, 504));
    fireEvent.click(await screen.findByRole("button", { name: "Laden" }));
    expect((await screen.findByRole("alert")).textContent).toMatch(/Zeitüberschreitung/);
  });

  it("shows not-found for unknown nodes", async () => {
    const mock = mockFetch({});
    const api = new ApiClient({ token: "t", fetchImpl: mock.fetch });
    render(<Harness api={api} nodeId="n1" />);
    expect((await screen.findByRole("alert")).textContent).toBe("Node nicht gefunden.");
  });

  it("applies live updates and prepends live events", async () => {
    setup(makeDetail());
    await screen.findByRole("heading", { name: "node-nvidia" });
    act(() => {
      nodeUpdated(makeNode({ state: "busy_gaming" }));
      nodeEvent("n1", {
        id: 9,
        kind: "game_detected",
        data: { game: "x" },
        ts: "2026-01-01T12:01:00Z",
        received_at: "",
      });
    });
    expect(screen.getByText("Spielt")).toBeTruthy();
    await openTab("Verlauf");
    const items = screen.getAllByRole("listitem").filter((li) => li.closest("ol.events"));
    expect(items[0]?.textContent).toContain("Spiel erkannt");
  });

  it("shows llama.cpp runtime, CPU topology, iGPU shared memory and process details", async () => {
    setup(
      makeDetail({
        cpu: {
          model: "Intel(R) Core(TM) i7-1255U",
          cores: 10,
          threads: 12,
          performance_cores: 2,
          efficiency_cores: 8,
        },
        runner: {
          type: "llamaserver",
          available: true,
          backend: "vulkan",
          llamacpp_release: "b11223",
          threads: 6,
        },
        gpus: [
          {
            index: 0,
            name: "Iris Xe",
            vendor: "intel",
            vram_total_mb: 128,
            vram_used_mb: 90,
            integrated: true,
            shared_total_mb: 16000,
            shared_used_mb: 1600,
          },
        ],
        models: [
          {
            name: "qwen-1.5b",
            size_bytes: 1_117_320_736,
            format: "gguf",
            loaded: true,
            vram_used_mb: 1400,
            ram_used_mb: 350,
            backend: "vulkan",
            port: 41001,
          },
        ],
        loaded: [
          {
            name: "qwen-1.5b",
            vram_used_mb: 1400,
            ram_used_mb: 350,
            backend: "vulkan",
            port: 41001,
            pid: 4242,
          },
        ],
      }),
    );
    await screen.findByRole("heading", { name: "node-nvidia" });
    expect(
      screen.getByText("Intel(R) Core(TM) i7-1255U – 10 Kerne (2 P + 8 E), 12 Threads"),
    ).toBeTruthy();
    expect(screen.getByText("b11223")).toBeTruthy();
    expect(screen.getByText("6")).toBeTruthy();
    const facts = document.querySelector(".facts-more dl.facts") as HTMLElement;
    expect(facts.textContent).toContain("Vulkan");
    // the summary names the iGPU's shared memory next to its VRAM
    expect(screen.getByTestId("glance-gpus").textContent).toContain("GB geteilt");
    expect(screen.getByRole("meter", { name: "VRAM Iris Xe" }).getAttribute("aria-valuenow")).toBe(
      "70",
    );
    expect(
      screen
        .getByRole("meter", { name: "Geteilter Speicher Iris Xe" })
        .getAttribute("aria-valuenow"),
    ).toBe("10");
    const row = screen.getByRole("link", { name: "qwen-1.5b" }).closest("tr") as HTMLElement;
    expect(row.textContent).toContain("RAM 350 MB");
    expect(row.textContent).toContain("41001");
    expect(screen.getByText(/PID 4242/).textContent).toContain("Port 41001");
  });

  it("shows automatic threads and a plain CPU without topology", async () => {
    setup(makeDetail({ cpu: { model: "Ryzen", cores: 8, threads: 16 } }));
    await screen.findByRole("heading", { name: "node-nvidia" });
    expect(screen.getByText("Ryzen – 8 Kerne, 16 Threads")).toBeTruthy();
    expect(screen.getByText("automatisch")).toBeTruthy();
  });

  it("deletes a stored model after a yes, and only one that is not loaded", async () => {
    const mock = setup(makeDetail(), { ok: true });
    const ask = vi.spyOn(window, "confirm").mockReturnValueOnce(false).mockReturnValueOnce(true);
    const del = await screen.findByRole("button", { name: "qwen-7b von diesem Node löschen" });
    expect(
      (screen.getByRole("button", { name: "llama-8b von diesem Node löschen" }) as HTMLButtonElement)
        .disabled,
    ).toBe(true); // loaded: unload it first
    fireEvent.click(del); // "no"
    await waitFor(() => expect(ask).toHaveBeenCalledTimes(1));
    expect(mock.calls.some((c) => c.method === "POST")).toBe(false);
    fireEvent.click(del); // "yes"
    await waitFor(() => expect(mock.calls.some((c) => c.method === "POST")).toBe(true));
    expect(mock.calls.find((c) => c.method === "POST")?.body).toEqual({
      name: "models.delete",
      args: { model: "qwen-7b", confirm: true },
    });
    ask.mockRestore();
  });

  it("leaves copying and moving to the models page", async () => {
    setup(makeDetail());
    await screen.findByRole("heading", { name: "node-nvidia" });
    expect(screen.queryByRole("button", { name: /kopieren|verschieben/i })).toBeNull();
    expect(screen.getByRole("link", { name: "Modelle" }).getAttribute("href")).toBe("#/admin/models");
    expect(screen.getByRole("link", { name: "qwen-7b" }).getAttribute("href")).toBe("#/admin/models/qwen-7b");
  });

  it("switches a node between compute node and tool node through config.roles", async () => {
    const config = {
      roles: ["gpu"],
      runner: { type: "fake" },
      allowed_actions: ["models.load"],
    };
    const mock = setup(makeDetail({ config }), undefined, {
      "PUT /api/nodes/n1/config": { config: { ...config, roles: ["tools"] }, config_hash: "h" },
    });
    await openTab("Rechte & Fähigkeiten");
    const box = (await screen.findByRole("checkbox", {
      name: /Rechen-Node/,
    })) as HTMLInputElement;
    expect(box.checked).toBe(true);
    fireEvent.click(box);
    await waitFor(() => expect(mock.calls.some((c) => c.method === "PUT")).toBe(true));
    const put = mock.calls.find((c) => c.method === "PUT");
    expect((put?.body as { roles: string[] }).roles).toEqual(["tools"]);
  });

  it("opens the download dialog", async () => {
    setup(makeDetail());
    fireEvent.click(await screen.findByRole("button", { name: "Modell herunterladen" }));
    expect(screen.getByRole("dialog", { name: "Modell herunterladen" })).toBeTruthy();
    fireEvent.keyDown(window, { key: "Escape" });
    expect(screen.queryByRole("dialog")).toBeNull();
  });

  it("refetches the detail when a benchmark finishes and labels new events", async () => {
    const mock = setup(makeDetail());
    await screen.findByRole("heading", { name: "node-nvidia" });
    const gets = () => mock.calls.filter((c) => c.method === "GET" && c.url === "/api/nodes/n1").length;
    expect(gets()).toBe(1);
    act(() => {
      nodeEvent("n1", {
        id: 20,
        kind: "benchmark_started",
        data: { benchmark_id: "b" },
        ts: "2026-01-01T12:01:00Z",
        received_at: "",
      });
    });
    expect(gets()).toBe(1);
    expect(screen.getByText("Benchmark gestartet")).toBeTruthy();
    act(() => {
      nodeEvent("n1", {
        id: 21,
        kind: "benchmark_finished",
        data: { benchmark_id: "b", ok: true },
        ts: "2026-01-01T12:02:00Z",
        received_at: "",
      });
    });
    await waitFor(() => expect(gets()).toBe(2));
  });
  it("shows that a model load was canceled and why", async () => {
    setup(
      makeDetail({
        events: [
          {
            id: 9,
            kind: "model_load_cancelled",
            data: { model: "big", reason: "unload_all", after_seconds: 41 },
            ts: "2026-01-01T12:02:00Z",
            received_at: "",
          },
        ],
      }),
    );
    await screen.findByRole("heading", { name: "node-nvidia" });
    await openTab("Verlauf");
    const items = screen.getAllByRole("listitem").filter((li) => li.closest("ol.events"));
    expect(items[0]?.textContent).toContain("Laden des Modells abgebrochen");
    expect(items[0]?.textContent).toContain("big: alles wurde entladen");
  });

  it("shows the paused state and why the node paused and continued", async () => {
    setup(
      makeDetail({
        state: "paused",
        events: [
          {
            id: 7,
            kind: "resumed",
            data: { paused_seconds: 184, reasons: ["input"] },
            ts: "2026-01-01T12:03:00Z",
            received_at: "",
          },
          {
            id: 6,
            kind: "paused",
            data: { reasons: ["input", "battery"], models: 1, streams: 0 },
            ts: "2026-01-01T12:00:00Z",
            received_at: "",
          },
        ],
      }),
    );
    await screen.findByRole("heading", { name: "node-nvidia" });
    expect(screen.getByText("Pausiert (wird genutzt)")).toBeTruthy();
    await openTab("Verlauf");
    const items = screen.getAllByRole("listitem").filter((li) => li.closest("ol.events"));
    expect(items[0]?.textContent).toContain("Nach lokaler Nutzung fortgesetzt");
    expect(items[1]?.textContent).toContain("Für lokale Nutzung pausiert");
    expect(items[1]?.textContent).toContain(
      "gerade Tastatur oder Maus benutzt werden, der Rechner im Akkubetrieb läuft",
    );
  });
  it("shows why a model load failed or was tried again", async () => {
    setup(
      makeDetail({
        events: [
          {
            id: 5,
            kind: "model_load_failed",
            data: { model: "big", code: "runner_error", message: "load big: failed to allocate" },
            ts: "2026-01-01T12:01:00Z",
            received_at: "",
          },
          {
            id: 4,
            kind: "model_load_retry",
            data: { model: "big", attempt: 2, plan: "cpu", error: "failed to allocate" },
            ts: "2026-01-01T12:00:30Z",
            received_at: "",
          },
        ],
      }),
    );
    await screen.findByRole("heading", { name: "node-nvidia" });
    await openTab("Verlauf");
    const items = screen.getAllByRole("listitem").filter((li) => li.closest("ol.events"));
    expect(items[0]?.textContent).toContain("Laden des Modells fehlgeschlagen");
    expect(items[0]?.textContent).toContain("big: load big: failed to allocate");
    expect(items[1]?.textContent).toContain("big: cpu – failed to allocate");
  });
  it("offers Wake-on-LAN for an offline node whose addresses the hub knows", async () => {
    const detail = makeDetail({
      state: "offline",
      connected: false,
      network: [{ name: "eth0", mac: "02:00:00:00:00:01", kind: "ethernet" }],
    });
    const mock = setup(detail, undefined, {
      "POST /api/nodes/n1/wake": { sent: 1, broadcast: "255.255.255.255", port: 9 },
    });
    fireEvent.click(await screen.findByRole("button", { name: "Aufwecken (Wake-on-LAN)" }));
    expect((await screen.findByRole("status")).textContent).toContain("Magic Packet gesendet");
    expect(mock.calls.some((c) => c.method === "POST" && c.url === "/api/nodes/n1/wake")).toBe(
      true,
    );
  });

  it("shows the network cards Wake-on-LAN would use", async () => {
    setup(
      makeDetail({
        network: [
          { name: "eth0", mac: "02:00:00:00:00:01", kind: "ethernet" },
          { name: "wlan0", mac: "02:00:00:00:00:02", kind: "wifi" },
        ],
      }),
    );
    const net = await screen.findByTestId("node-network");
    // one card per line: a run of names and MACs is hard to read on a phone
    expect([...net.querySelectorAll(".fact-item")].map((e) => e.textContent)).toEqual([
      "eth0 02:00:00:00:00:01",
      "wlan0 02:00:00:00:00:02",
    ]);
  });

  it("does not offer waking without a known address, or sleeping without the permission", async () => {
    setup(makeDetail({ state: "offline", connected: false, network: [] }));
    await screen.findByRole("heading", { name: "node-nvidia" });
    expect(screen.queryByRole("button", { name: "Aufwecken (Wake-on-LAN)" })).toBeNull();
    expect(screen.queryByRole("button", { name: "In den Ruhezustand versetzen" })).toBeNull();
  });

  it("puts a node to sleep after a confirmation when node.sleep is allowed", async () => {
    vi.spyOn(window, "confirm").mockReturnValue(true);
    const base = makeDetail();
    const detail = {
      ...base,
      config: { ...(base.config ?? {}), allowed_actions: ["models.list", "node.sleep"] },
    };
    const mock = setup(detail, { ok: true, result: { sleeping_in_seconds: 3 } });
    fireEvent.click(await screen.findByRole("button", { name: "In den Ruhezustand versetzen" }));
    expect((await screen.findByRole("status")).textContent).toContain("Ruhezustand");
    const post = mock.calls.find((c) => c.method === "POST");
    expect(post?.body).toEqual({ name: "node.sleep", args: {} });
  });

  it("shows the yielded state, other programs' GPU usage and event reasons", async () => {
    setup(
      makeDetail({
        state: "yielded",
        gpus: [
          {
            index: 0,
            name: "Fake GPU",
            vendor: "nvidia",
            vram_total_mb: 8192,
            vram_used_mb: 7000,
            foreign_vram_used_mb: 6800,
            foreign_gpu_util: 97,
            foreign_shared_max_mb: 7743,
          },
        ],
        events: [
          {
            id: 3,
            kind: "auto_reload_paused",
            data: { model: "llama-8b", until: "2026-01-01T13:00:00Z", yields: 3 },
            ts: "2026-01-01T12:00:00Z",
            received_at: "",
          },
          {
            id: 2,
            kind: "model_unloaded",
            data: {
              model: "llama-8b",
              reason: "vram_pressure",
              signal: "unmet_demand",
              free_mb: 400,
            },
            ts: "2026-01-01T11:59:30Z",
            received_at: "",
          },
        ],
      }),
    );
    await screen.findByRole("heading", { name: "node-nvidia" });
    expect(screen.getByText("VRAM freigegeben")).toBeTruthy();
    expect(
      screen.getByText(
        "Von anderen Programmen belegt: 6.800 MB · GPU-Last anderer Programme: 97 % · Anderes Programm weicht in den Systemspeicher aus: 7.743 MB",
      ),
    ).toBeTruthy();
    await openTab("Verlauf");
    const items = screen.getAllByRole("listitem").filter((li) => li.closest("ol.events"));
    expect(items[0]?.textContent).toContain("Automatisches Laden pausiert");
    expect(items[0]?.textContent).toMatch(/bis .*\(zu oft freigegeben\)/);
    expect(items[1]?.textContent).toContain("Modell entladen");
    expect(items[1]?.textContent).toContain(
      "VRAM für andere Programme freigegeben (anderes Programm musste in den Systemspeicher ausweichen)",
    );
  });

  it("toggles Automatisch bereithalten and saves the priority", async () => {
    const config = {
      residents: [{ model: "llama-8b", priority: 50, auto_reload: false, context_length: 4096 }],
    };
    const put = (init: RequestInit | undefined) => {
      const entry = JSON.parse(String(init?.body)) as Record<string, unknown>;
      return jsonResponse({
        config: { residents: [{ model: "qwen-7b", ...entry }, ...config.residents] },
        config_hash: "h2",
        changed: true,
      });
    };
    const mock = setup(makeDetail({ config }), undefined, {
      "PUT /api/nodes/n1/residents/qwen-7b": put,
      "PUT /api/nodes/n1/residents/llama-8b": {
        config: { residents: [{ ...config.residents[0], priority: 80 }] },
        config_hash: "h3",
        changed: true,
      },
      "DELETE /api/nodes/n1/residents/llama-8b": {
        config: { residents: [] },
        config_hash: "h4",
        changed: true,
      },
    });
    await screen.findByRole("heading", { name: "node-nvidia" });
    // A manually unloaded resident explains why it is not loaded again.
    expect(screen.getByText(/pausiert \(manuell entladen\)/)).toBeTruthy();

    const toggle = screen.getByRole("checkbox", { name: "qwen-7b automatisch bereithalten" });
    expect((toggle as HTMLInputElement).checked).toBe(false);
    fireEvent.click(toggle);
    await waitFor(() =>
      expect(
        (
          screen.getByRole("checkbox", {
            name: "qwen-7b automatisch bereithalten",
          }) as HTMLInputElement
        ).checked,
      ).toBe(true),
    );
    expect(mock.calls.find((c) => c.method === "PUT")?.body).toEqual({});

    const priority = screen.getByRole("spinbutton", { name: "Priorität für llama-8b" });
    fireEvent.change(priority, { target: { value: "80" } });
    fireEvent.blur(priority);
    await waitFor(() =>
      expect(mock.calls.some((c) => c.url === "/api/nodes/n1/residents/llama-8b")).toBe(true),
    );
    // PUT replaces the entry: auto_reload and context length are sent along.
    expect(mock.calls.find((c) => c.url === "/api/nodes/n1/residents/llama-8b")?.body).toEqual({
      priority: 80,
      auto_reload: false,
      context_length: 4096,
    });

    fireEvent.click(screen.getByRole("checkbox", { name: "llama-8b automatisch bereithalten" }));
    await waitFor(() => expect(mock.calls.some((c) => c.method === "DELETE")).toBe(true));
    await waitFor(() =>
      expect(screen.queryByRole("spinbutton", { name: "Priorität für llama-8b" })).toBeNull(),
    );
  });

  it("offers to remove residents that are not on the node", async () => {
    const mock = setup(makeDetail({ config: { residents: [{ model: "gone-13b" }] } }), undefined, {
      "DELETE /api/nodes/n1/residents/gone-13b": {
        config: { residents: [] },
        config_hash: "h",
        changed: true,
      },
    });
    await screen.findByRole("heading", { name: "node-nvidia" });
    expect(screen.getByText("gone-13b")).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "gone-13b nicht mehr bereithalten" }));
    await waitFor(() => expect(screen.queryByText("gone-13b")).toBeNull());
    expect(mock.calls.some((c) => c.method === "DELETE")).toBe(true);
  });

  it("refetches the detail when the config changed", async () => {
    const mock = setup(makeDetail());
    await screen.findByRole("heading", { name: "node-nvidia" });
    const gets = () => mock.calls.filter((c) => c.method === "GET" && c.url === "/api/nodes/n1").length;
    act(() => {
      nodeEvent("n1", {
        id: 30,
        kind: "config_changed",
        data: { source: "manual_unload", models: ["llama-8b"], auto_reload: false },
        ts: "2026-01-01T12:03:00Z",
        received_at: "",
      });
    });
    expect(
      screen.getByText("Automatisches Laden nach manuellem Entladen ausgeschaltet"),
    ).toBeTruthy();
    await waitFor(() => expect(gets()).toBe(2));
  });

  it("shows a tool node's capabilities instead of models, GPUs and benchmarks", async () => {
    setup(
      makeDetail({ config: { roles: ["tools"], runner: { type: "none" }, capabilities: [] } }),
      undefined,
      {
        "GET /api/nodes/n1/capabilities": {
          node: { id: "n1", name: "node-nvidia", roles: ["tools"] },
          offered: [],
          enabled: [],
          usable: [],
          grants: [],
        },
      },
    );
    expect(await screen.findByRole("heading", { name: "Fähigkeiten" })).toBeTruthy();
    expect(screen.queryByRole("heading", { name: "Modelle" })).toBeNull();
    expect(screen.queryByRole("heading", { name: /Benchmark/ })).toBeNull();
    expect(screen.queryByRole("heading", { name: "GPUs" })).toBeNull();
    await openTab("Wartung");
    expect(screen.getByRole("heading", { name: "Konfiguration" })).toBeTruthy();
  });

  it("treats a dev node like a tool node: capabilities, no models", async () => {
    setup(
      makeDetail({ config: { roles: ["dev"], runner: { type: "none" }, capabilities: [] } }),
      undefined,
      {
        "GET /api/nodes/n1/capabilities": {
          node: { id: "n1", name: "dev-alpha", roles: ["dev"] },
          offered: [],
          enabled: [],
          usable: [],
          grants: [],
        },
      },
    );
    expect(await screen.findByRole("heading", { name: "Fähigkeiten" })).toBeTruthy();
    expect(screen.queryByRole("heading", { name: "Modelle" })).toBeNull();
  });

  it("loads again after a rename and hands each loaded detail to onLoaded (the node list)", async () => {
    // the hub publishes no node_updated on a rename: only the reload brings the new name
    vi.spyOn(window, "confirm").mockReturnValue(true);
    let name = "node-nvidia";
    const mock = mockFetch({
      "GET /api/nodes/n1": () => jsonResponse(makeDetail({ name })),
      "POST /api/nodes/n1/rename": () => {
        name = "office-pc";
        return jsonResponse({ id: "n1", from: "node-nvidia", to: name, rewritten: {} });
      },
    });
    const onLoaded = vi.fn();
    const api = new ApiClient({ token: "t", fetchImpl: mock.fetch });
    render(<NodeDetailView api={api} nodeId="n1" events={bus} onLoaded={onLoaded} />);
    await screen.findByRole("heading", { name: "node-nvidia" });
    expect(onLoaded).toHaveBeenCalledTimes(1);

    await openTab("Wartung");
    fireEvent.click(screen.getByRole("button", { name: "Umbenennen" }));
    fireEvent.change(screen.getByRole("textbox", { name: "Neuer Name" }), {
      target: { value: "office-pc" },
    });
    const form = screen.getByRole("textbox", { name: "Neuer Name" }).closest("form");
    if (!form) throw new Error("no rename form");
    fireEvent.submit(form);

    expect(await screen.findByRole("heading", { name: "office-pc" })).toBeTruthy();
    expect(onLoaded).toHaveBeenCalledTimes(2);
    expect(onLoaded).toHaveBeenLastCalledWith(expect.objectContaining({ id: "n1", name: "office-pc" }));
  });
});
