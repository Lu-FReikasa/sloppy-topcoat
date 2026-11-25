import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { ApiClient } from "../api/client";
import { jsonResponse, mockFetch } from "../test/mockFetch";
import { MeasurementsPanel } from "./MeasurementsPanel";

const row = (over: Record<string, unknown> = {}) => ({
  application: "chat",
  model: "big",
  node: "node-nvidia",
  requests: 12,
  failed: 1,
  cancelled: 1,
  cut_off: 2,
  waited_seconds: 3.5,
  generated_tps: 33.1,
  prompt_tps: 410,
  cached_share: 0.4,
  ...over,
});

function setup(rows: unknown[]) {
  const mock = mockFetch({
    "GET /api/scheduling/measurements?days=7": () => jsonResponse({ days: 7, rows }),
    "GET /api/scheduling/measurements?days=30": () => jsonResponse({ days: 30, rows }),
  });
  return { mock, api: new ApiClient({ token: "t", fetchImpl: mock.fetch }) };
}

describe("MeasurementsPanel", () => {
  it("shows the figures per application, model and node", async () => {
    const { api } = setup([row(), row({ application: "dev", generated_tps: null, cached_share: null })]);
    render(<MeasurementsPanel api={api} />);
    const table = within(await screen.findByTestId("measurements-table"));
    const rows = table.getAllByRole("row");
    expect(rows).toHaveLength(3); // header and two rows
    const first = within(rows[1]!).getAllByRole("cell").map((c) => c.textContent);
    expect(first[0]).toBe("chat");
    expect(first[1]).toContain("big");
    expect(first[3]).toBe("2"); // failed and stopped together
    expect(first[8]).toBe("40 %");
    const second = within(rows[2]!).getAllByRole("cell").map((c) => c.textContent);
    expect(second[6]).toBe("–"); // no speed measured
    expect(second[8]).toBe("–");
  });

  it("asks again for another period", async () => {
    const { api, mock } = setup([row()]);
    render(<MeasurementsPanel api={api} />);
    await screen.findByTestId("measurements-table");
    fireEvent.change(screen.getByLabelText("Zeitraum"), { target: { value: "30" } });
    await waitFor(() =>
      expect(mock.calls.some((c) => c.url === "/api/scheduling/measurements?days=30")).toBe(true),
    );
  });

  it("says so when nothing was requested", async () => {
    const { api } = setup([]);
    render(<MeasurementsPanel api={api} />);
    expect(await screen.findByText("Keine Anfragen in diesem Zeitraum.")).toBeTruthy();
  });
});
