import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { ApiClient } from "../api/client";
import { jsonResponse, mockFetch } from "../test/mockFetch";
import { PoolsPanel } from "./PoolsPanel";

const model = (name: string, available = true) => ({
  name,
  loaded: false,
  available,
  kinds: ["chat"],
  kinds_source: "detected" as const,
  size_mb: 4096,
  tps: name === "big" ? 33.1 : null,
  tags: name === "big" ? ["chat", "26B", "moe"] : ["chat", "4B", "small"],
});

function setup() {
  let view = {
    default_model: "big",
    nodes: [
      {
        node: "node-nvidia",
        node_id: "n1",
        house: null as string | null,
        worker: false,
        connected: true,
        restricted: false,
        max_loaded: null as number | null,
        models: [model("big"), model("small")],
      },
    ],
  };
  const mock = mockFetch({
    "GET /api/scheduling/pools": () => jsonResponse(view),
    "PUT /api/scheduling/pools": (init?: RequestInit) => {
      const body = JSON.parse(String(init?.body)) as {
        pools: Record<string, { available: string[] }>;
      };
      const avail = body.pools["node-nvidia"]?.available;
      view = {
        ...view,
        nodes: [
          {
            node: "node-nvidia",
            node_id: "n1",
            house: null,
            worker: false,
            connected: true,
            restricted: !!avail,
            max_loaded: view.nodes[0]?.max_loaded ?? null,
            models: ["big", "small"].map((m) => model(m, !avail || avail.includes(m))),
          },
        ],
      };
      return jsonResponse(view);
    },
    "PUT /api/scheduling/max-loaded": (init?: RequestInit) => {
      const body = JSON.parse(String(init?.body)) as { max_loaded: Record<string, number> };
      const node = view.nodes[0];
      if (node) view = { ...view, nodes: [{ ...node, max_loaded: body.max_loaded["node-nvidia"] ?? null }] };
      return jsonResponse(view);
    },
    "PUT /api/scheduling/model-kinds": () => jsonResponse(view),
    "PUT /api/scheduling/house": () => jsonResponse(view),
    "PUT /api/nodes/n1/residents/big": () => jsonResponse({ changed: true }),
    "POST /api/scheduling/model-probes": () => {
      const node = view.nodes[0];
      if (node) {
        const models = node.models.map((m) =>
          m.name === "big" ? { ...m, capabilities: { tools: true, embedding: false } } : m,
        );
        view = { ...view, nodes: [{ ...node, models }] };
      }
      return jsonResponse({ results: { tools: { ok: true }, embedding: { ok: false } } });
    },
  });
  return { mock, api: new ApiClient({ token: "t", fetchImpl: mock.fetch }) };
}

