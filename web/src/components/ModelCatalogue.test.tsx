import { cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";
import { ApiClient } from "../api/client";
import { modelHref, parseRoute } from "../route";
import { makeNode } from "../test/fixtures";
import { mockFetch } from "../test/mockFetch";
import { buildCatalogue, ModelCatalogue } from "./ModelCatalogue";

const amd = makeNode({
  id: "a",
  name: "node-amd",
  models: [
    {
      name: "qwen-7b",
      size_bytes: 4_500_000_000,
      loaded: true,
      vram_used_mb: 5200,
      backend: "vulkan",
    },
  ],
});
const nvidia = makeNode({ id: "b", name: "node-nvidia" }); // qwen-7b (not loaded), llama-8b (loaded)
const laptop = makeNode({
  id: "c",
  name: "node-laptop",
  connected: false,
  state: "offline",
  models: [{ name: "qwen-7b", loaded: false, vram_used_mb: null }],
});

describe("buildCatalogue", () => {
  it("groups models by name across nodes", () => {
    const entries = buildCatalogue([nvidia, laptop, amd]);
    expect(entries.map((e) => e.name)).toEqual(["llama-8b", "qwen-7b"]);
    const qwen = entries[1]!;
    expect(qwen.sizeBytes).toBe(4_500_000_000);
    expect(qwen.placements.map((p) => [p.nodeName, p.loaded, p.connected])).toEqual([
      ["node-amd", true, true],
      ["node-laptop", false, false],
      ["node-nvidia", false, true],
    ]);
  });

  it("is empty without models", () => {
    expect(buildCatalogue([makeNode({ models: [] })])).toEqual([]);
  });
});

afterEach(cleanup);

const api = new ApiClient({ token: "t", fetchImpl: mockFetch({}).fetch });

describe("ModelCatalogue", () => {
  it("shows one row per model with stored/loaded counts and links to the detail page", () => {
    const tool = makeNode({ id: "d", name: "node-tools", models: [], allowed_actions: ["tools.run"] });
    render(<ModelCatalogue api={api} nodes={[amd, nvidia, laptop, tool]} loaded error={null} />);
    expect(screen.getByTestId("catalogue-summary").textContent).toBe(
      "2 Modelle auf 3 Nodes, davon 2 geladen",
    );
    const qwen = screen.getByTestId("catalogue-row-qwen-7b");
    // the tool-only node does not count: 3 nodes can hold models
    expect(within(qwen).getByTestId("catalogue-stored").textContent).toBe("3 von 3 Nodes");
    expect(within(qwen).getByTestId("catalogue-loaded").textContent).toBe("1 von 3 Nodes");
    expect(
      within(qwen).getByRole("link", { name: "Seite von qwen-7b öffnen" }).getAttribute("href"),
    ).toBe("#/admin/models/qwen-7b");
    const llama = screen.getByTestId("catalogue-row-llama-8b");
    expect(within(llama).getByTestId("catalogue-stored").textContent).toBe("1 von 3 Nodes");
    expect(within(llama).getByTestId("catalogue-loaded").textContent).toBe("1 von 3 Nodes");
  });

  it("uses the node role: a tool node is neither listed nor counted", () => {
    const gpu = makeNode({ id: "g", name: "node-gpu", roles: ["gpu"], models: [amd.models[0]!] });
    const tools = makeNode({ id: "t", name: "node-tools", roles: ["tools"], models: [amd.models[0]!] });
    render(<ModelCatalogue api={api} nodes={[gpu, tools]} loaded error={null} />);
    expect(screen.getByTestId("catalogue-summary").textContent).toBe(
      "1 Modelle auf 1 Nodes, davon 1 geladen",
    );
    expect(screen.getByTestId("catalogue-row-qwen-7b").textContent).toContain("1 von 1 Nodes");
  });

  it("unfolds the node list of a model in place", () => {
    render(<ModelCatalogue api={api} nodes={[amd, nvidia]} loaded error={null} />);
    expect(screen.queryByTestId("model-row-a")).toBeNull();
    const toggle = screen.getByRole("button", { name: "Nodes von qwen-7b anzeigen" });
    expect(toggle.getAttribute("aria-expanded")).toBe("false");
    fireEvent.click(toggle);
    const detail = screen.getByTestId("catalogue-detail-qwen-7b");
    expect(within(detail).getByTestId("model-row-a")).toBeTruthy();
    expect(within(detail).getByTestId("model-row-b")).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "Nodes von qwen-7b ausblenden" }));
    expect(screen.queryByTestId("catalogue-detail-qwen-7b")).toBeNull();
  });

  it("counts all nodes when the allowed actions are unknown", () => {
    render(<ModelCatalogue api={api} nodes={[amd, nvidia]} loaded error={null} />);
    expect(screen.getByTestId("catalogue-row-qwen-7b").textContent).toContain("2 von 2 Nodes");
  });

  it("shows an empty state", () => {
    render(<ModelCatalogue api={api} nodes={[makeNode({ models: [] })]} loaded error={null} />);
    expect(screen.getByText(/Noch keine Modelle/)).toBeTruthy();
  });
});

describe("route", () => {
  it("parses the catalogue and model routes", () => {
    expect(parseRoute("#/models")).toEqual({ view: "models" });
    expect(parseRoute("#/models/")).toEqual({ view: "models" });
    expect(parseRoute("#/admin/models/qwen-7b")).toEqual({ view: "model", name: "qwen-7b" });
    expect(parseRoute("#/models/a%2Fb%20c/")).toEqual({ view: "model", name: "a/b c" });
    expect(parseRoute("#/models/%E0%A4%A")).toEqual({ view: "models" });
    expect(modelHref("a/b c")).toBe("#/admin/models/a%2Fb%20c");
    expect(parseRoute(modelHref("x:1/y"))).toEqual({ view: "model", name: "x:1/y" });
    expect(parseRoute("#/nodes/x")).toEqual({ view: "node", id: "x" });
    expect(parseRoute("#/nodes")).toEqual({ view: "overview" });
    expect(parseRoute("#/")).toEqual({ view: "start" });
  });
});
