import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { ApiClient } from "../api/client";
import { createHubEvents } from "../data/hubEvents";
import { makeDetail } from "../test/fixtures";
import { mockFetch } from "../test/mockFetch";
import type { NodeDetail, NodeUpdate } from "../types";
import { NodeDetailView } from "./NodeDetail";
import { nodeVersion } from "./NodeHeader";

// fixture figures
const CPU_PERCENT = 37.4;
const LOADED_VRAM_MB = 6000;
const WOL_PORT = 9;
const SLEEP_SECONDS = 3;

afterEach(() => {
  vi.restoreAllMocks();
});

const update = (over: Partial<NodeUpdate> = {}): NodeUpdate => ({
  channel: "stable",
  capable: true,
  updater: "ok",
  key_ids: [],
  available: "0.2.19",
  newer: true,
  action_allowed: true,
  ...over,
});

function setup(detail: NodeDetail, extra: Record<string, unknown> = {}) {
  const mock = mockFetch({ "GET /api/nodes/n1": detail, ...extra });
  const api = new ApiClient({ token: "t", fetchImpl: mock.fetch });
  render(<NodeDetailView api={api} nodeId="n1" events={createHubEvents()} />);
  return mock;
}

describe("nodeVersion", () => {
  it("keeps the reported version as it is", () => {
    expect(nodeVersion({ agent_version: "0.2.18" }).installed).toBe("0.2.18");
    expect(nodeVersion({ agent_version: "0.2.18-rc.1" }).installed).toBe("0.2.18-rc.1");
    expect(nodeVersion({ agent_version: null }).installed).toBeNull();
    expect(nodeVersion({ agent_version: "" }).installed).toBeNull();
  });

  it("names the newer release only when the hub says it is newer", () => {
    expect(nodeVersion({ agent_version: "0.2.18", update: update() }).newer).toBe("0.2.19");
    expect(
      nodeVersion({ agent_version: "0.2.18", update: update({ newer: false }) }).newer,
    ).toBeNull();
    expect(nodeVersion({ agent_version: "0.2.18" }).newer).toBeNull();
  });
});

describe("NodeHeader", () => {
  it("shows state, load, loaded model and version from the fixture", async () => {
    setup(makeDetail({ agent_version: "0.2.18", cpu_util: CPU_PERCENT, update: update() }));
    const header = await screen.findByTestId("node-header");
    const h = within(header);
    expect(h.getByRole("heading", { name: "node-nvidia" })).toBeTruthy();
    expect(h.getByText("Verfügbar")).toBeTruthy();
    expect(h.getByTestId("node-glance").textContent).toContain("8,0 / 32,0 GB");
    expect(h.getByTestId("glance-cpu").textContent).toBe("37 % Last");
    expect(h.getByTestId("glance-gpus").textContent).toBe("Fake GPU: 8,0 / 24,0 GB VRAM");
    expect(h.getByTestId("glance-loaded").textContent).toBe("llama-8b (5,9 GB VRAM)");
    // 0.2.18 stays 0.2.18 (it was once shown as 0.2.8)
    expect(h.getByTestId("node-version").textContent).toBe("Agent 0.2.18 Update 0.2.19 verfügbar");
    expect(h.getByTestId("node-update-hint").textContent).toBe("Update 0.2.19 verfügbar");
  });

  it("gives no update hint while the node is current, and no CPU figure it did not report", async () => {
    setup(makeDetail({ agent_version: "0.2.18", update: update({ newer: false }) }));
    const header = await screen.findByTestId("node-header");
    expect(within(header).queryByTestId("node-update-hint")).toBeNull();
    expect(within(header).queryByTestId("glance-cpu")).toBeNull();
  });

  it("loads the model picked in the header through the same action as the table", async () => {
    const mock = setup(
      makeDetail({
        models: [
          { name: "qwen-7b", loaded: false, vram_used_mb: null },
          { name: "mistral-7b", loaded: false, vram_used_mb: null },
          { name: "llama-8b", loaded: true, vram_used_mb: LOADED_VRAM_MB },
        ],
      }),
      { "POST /api/nodes/n1/actions": { ok: true } },
    );
    const header = await screen.findByTestId("node-header");
    const pick = within(header).getByRole("combobox", { name: "Zu ladendes Modell" });
    // only what is not loaded yet
    expect([...(pick as HTMLSelectElement).options].map((o) => o.value)).toEqual([
      "qwen-7b",
      "mistral-7b",
    ]);
    fireEvent.change(pick, { target: { value: "mistral-7b" } });
    fireEvent.click(within(header).getByRole("button", { name: "Modell laden" }));
    await waitFor(() => expect(mock.calls.some((c) => c.method === "POST")).toBe(true));
    const post = mock.calls.find((c) => c.method === "POST");
    expect(post?.url).toBe("/api/nodes/n1/actions");
    expect(post?.body).toEqual({ name: "models.load", args: { model: "mistral-7b" } });
  });

  it("has wake and sleep in the header, calling the hub as before", async () => {
    const wake = setup(
      makeDetail({
        state: "offline",
        connected: false,
        network: [{ name: "eth0", mac: "02:00:00:00:00:01", kind: "ethernet" }],
      }),
      { "POST /api/nodes/n1/wake": { sent: 1, broadcast: "255.255.255.255", port: WOL_PORT } },
    );
    let header = await screen.findByTestId("node-header");
    // an offline node loads nothing: no picker
    expect(within(header).queryByRole("combobox")).toBeNull();
    expect(within(header).queryByRole("button", { name: "Modell laden" })).toBeNull();
    fireEvent.click(within(header).getByRole("button", { name: "Aufwecken (Wake-on-LAN)" }));
    await waitFor(() =>
      expect(wake.calls.some((c) => c.method === "POST" && c.url === "/api/nodes/n1/wake")).toBe(
        true,
      ),
    );
    cleanup();

    vi.spyOn(window, "confirm").mockReturnValue(true);
    const base = makeDetail();
    const sleep = setup(
      { ...base, config: { ...(base.config ?? {}), allowed_actions: ["node.sleep"] } },
      { "POST /api/nodes/n1/actions": { ok: true, result: { sleeping_in_seconds: SLEEP_SECONDS } } },
    );
    header = await screen.findByTestId("node-header");
    fireEvent.click(within(header).getByRole("button", { name: "In den Ruhezustand versetzen" }));
    await waitFor(() => expect(sleep.calls.some((c) => c.method === "POST")).toBe(true));
    expect(sleep.calls.find((c) => c.method === "POST")?.body).toEqual({
      name: "node.sleep",
      args: {},
    });
  });

  it("leaves the model picker out on a tool node", async () => {
    const base = makeDetail();
    setup({ ...base, config: { ...(base.config ?? {}), roles: ["tools"] } });
    const header = await screen.findByTestId("node-header");
    expect(within(header).queryByRole("combobox")).toBeNull();
    expect(within(header).queryByTestId("glance-loaded")).toBeNull();
  });
});
