import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { ApiClient } from "../api/client";
import { jsonResponse, mockFetch } from "../test/mockFetch";
import { QualityPanel } from "./QualityPanel";

const row = (over: Record<string, unknown> = {}) => ({
  application: "chat",
  model: "big",
  looks: 20,
  ok: 10,
  hint: 6,
  error: 4,
  ok_share: 0.5,
  floor: 0.8,
  below_floor: true,
  ...over,
});

function setup(rows: unknown[], floors: Record<string, number> = { chat: 0.8 }) {
  const mock = mockFetch({
    "GET /api/scheduling/quality?days=30": () => jsonResponse({ days: 30, min_looks: 10, floors, rows }),
    "PUT /api/scheduling/quality-floor": () => jsonResponse({ floors: { chat: 0.9 } }),
  });
  return { mock, api: new ApiClient({ token: "t", fetchImpl: mock.fetch }) };
}

describe("QualityPanel", () => {
  it("counts the looks and marks a model below the floor", async () => {
    const { api } = setup([row(), row({ model: "small", ok_share: 0.95, below_floor: false })]);
    render(<QualityPanel api={api} />);
    const table = within(await screen.findByRole("table"));
    const rows = table.getAllByRole("row");
    expect(rows).toHaveLength(3);
    expect(rows[1]?.textContent).toContain("50 %");
    expect(rows[1]?.textContent).toContain("unter der Untergrenze");
    expect(rows[2]?.textContent).not.toContain("unter der Untergrenze");
  });

  it("sets a floor as a percentage and rejects nonsense before asking the hub", async () => {
    const { api, mock } = setup([row()]);
    render(<QualityPanel api={api} />);
    const field = await screen.findByRole("textbox");
    fireEvent.change(field, { target: { value: "150" } });
    fireEvent.click(screen.getByRole("button", { name: "Speichern" }));
    expect(await screen.findByText("Eine Untergrenze ist ein Anteil von 0 bis 100 Prozent.")).toBeTruthy();
    expect(mock.calls.some((c) => c.method === "PUT")).toBe(false);
    fireEvent.change(field, { target: { value: "90" } });
    fireEvent.click(screen.getByRole("button", { name: "Speichern" }));
    await waitFor(() => expect(mock.calls.some((c) => c.method === "PUT")).toBe(true));
    expect(mock.calls.find((c) => c.method === "PUT")?.body).toEqual({ application: "chat", floor: 0.9 });
  });

  it("says so when there are no looks", async () => {
    const { api } = setup([], {});
    render(<QualityPanel api={api} />);
    expect(await screen.findByText("Keine zweiten Blicke in diesem Zeitraum.")).toBeTruthy();
  });
});
