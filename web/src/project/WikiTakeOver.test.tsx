import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { ApiClient } from "../api/client";
import { mockFetch } from "../test/mockFetch";
import { WikiTakeOver } from "./WikiTakeOver";

const PREVIEW = {
  node_id: "n1",
  section: "B Hub",
  title: "B12 Wissensbasis",
  path: "B Hub/B12 Wissensbasis",
  page_id: "p1",
  new_page: false,
  version: 3,
  unchanged: false,
  chars: 4200,
};

function show(routes: Record<string, unknown>) {
  const mock = mockFetch(routes);
  const onError = vi.fn();
  render(<WikiTakeOver api={new ApiClient({ token: "t", fetchImpl: mock.fetch })} nodeId="n1" onError={onError} />);
  return { mock, onError };
}

describe("WikiTakeOver", () => {
  it("shows what would happen first and takes over only on the click on the card", async () => {
    const { mock } = show({
      "GET /api/project/nodes/n1/wiki": PREVIEW,
      "POST /api/project/nodes/n1/wiki": { id: "p1", version: 3 },
    });
    fireEvent.click(screen.getByRole("button", { name: "Ins Wiki übernehmen" }));
    const card = await screen.findByTestId("wiki-take-over-card");
    expect(card.textContent).toContain("Wiki: B Hub/B12 Wissensbasis – Version 3");
    expect(mock.calls.some((c) => c.method === "POST")).toBe(false);
    fireEvent.click(within(card).getByRole("button", { name: "Übernehmen" }));
    const done = await screen.findByRole("status");
    expect(done.textContent).toContain("Als Version 3 übernommen");
    expect(within(done).getByRole("link").getAttribute("href")).toBe("#/wiki/p1");
    await waitFor(() => expect(mock.calls.filter((c) => c.method === "POST")).toHaveLength(1));
  });

  it("offers nothing to take over when the page has the text already, and cancels", async () => {
    const { mock } = show({ "GET /api/project/nodes/n1/wiki": { ...PREVIEW, unchanged: true } });
    fireEvent.click(screen.getByRole("button", { name: "Ins Wiki übernehmen" }));
    const card = await screen.findByTestId("wiki-take-over-card");
    expect(card.textContent).toContain("hat diesen Text schon");
    expect(within(card).queryByRole("button", { name: "Übernehmen" })).toBeNull();
    fireEvent.click(within(card).getByRole("button", { name: "Abbrechen" }));
    expect(screen.queryByTestId("wiki-take-over-card")).toBeNull();
    expect(mock.calls.some((c) => c.method === "POST")).toBe(false);
  });
});