describe("PoolsPanel", () => {
  it("puts a node's house model first and sends the house order (issue 2.27)", async () => {
    const node = (name: string, id: string, house: string, rank: number) => ({
      node: name,
      node_id: id,
      house,
      house_rank: rank,
      worker: false,
      connected: true,
      restricted: false,
      max_loaded: null,
      models: [model(house)],
    });
    const view = {
      default_model: "",
      nodes: [node("node-desk", "n1", "gpt-oss", 1), node("node-tower", "n2", "qwen3-coder", 2)],
    };
    const mock = mockFetch({
      "GET /api/scheduling/pools": () => jsonResponse(view),
      "PUT /api/scheduling/pools": () => jsonResponse(view),
      "PUT /api/scheduling/house": () => jsonResponse(view),
    });
    render(<PoolsPanel api={new ApiClient({ token: "t", fetchImpl: mock.fetch })} />);
    expect(await screen.findByText(/Platz 2/)).toBeTruthy();
    // the first node has no "First" button, the second has
    expect(screen.queryByRole("button", { name: "Hausmodell von node-desk zuerst nehmen" })).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "Hausmodell von node-tower zuerst nehmen" }));
    expect(screen.getByText(/Platz 1/).closest("p")?.textContent).not.toContain("Zuerst");
    fireEvent.click(screen.getByRole("button", { name: "Pools speichern" }));
    await waitFor(() => expect(mock.calls.some((c) => c.url === "/api/scheduling/house")).toBe(true));
    expect(mock.calls.find((c) => c.url === "/api/scheduling/house")?.body).toEqual({
      house: { "node-desk": "gpt-oss", "node-tower": "qwen3-coder" },
      workers: [],
      order: ["node-tower", "node-desk"],
    });
  });

  it("saves a pool of the models that stay on", async () => {
    const { api, mock } = setup();
    render(<PoolsPanel api={api} />);
    expect(mock.calls.some((c) => c.url === "/api/scheduling/max-loaded")).toBe(false);
    fireEvent.click(await screen.findByLabelText("node-nvidia: big verfügbar"));
    fireEvent.click(screen.getByRole("button", { name: "Pools speichern" }));
    await waitFor(() => expect(mock.calls.some((c) => c.method === "PUT")).toBe(true));
    expect(mock.calls.find((c) => c.method === "PUT")?.body).toEqual({
      pools: { "node-nvidia": { available: ["small"] } },
    });
  });

  it("limits the models a node holds at once, and a cleared field means no limit", async () => {
    const { api, mock } = setup();
    render(<PoolsPanel api={api} />);
    fireEvent.change(await screen.findByLabelText("node-nvidia: Höchstens so viele Modelle gleichzeitig geladen"), {
      target: { value: "1" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Pools speichern" }));
    await waitFor(() => expect(mock.calls.some((c) => c.url === "/api/scheduling/max-loaded")).toBe(true));
    expect(mock.calls.find((c) => c.url === "/api/scheduling/max-loaded")?.body).toEqual({
      max_loaded: { "node-nvidia": 1 },
    });
  });

  it("sets the kind of a model by hand", async () => {
    const { api, mock } = setup();
    render(<PoolsPanel api={api} />);
    fireEvent.change(await screen.findByLabelText("small: Art"), { target: { value: "embedding" } });
    fireEvent.click(screen.getByRole("button", { name: "Pools speichern" }));
    await waitFor(() => expect(mock.calls.some((c) => c.url === "/api/scheduling/model-kinds")).toBe(true));
    expect(mock.calls.find((c) => c.url === "/api/scheduling/model-kinds")?.body).toEqual({
      model_kinds: { small: ["embedding"] },
    });
  });

  it("shows the speed measured in real answers, only where there is one", async () => {
    const { api } = setup();
    render(<PoolsPanel api={api} />);
    expect(await screen.findByText(/33[.,]1 Token\/s gemessen/)).toBeTruthy();
    expect(screen.getAllByText(/Token\/s gemessen/)).toHaveLength(1);
  });

  it("proves a model on a node and shows what the probes found", async () => {
    const { api, mock } = setup();
    render(<PoolsPanel api={api} />);
    fireEvent.click(await screen.findByRole("button", { name: "node-nvidia: big Prüfen" }));
    await waitFor(() => expect(mock.calls.some((c) => c.url === "/api/scheduling/model-probes")).toBe(true));
    expect(mock.calls.find((c) => c.url === "/api/scheduling/model-probes")?.body).toEqual({
      model: "big",
      node: "node-nvidia",
    });
    await waitFor(() =>
      expect(screen.getByTestId("proof-big").textContent).toBe("Werkzeugaufruf: ja · Einbettung: nein"),
    );
  });

  it("sends no pool for a node where every model is on", async () => {
    const { api, mock } = setup();
    render(<PoolsPanel api={api} />);
    await screen.findByLabelText("node-nvidia: big verfügbar");
    fireEvent.click(screen.getByRole("button", { name: "Pools speichern" }));
    await waitFor(() => expect(mock.calls.some((c) => c.method === "PUT")).toBe(true));
    expect(mock.calls.find((c) => c.method === "PUT")?.body).toEqual({ pools: {} });
  });

  it("sets a house model, which the node then keeps loaded, and a worker", async () => {
    const { api, mock } = setup();
    render(<PoolsPanel api={api} />);
    fireEvent.change(await screen.findByLabelText("node-nvidia: Hausmodell"), {
      target: { value: "big" },
    });
    fireEvent.click(screen.getByLabelText("node-nvidia: Worker für Hintergrundarbeit"));
    fireEvent.click(screen.getByRole("button", { name: "Pools speichern" }));
    await waitFor(() =>
      expect(mock.calls.some((c) => c.url === "/api/nodes/n1/residents/big")).toBe(true),
    );
    expect(mock.calls.find((c) => c.url === "/api/scheduling/house")?.body).toEqual({
      house: { "node-nvidia": "big" },
      workers: ["node-nvidia"],
    });
    expect(mock.calls.find((c) => c.url === "/api/nodes/n1/residents/big")?.body).toEqual({
      priority: 50,
      auto_reload: true,
    });
  });

  it("shows the tags of a model and filters by them", async () => {
    const { api } = setup();
    render(<PoolsPanel api={api} />);
    expect((await screen.findByTestId("tags-node-nvidia-big")).textContent).toContain("moe");
    fireEvent.click(screen.getByRole("button", { name: "small" }));
    expect(screen.queryByTestId("tags-node-nvidia-big")).toBeNull();
    expect(screen.getByTestId("tags-node-nvidia-small")).toBeTruthy();
  });
});
