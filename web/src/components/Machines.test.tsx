import {
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
  within,
} from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";
import { ApiClient } from "../api/client";
import { jsonResponse, mockFetch } from "../test/mockFetch";
import { makeNode } from "../test/fixtures";
import type { ActivityView, MachinesView, NodeMetricPoint } from "../types";
import { metersOf } from "./MachineMeters";
import { Overview } from "./Overview";

afterEach(cleanup);

const host = makeNode({ id: "h", name: "node-host", roles: ["gpu"] });
const tool = makeNode({
  id: "t",
  name: "tools-guest",
  gpus: [],
  loaded: [],
  roles: ["tools"],
  ram_total_mb: 8192,
  ram_used_mb: 4096,
  capabilities: ["docker.ps"],
});

const VIEW: MachinesView = {
  machines: [
    {
      key: "host",
      name: "Host",
      kind: "physical",
      host: null,
      configured: true,
      nodes: ["node-host"],
    },
    {
      key: "guest",
      name: "Guest",
      kind: "vm",
      host: "host",
      configured: true,
      nodes: ["tools-guest"],
    },
  ],
  node_machine: { "node-host": "host", "tools-guest": "guest" },
  assign: {},
};

const point = (
  t: number,
  cpu: number | null,
  gpu: number | null,
): NodeMetricPoint => ({
  t,
  ram_used_mb: 1000,
  cpu_util: cpu,
  gpu_util: gpu,
  vram_used_mb: 500,
});

function hub(view: MachinesView = VIEW) {
  const mock = mockFetch({
    "GET /api/machines": () => jsonResponse(view),
    "GET /api/nodes/metrics": () =>
      jsonResponse({
        window_seconds: 1800,
        nodes: {
          h: [point(1, 10, 20), point(2, 30, 60), point(3, 55, 80)],
          t: [point(1, 5, null), point(2, 7, null)],
        },
      }),
    "GET /api/activity": () => jsonResponse({ detail: "n/a" }, 503),
    "PUT /api/machines": (init?: RequestInit) =>
      jsonResponse({ ...VIEW, assign: JSON.parse(String(init?.body)).assign }),
  });
  return { mock, api: new ApiClient({ token: "t", fetchImpl: mock.fetch }) };
}

describe("metersOf", () => {
  it("takes each reading from the node that reports it, the compute node first", () => {
    const m = metersOf([tool, host], {
      h: [point(1, 10, 20), point(2, 30, 60)],
      t: [point(1, 5, null)],
    });
    expect(m.cpu).toEqual([10, 30]);
    expect(m.gpu).toEqual([20, 60]);
    expect(m.ram).toEqual({ usedMb: 8192, totalMb: 32768 });
    expect(m.vram.map((g) => g.totalMb)).toEqual([24576]);
  });

  it("falls back to a tool node for the CPU and leaves out what nobody reports", () => {
    const m = metersOf([tool], { t: [point(1, 5, null), point(2, 7, null)] });
    expect(m.cpu).toEqual([5, 7]);
    expect(m.gpu).toEqual([]);
    expect(m.vram).toEqual([]);
  });

  it("shows nothing for a machine whose nodes are all offline", () => {
    const m = metersOf([{ ...host, state: "offline" }], {
      h: [point(1, 10, 20)],
    });
    expect(m.cpu).toEqual([]);
    expect(m.ram).toBeNull();
    expect(m.vram).toEqual([]);
  });
});

const activityView: ActivityView = {
  nodes: [
    {
      name: "node-host",
      state: "available",
      work: [{ node: "node-host", model: "gemma-12b", kind: "chat", seconds: 12 }],
      queued: { chat: 1, answer: 0, idle: 2 },
      waiting: [
        { place: 0, seconds: 4, kind: "chat", model: "gemma-12b" },
        { place: 2, seconds: 95, kind: "care", model: "qwen-9b" },
      ],
    },
    { name: "tools-guest", state: "available", work: [] },
  ],
  roles: [{ key: "chat", model: "gemma-12b", node: null }],
  background_nodes: [],
  digest: { running: 0, waiting: 0 },
};

function hubWithActivity(view: MachinesView = VIEW) {
  const mock = mockFetch({
    "GET /api/machines": () => jsonResponse(view),
    "GET /api/nodes/metrics": () =>
      jsonResponse({
        window_seconds: 1800,
        nodes: { h: [point(1, 10, 20), point(2, 30, 60), point(3, 55, 80)], t: [point(1, 5, null), point(2, 7, null)] },
      }),
    "GET /api/activity": () => jsonResponse(activityView),
  });
  return new ApiClient({ token: "t", fetchImpl: mock.fetch });
}

