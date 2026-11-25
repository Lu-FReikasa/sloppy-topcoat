import {
  fireEvent,
  render,
  screen,
  waitFor,
  within,
} from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { ApiClient } from "../api/client";
import { INTENT_EVENT, takeIntent } from "../layout/intent";
import { jsonResponse, mockFetch } from "../test/mockFetch";
import type { InboxItem } from "../types";
import { InboxPage, rowsOf } from "./InboxPage";

function item(id: string, over: Partial<InboxItem> = {}): InboxItem {
  return {
    id,
    application: "chat",
    kind: "task_done",
    title: `Item ${id}`,
    summary: "",
    ref: {},
    urgent: false,
    unresolved: false,
    read: false,
    pinned: false,
    filed: false,
    created_at: "2026-10-22T09:00:00Z",
    read_at: null,
    resolved_at: null,
    ...over,
  };
}

const counts = (items: InboxItem[]) => ({
  unread: items.filter((i) => !i.read).length,
  urgent: items.filter((i) => i.unresolved).length,
  applications: {} as Record<string, number>,
});

/** A small hub: the items it holds, what each route does to them. */
function hub(start: InboxItem[]) {
  let items = start;
  const patch = (id: string, over: Partial<InboxItem>) => {
    items = items.map((i) => (i.id === id ? { ...i, ...over } : i));
    return items.find((i) => i.id === id)!;
  };
  const mock = mockFetch({
    "GET /api/inbox?view=inbox": () =>
      jsonResponse({
        items: items.filter((i) => !i.read || i.unresolved),
        counts: counts(items),
      }),
    "GET /api/inbox?view=filed": () =>
      jsonResponse({
        items: items.filter((i) => i.filed && !i.pinned),
        counts: counts(items),
      }),
    "POST /api/inbox/a/open": () =>
      jsonResponse(patch("a", { read: true, filed: true })),
    "POST /api/inbox/u/open": () => jsonResponse(patch("u", { read: true })),
    "POST /api/inbox/u/resolve": () =>
      jsonResponse(
        patch("u", {
          unresolved: false,
          filed: true,
          resolved_at: "2026-10-22T10:00:00Z",
        }),
      ),
    "POST /api/inbox/a/pin": () =>
      jsonResponse(patch("a", { pinned: true, filed: false })),
    "POST /api/inbox/read-all": () => {
      const before = items.filter((i) => !i.read && !i.unresolved).length;
      items = items.map((i) => (i.unresolved ? i : { ...i, read: true, filed: !i.pinned }));
      return jsonResponse({ filed: before });
    },
  });
  const api = new ApiClient({ token: "t", fetchImpl: mock.fetch });
  return { api, mock };
}

