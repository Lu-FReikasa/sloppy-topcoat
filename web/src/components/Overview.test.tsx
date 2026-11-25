import { fireEvent, render, screen, within } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { makeNode } from "../test/fixtures";
import { Overview } from "./Overview";

describe("Overview (one row per machine)", () => {
  it("shows a single node as one row: readings once, in GB, the node as a badge and a tooltip", () => {
    render(<Overview nodes={[makeNode()]} loaded error={null} />);
    const row = screen.getByTestId("machine-node:n1");
    expect(within(row).getByText("node-nvidia")).toBeTruthy();
    expect(within(row).getByText("Rechnen")).toBeTruthy(); // the kind, a small badge
    expect(within(row).getByText("Verfügbar")).toBeTruthy();
    // no second card with the same readings
    expect(screen.queryByTestId("node-card-n1")).toBeNull();
    expect(screen.getAllByText("node-nvidia")).toHaveLength(1);
    // RAM and VRAM once, as a percentage; the exact figures in one unit as the tooltip
    const meters = within(row).getAllByRole("meter");
    expect(meters.map((m) => m.getAttribute("aria-label"))).toEqual(["RAM", "VRAM"]);
    expect(meters[0]!.getAttribute("aria-valuenow")).toBe("25");
    expect(meters[1]!.getAttribute("aria-valuenow")).toBe("33");
    expect(meters[1]!.closest("[title]")!.getAttribute("title")).toBe("VRAM: Fake GPU: 8,0 / 24,0 GB");
    expect(meters[0]!.closest("[title]")!.getAttribute("title")).toBe("RAM: 8,0 / 32,0 GB");
    expect(row.textContent).not.toContain("MB");
    expect(within(row).getByText("llama-8b")).toBeTruthy();
    expect(within(row).getByRole("link", { name: /node-nvidia/ }).getAttribute("href")).toBe(
      "#/admin/nodes/n1",
    );
  });

  it.each([
    ["available", "Verfügbar"],
    ["busy_gaming", "Spielt"],
    ["degraded", "Eingeschränkt"],
    ["offline", "Offline"],
  ] as const)("shows state %s as %s", (state, label) => {
    render(<Overview nodes={[makeNode({ state })]} loaded error={null} />);
    expect(screen.getByText(label)).toBeTruthy();
  });

  it("handles unknown VRAM usage", () => {
    const node = makeNode({
      gpus: [{ index: 0, name: "Fake AMD", vendor: "amd", vram_total_mb: 16384, vram_used_mb: null }],
      loaded: [],
    });
    render(<Overview nodes={[node]} loaded error={null} />);
    const vram = screen.getByRole("meter", { name: "VRAM" });
    expect(vram.getAttribute("aria-valuenow")).toBeNull();
    expect(vram.getAttribute("aria-valuetext")).toBe("Fake AMD: ? / 16,0 GB");
  });

  it("shows the empty state and the loading hint", () => {
    const { unmount } = render(<Overview nodes={[]} loaded error={null} />);
    expect(screen.getByText(/Noch keine Nodes gekoppelt/)).toBeTruthy();
    expect(screen.queryByTestId("machine-table")).toBeNull();
    unmount();
    render(<Overview nodes={[]} loaded={false} error={null} />);
    expect(screen.getByText("Lade …")).toBeTruthy();
  });

  it("has a table with column headers and a row that can be reached by keyboard", () => {
    render(<Overview nodes={[makeNode()]} loaded error={null} />);
    const table = screen.getByRole("table");
    expect(within(table).getAllByRole("columnheader").map((h) => h.textContent)).toEqual([
      "Maschine",
      "Zustand",
      "CPU",
      "GPU",
      "RAM",
      "VRAM",
      "Arbeit / Modelle",
    ]);
    expect(within(table).getByRole("rowheader").textContent).toContain("node-nvidia");
    expect(within(table).getByRole("link", { name: /node-nvidia/ })).toBeTruthy(); // focusable
  });

  it("opens the machine when the row is clicked", () => {
    render(<Overview nodes={[makeNode()]} loaded error={null} />);
    fireEvent.click(within(screen.getByTestId("machine-node:n1")).getByText("llama-8b"));
    expect(window.location.hash).toBe("#/admin/nodes/n1");
  });
});

describe("Overview by node kind", () => {
  const tool = makeNode({
    id: "t1",
    name: "tools-a",
    gpus: [],
    loaded: [],
    roles: ["tools"],
    capabilities: ["web.fetch", "web.search", "dns.lookup"],
  });
  const gpu = makeNode({ roles: ["gpu"] });

  it("shows a tool node with its tool groups and filters by kind", () => {
    render(<Overview nodes={[gpu, tool]} loaded error={null} />);
    const row = screen.getByTestId("machine-node:t1");
    expect(within(row).getByText("Werkzeuge")).toBeTruthy();
    expect(within(row).getByText("Werkzeuge: dns, web")).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: /^Werkzeug-Nodes/ }));
    expect(screen.queryByTestId("machine-node:n1")).toBeNull();
    expect(screen.getByTestId("machine-node:t1")).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: /^Rechen-Node/ }));
    expect(screen.queryByTestId("machine-node:t1")).toBeNull();
    expect(screen.getByTestId("machine-node:n1")).toBeTruthy();
  });

  it("shows no readings for an offline node", () => {
    render(<Overview nodes={[makeNode({ state: "offline", roles: ["gpu"] })]} loaded error={null} />);
    const row = screen.getByTestId("machine-node:n1");
    expect(within(row).getByText(/keine aktuellen Daten/)).toBeTruthy();
    expect(within(row).queryByRole("meter")).toBeNull();
    expect(row.textContent).not.toContain("GB");
    expect(row.textContent).not.toContain("llama-8b");
  });
});