describe("Overview by machine", () => {
  it("shows the host as a row with its readings and the VM as an indented row below it", async () => {
    const { api } = hub();
    render(<Overview nodes={[host, tool]} loaded error={null} api={api} />);
    const row = await screen.findByTestId("machine-host");
    const vm = screen.getByTestId("machine-guest");
    expect(within(row).getByRole("link", { name: /node-host/ })).toBeTruthy();
    expect(within(vm).getByText("VM auf Host")).toBeTruthy();
    // the VM follows its host and is marked as indented
    const rows = screen.getAllByTestId(/^machine-(host|guest)$/);
    expect(rows.map((r) => r.getAttribute("data-testid"))).toEqual(["machine-host", "machine-guest"]);
    expect(vm.className).toContain("mt-vm");
    expect(row.className).not.toContain("mt-vm");
    // a node is not shown as a second thing
    expect(screen.queryByTestId("node-card-h")).toBeNull();
    await waitFor(() => {
      const spark = within(row).getAllByRole("img").map((i) => i.getAttribute("aria-label"));
      expect(spark).toContain("CPU-Last, aktuell 55 %");
      expect(spark).toContain("GPU-Last, aktuell 80 %");
    });
    expect(within(row).getAllByRole("meter").length).toBe(2); // RAM and VRAM
    expect(within(vm).queryByLabelText(/GPU-Last/)).toBeNull();
    expect(within(vm).getAllByRole("meter").length).toBe(1); // RAM only, no VRAM
  });

  it("shows a machine with several nodes as one row with a chip and a link per node", async () => {
    const dev = makeNode({ id: "d", name: "dev-guest", gpus: [], loaded: [], roles: ["dev"] });
    const { api } = hub({
      ...VIEW,
      machines: VIEW.machines.map((m) => (m.key === "guest" ? { ...m, nodes: [...m.nodes, "dev-guest"] } : m)),
      node_machine: { ...VIEW.node_machine, "dev-guest": "guest" },
    });
    render(<Overview nodes={[host, tool, dev]} loaded error={null} api={api} />);
    const guest = await screen.findByTestId("machine-guest");
    expect(screen.getAllByTestId(/^machine-/).filter((r) => r.tagName === "TR")).toHaveLength(2);
    const chipT = within(guest).getByTestId("node-chip-t");
    const chipD = within(guest).getByTestId("node-chip-d");
    expect(within(chipT).getByText("Werkzeuge")).toBeTruthy();
    expect(within(chipD).getByText("Entwicklung")).toBeTruthy();
    expect(within(chipD).getByRole("link", { name: /dev-guest/ }).getAttribute("href")).toBe("#/admin/nodes/d");
    expect(within(chipT).getByText("Verfügbar")).toBeTruthy();
  });

  it("keeps the short machine name on screen and gives the full node name as tooltip and label (#77)", async () => {
    const { api } = hub();
    render(<Overview nodes={[host, tool]} loaded error={null} api={api} />);
    const row = await screen.findByTestId("machine-host");
    const link = within(row).getByRole("link", { name: "Details zu node-host anzeigen" });
    expect(link.textContent).toBe("Host");
    expect(link.getAttribute("title")).toBe("node-host");
  });

  it("applies the node filter to the rows", async () => {
    const { api } = hub();
    render(<Overview nodes={[host, tool]} loaded error={null} api={api} />);
    await screen.findByTestId("machine-host");
    fireEvent.click(screen.getByRole("button", { name: /^Werkzeug-Nodes/ }));
    // the host has no tool node left, but its VM stays, so the host row stays as its parent
    expect(screen.getByTestId("machine-guest")).toBeTruthy();
    expect(within(screen.getByTestId("machine-host")).queryByText("llama-8b")).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: /^Rechen-Node/ }));
    expect(screen.queryByTestId("machine-guest")).toBeNull();
    expect(screen.getByTestId("machine-host")).toBeTruthy();
  });

  it("shows what runs now, otherwise free and the loaded models, and the waiting work below", async () => {
    const api = hubWithActivity();
    render(<Overview nodes={[host, tool]} loaded error={null} api={api} />);
    const row = await screen.findByTestId("machine-host");
    await waitFor(() => expect(within(row).getByTestId("machine-work").textContent).toContain("gemma-12b · 12 s"));
    expect(within(row).getByTestId("machine-work").textContent).toContain("Chat-Antwort");
    expect(row.textContent).toContain("3 wartend");
    const waiting = screen.getByTestId("waiting-work");
    expect(within(waiting).getByTestId("activity-queued-node-host").textContent).toContain(
      "wartet: 1 Chat, 0 nach einer Antwort, 2 übrige",
    );
    // SCH-03 step 3: the single waiting requests, in the order they will be served
    const list = within(waiting).getByTestId("activity-waiting-node-host");
    expect(within(list).getAllByRole("listitem").map((li) => li.textContent)).toEqual([
      expect.stringContaining("Chat-Antwort"),
      expect.stringContaining("qwen-9b"),
    ]);
    // the tools node is not busy: its tool groups, no work line
    const guest = screen.getByTestId("machine-guest");
    expect(within(guest).queryByTestId("machine-work")).toBeNull();
    expect(within(guest).getByText("Werkzeuge: docker")).toBeTruthy();
    // the standing tasks sit in a collapsed section
    expect((screen.getByTestId("activity-panel") as HTMLDetailsElement).open).toBe(false);
  });

  it("says free for an idle compute node and shows its models", async () => {
    const idle = makeNode({ id: "h", name: "node-host", roles: ["gpu"] });
    const mock = mockFetch({
      "GET /api/machines": () => jsonResponse(VIEW),
      "GET /api/nodes/metrics": () => jsonResponse({ window_seconds: 1800, nodes: {} }),
      "GET /api/activity": () =>
        jsonResponse({ ...activityView, nodes: activityView.nodes.map((n) => ({ ...n, work: [], queued: undefined })) }),
    });
    render(
      <Overview
        nodes={[idle, tool]}
        loaded
        error={null}
        api={new ApiClient({ token: "t", fetchImpl: mock.fetch })}
      />,
    );
    const row = await screen.findByTestId("machine-host");
    await waitFor(() => expect(within(row).getByText("frei")).toBeTruthy());
    expect(within(row).getByText("llama-8b")).toBeTruthy();
    expect(screen.queryByTestId("waiting-work")).toBeNull();
  });

  it("gives a node the machine view does not know a row of its own", async () => {
    const stray = makeNode({ id: "s", name: "node-stray", roles: ["gpu"] });
    const { api } = hub();
    render(<Overview nodes={[host, tool, stray]} loaded error={null} api={api} />);
    await screen.findByTestId("machine-host");
    expect(screen.getByTestId("machine-node:s")).toBeTruthy();
  });

  it("shows no stale values for an offline machine, and never a chat title", async () => {
    const api = hubWithActivity();
    const off = { ...host, state: "offline" as const };
    render(<Overview nodes={[off, tool]} loaded error={null} api={api} />);
    const row = await screen.findByTestId("machine-host");
    expect(within(row).getByText("Offline")).toBeTruthy();
    expect(within(row).queryByRole("meter")).toBeNull();
    expect(within(row).queryByRole("img")).toBeNull();
    expect(row.textContent).not.toContain("llama-8b");
    expect(row.textContent).not.toContain("GB");
  });

  it("shows an asleep machine that the hub is waking, and nothing else of its work (#102)", async () => {
    const view: ActivityView = {
      ...activityView,
      nodes: [
        {
          name: "node-host",
          state: "offline",
          work: [
            { node: "node-host", model: "llama-8b", kind: "chat", seconds: 300 },
            { node: "node-host", model: "gemma-12b", kind: "waking", seconds: 7 },
          ],
        },
      ],
    };
    const mock = mockFetch({
      "GET /api/machines": () => jsonResponse(VIEW),
      "GET /api/nodes/metrics": () => jsonResponse({ window_seconds: 1800, nodes: {} }),
      "GET /api/activity": () => jsonResponse(view),
    });
    const api = new ApiClient({ token: "t", fetchImpl: mock.fetch });
    render(<Overview nodes={[{ ...host, state: "offline" }, tool]} loaded error={null} api={api} />);
    const row = await screen.findByTestId("machine-host");
    await waitFor(() => expect(within(row).getAllByTestId("machine-work")).toHaveLength(1));
    expect(within(row).getByTestId("machine-work").textContent).toBe("Wird geweckt … gemma-12b · 7 s");
    expect(row.textContent).not.toContain("llama-8b");
  });

  it("lets an admin say which machine is a VM and where it runs", async () => {
    const view: MachinesView = {
      machines: [
        {
          key: "host",
          name: "host",
          kind: "physical",
          host: null,
          configured: false,
          nodes: ["node-host"],
        },
        {
          key: "guest",
          name: "guest",
          kind: "physical",
          host: null,
          configured: false,
          nodes: ["tools-guest"],
        },
      ],
      node_machine: { "node-host": "host", "tools-guest": "guest" },
      assign: {},
    };
    const { api, mock } = hub(view);
    render(
      <Overview nodes={[host, tool]} loaded error={null} api={api} canAdd />,
    );
    fireEvent.click(
      await screen.findByRole("button", { name: "Nodes ordnen" }),
    );
    const list = await screen.findByTestId("machines-list");
    fireEvent.change(
      within(list).getByRole("combobox", { name: "Art von guest" }),
      {
        target: { value: "vm" },
      },
    );
    fireEvent.change(
      within(list).getByRole("combobox", { name: "Host von guest" }),
      {
        target: { value: "host" },
      },
    );
    fireEvent.click(screen.getByRole("button", { name: "Speichern" }));
    await waitFor(() =>
      expect(mock.calls.some((c) => c.method === "PUT")).toBe(true),
    );
    const body = mock.calls.find((c) => c.method === "PUT")!.body as {
      machines: { key: string; kind: string; host?: string }[];
      assign: Record<string, string>;
    };
    expect(body.machines.find((m) => m.key === "guest")).toMatchObject({
      kind: "vm",
      host: "host",
    });
    expect(body.machines.find((m) => m.key === "host")?.kind).toBe("physical");
    expect(body.assign).toEqual({}); // the names already say where the nodes are
  });
});