describe("InboxPage", () => {
  afterEach(() => vi.restoreAllMocks());

  it("marks everything read in one click; what waits for an action stays", async () => {
    const { api, mock } = hub([item("a"), item("b"), item("u", { urgent: true, unresolved: true })]);
    render(<InboxPage api={api} tick={0} />);
    fireEvent.click(await screen.findByRole("button", { name: "Alle als gelesen markieren" }));
    await waitFor(() => expect(screen.queryByText("Item a")).toBeNull());
    expect(screen.getByText("Item u")).toBeTruthy();
    expect(mock.calls.some((c) => c.method === "POST" && c.url === "/api/inbox/read-all")).toBe(true);
  });

  it("lists what waits, each with the application it comes from", async () => {
    const { api } = hub([
      item("a", { application: "admin", title: "Backup done" }),
      item("b", { application: "dev" }),
    ]);
    render(<InboxPage api={api} tick={0} />);
    expect(await screen.findByText("Backup done")).toBeTruthy();
    const tags = screen.getAllByTestId("app-tag").map((t) => t.textContent);
    expect(tags).toContain("Admin");
    expect(tags).toContain("Dev");
    expect((await screen.findByTestId("inbox-unread")).textContent).toBe("2");
  });

  it("says so when nothing waits", async () => {
    const { api } = hub([]);
    render(<InboxPage api={api} tick={0} />);
    expect(await screen.findByTestId("inbox-empty")).toBeTruthy();
  });

  it("opens an item on a click: it is read and leaves the list", async () => {
    const { api, mock } = hub([
      item("a", { title: "Backup done", summary: "All good." }),
    ]);
    render(<InboxPage api={api} tick={0} />);
    fireEvent.click(await screen.findByRole("button", { name: /Backup done/ }));
    const reading = await screen.findByTestId("inbox-reading");
    expect(within(reading).getByText("All good.")).toBeTruthy();
    expect(
      mock.calls.some(
        (c) => c.method === "POST" && c.url === "/api/inbox/a/open",
      ),
    ).toBe(true);
    await waitFor(() => expect(screen.getByTestId("inbox-empty")).toBeTruthy());
    expect(screen.getByTestId("inbox-reading")).toBeTruthy(); // still open until it is closed
    fireEvent.click(within(reading).getByRole("button", { name: "Schließen" }));
    expect(screen.queryByTestId("inbox-reading")).toBeNull();
  });

  it("keeps an urgent item in the list after it was read, until it is settled", async () => {
    const urgent = item("u", {
      title: "Needs a click",
      urgent: true,
      unresolved: true,
    });
    const { api } = hub([urgent]);
    render(<InboxPage api={api} tick={0} />);
    fireEvent.click(
      await screen.findByRole("button", { name: /Needs a click/ }),
    );
    const reading = await screen.findByTestId("inbox-reading");
    expect(
      within(reading).getByText("braucht deine Aufmerksamkeit"),
    ).toBeTruthy();
    expect(screen.queryByTestId("inbox-empty")).toBeNull(); // read, but it stays
    fireEvent.click(
      within(reading).getByRole("button", { name: "Als erledigt markieren" }),
    );
    await waitFor(() => expect(screen.getByTestId("inbox-empty")).toBeTruthy());
  });

  it("pins an open item to the board", async () => {
    const { api, mock } = hub([item("a", { title: "Keep me" })]);
    render(<InboxPage api={api} tick={0} />);
    fireEvent.click(await screen.findByRole("button", { name: /Keep me/ }));
    const reading = await screen.findByTestId("inbox-reading");
    fireEvent.click(
      within(reading).getByRole("button", { name: "An die Pinnwand heften" }),
    );
    await waitFor(() =>
      expect(
        mock.calls.find((c) => c.url === "/api/inbox/a/pin")?.body,
      ).toEqual({ pinned: true }),
    );
    expect(
      await within(screen.getByTestId("inbox-reading")).findByRole("button", {
        name: "Von der Pinnwand nehmen",
      }),
    ).toBeTruthy();
  });

  it("shows the window an item is about", async () => {
    const heard = vi.fn();
    window.addEventListener(INTENT_EVENT, heard);
    const { api } = hub([
      item("a", {
        title: "A task",
        ref: { module: "page.tasks", props: { id: "t1" } },
      }),
    ]);
    render(<InboxPage api={api} tick={0} />);
    fireEvent.click(await screen.findByRole("button", { name: /A task/ }));
    fireEvent.click(await screen.findByRole("button", { name: "Zeigen" }));
    expect(heard).toHaveBeenCalled();
    expect(takeIntent()).toEqual({ module: "page.tasks", props: { id: "t1" } });
    window.removeEventListener(INTENT_EVENT, heard);
  });

  it("keeps what was read in the filed corner", async () => {
    const { api } = hub([
      item("a", { title: "Old news", read: true, filed: true }),
    ]);
    render(<InboxPage api={api} tick={0} />);
    fireEvent.click(await screen.findByRole("tab", { name: "Abgelegt" }));
    expect(await screen.findByText("Old news")).toBeTruthy();
  });

  it("offers a retry when the inbox cannot be loaded", async () => {
    const mock = mockFetch({
      "GET /api/inbox?view=inbox": () => jsonResponse({ detail: "down" }, 503),
    });
    const api = new ApiClient({ token: "t", fetchImpl: mock.fetch });
    render(<InboxPage api={api} tick={0} />);
    expect(await screen.findByRole("alert")).toBeTruthy();
    expect(screen.getByRole("button", { name: "Noch einmal" })).toBeTruthy();
  });
});

describe("rowsOf", () => {
  it("bundles the second look's notes per application in the place of the first, a single one stays an item", () => {
    const rows = rowsOf([
      item("a", { kind: "review_hint", application: "dev" }),
      item("b"),
      item("c", { kind: "review_error", application: "dev" }),
      item("d", { kind: "review_hint", application: "chat" }),
    ]);
    expect(rows.map((r) => ("item" in r ? r.item.id : `${r.bundle}:${r.items.map((i) => i.id).join("")}`))).toEqual([
      "dev:ac",
      "b",
      "d",
    ]);
  });
});
