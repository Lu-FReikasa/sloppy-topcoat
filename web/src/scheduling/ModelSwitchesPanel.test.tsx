import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { ApiClient } from "../api/client";
import { jsonResponse, mockFetch } from "../test/mockFetch";
import { ModelSwitchesPanel } from "./ModelSwitchesPanel";

const view = {
  days: 7,
  choice_mode: "alt",
  totals: {
    requests: 10,
    loads: 4,
    switches: 2,
    review_requests: 3,
    review_loads: 1,
    waited_seconds_on_loads: 12.5,
    loads_by_work: { review: 1, chat: 3 },
    loads_by_reason: { "-": 4 },
  },
  review_load_share: 0.25,
  cells: [
    {
      date: "2026-11-06",
      node: "node-nvidia",
      requests: 10,
      loads: 4,
      switches: 2,
      review_loads: 1,
      loads_by_work: { review: 1, chat: 3 },
    },
  ],
};

function setup(body: unknown) {
  const mock = mockFetch({
    "GET /api/scheduling/model-switches?days=7": () => jsonResponse(body),
  });
  return new ApiClient({ token: "t", fetchImpl: mock.fetch });
}

describe("ModelSwitchesPanel", () => {
  it("shows the loads, the changes and the share the second look caused", async () => {
    render(<ModelSwitchesPanel api={setup(view)} />);
    const totals = await screen.findByTestId("model-switches-totals");
    expect(totals.textContent).toContain("4");
    expect(totals.textContent).toContain("25 %");
    const rows = within(screen.getByTestId("model-switches-table")).getAllByRole("row");
    const cells = within(rows[1]!).getAllByRole("cell").map((c) => c.textContent);
    expect(cells).toEqual(["2026-11-06", "node-nvidia", "10", "4", "2", "1"]);
  });

  it("says so when nothing was requested", async () => {
    render(
      <ModelSwitchesPanel
        api={setup({ ...view, totals: { ...view.totals, requests: 0 }, cells: [], review_load_share: null })}
      />,
    );
    expect(await screen.findByText(/No requests in this period|Keine Anfragen/)).toBeTruthy();
    expect(screen.queryByTestId("model-switches-table")).toBeNull();
  });
});

describe("ModelSwitchesPanel choice switch", () => {
  it("shows the current choice and sends the new one to the hub", async () => {
    const calls: unknown[] = [];
    const mock = mockFetch({
      "GET /api/scheduling/model-switches?days=7": () => jsonResponse(view),
      "PUT /api/scheduling/choice-mode": (init: RequestInit | undefined) => {
        calls.push(JSON.parse(String(init?.body)));
        return jsonResponse({ mode: "neu" });
      },
    });
    const api = new ApiClient({ token: "t", fetchImpl: mock.fetch });
    render(<ModelSwitchesPanel api={api} />);
    const select = (await screen.findByLabelText(/Choice of the first machine|Wahl der ersten Maschine/)) as HTMLSelectElement;
    expect(select.value).toBe("alt");
    fireEvent.change(select, { target: { value: "neu" } });
    await waitFor(() => expect(select.value).toBe("neu"));
    expect(calls).toEqual([{ mode: "neu" }]);
  });
});
