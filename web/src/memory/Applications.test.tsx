import {
  fireEvent,
  render,
  screen,
  waitFor,
  within,
} from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { ApiClient } from "../api/client";
import { jsonResponse, mockFetch } from "../test/mockFetch";
import type { MemoryItem, MemoryTag, MemoryTagTree } from "../types";
import { Placement } from "./Placement";
import { SortBanner } from "./SortBanner";
import { SensitiveReleases } from "./SensitiveReleases";
import { TagTree } from "./TagTree";
import { Reports } from "./Reports";

afterEach(() => vi.restoreAllMocks());

const NOTE = {
  id: "a",
  text: "Die Tests laufen mit pytest",
  status: "active",
  application: "chat",
  tags: ["work"],
} as unknown as MemoryItem;

function client(routes: Record<string, unknown>) {
  const mock = mockFetch(routes);
  return { mock, api: new ApiClient({ token: "t", fetchImpl: mock.fetch }) };
}

describe("Placement", () => {
  it("moves a note to a program, or into the core", async () => {
    const { api, mock } = client({ "PATCH /api/memory/a/placement": NOTE });
    const act = vi.fn(async (fn: () => Promise<unknown>) => void (await fn()));
    const { container } = render(
      <Placement api={api} item={NOTE} tags={TREE.tags} act={act} />,
    );
    const select = container.querySelector("select") as HTMLSelectElement;
    expect(select.value).toBe("chat");
    fireEvent.change(select, { target: { value: "dev" } });
    await waitFor(() =>
      expect(
        mock.calls.find((c) => c.url === "/api/memory/a/placement")?.body,
      ).toEqual({ application: "dev" }),
    );
    fireEvent.change(select, { target: { value: "" } });
    await waitFor(() =>
      expect(
        mock.calls.filter((c) => c.url === "/api/memory/a/placement")[1]?.body,
      ).toEqual({ application: null }),
    );
  });

  it("adds and removes tags", async () => {
    const { api, mock } = client({ "PATCH /api/memory/a/placement": NOTE });
    const act = vi.fn(async (fn: () => Promise<unknown>) => void (await fn()));
    render(<Placement api={api} item={NOTE} tags={TREE.tags} act={act} />);
    expect(
      screen.getAllByTestId("memory-tag").map((t) => t.textContent?.trim()),
    ).toEqual(["work ×"]);
    fireEvent.change(screen.getByPlaceholderText("Schlagwort hinzufügen"), {
      target: { value: "health" },
    });
    fireEvent.submit(screen.getByPlaceholderText("Schlagwort hinzufügen"));
    await waitFor(() =>
      expect(
        mock.calls.find((c) => c.url === "/api/memory/a/placement")?.body,
      ).toEqual({ tags: ["work", "health"] }),
    );
    fireEvent.click(
      screen.getByRole("button", { name: "Schlagwort work entfernen" }),
    );
    await waitFor(() =>
      expect(
        mock.calls.filter((c) => c.url === "/api/memory/a/placement")[1]?.body,
      ).toEqual({ tags: [] }),
    );
  });
});

const SUGGESTIONS = {
  suggestions: [
    {
      id: "d",
      text: "Die Tests im Repository laufen mit pytest",
      kind: "operational",
      current: "chat",
      suggested: "dev",
    },
    {
      id: "m",
      text: "Home Assistant läuft als VM auf dem Proxmox-Host",
      kind: "operational",
      current: "chat",
      suggested: "admin",
    },
  ],
};

describe("SortBanner", () => {
  it("says nothing when there is nothing to sort", async () => {
    const { api, mock } = client({
      "GET /api/memory/sort": { suggestions: [] },
    });
    const { container } = render(
      <SortBanner api={api} tick={0} onDone={vi.fn()} />,
    );
    await waitFor(() =>
      expect(mock.calls.some((c) => c.url === "/api/memory/sort")).toBe(true),
    );
    expect(container.querySelector("[data-testid=memory-sort]")).toBeNull();
  });

  it("moves the accepted suggestions in one go and leaves the others", async () => {
    const onDone = vi.fn();
    const { api, mock } = client({
      "GET /api/memory/sort": SUGGESTIONS,
      "POST /api/memory/sort": { moved: 1 },
    });
    render(<SortBanner api={api} tick={0} onDone={onDone} />);
    const banner = await screen.findByTestId("memory-sort");
    expect(banner.textContent).toContain("2 Notiz(en)");
    fireEvent.click(within(banner).getByRole("button", { name: "Ansehen" }));
    const rows = within(banner).getAllByRole("combobox") as HTMLSelectElement[];
    expect(rows.map((r) => r.value)).toEqual(["dev", "admin"]); // the suggestions are preselected
    fireEvent.change(rows[1] as HTMLSelectElement, { target: { value: "" } }); // keep this one in Chat
    fireEvent.click(
      within(banner).getByRole("button", { name: "1 verschieben" }),
    );
    await waitFor(() => expect(onDone).toHaveBeenCalled());
    expect(mock.calls.find((c) => c.method === "POST")?.body).toEqual({
      assignments: [{ id: "d", application: "dev" }],
    });
  });

  it("can be put off", async () => {
    const { api } = client({ "GET /api/memory/sort": SUGGESTIONS });
    render(<SortBanner api={api} tick={0} onDone={vi.fn()} />);
    fireEvent.click(await screen.findByRole("button", { name: "Später" }));
    expect(screen.queryByTestId("memory-sort")).toBeNull();
  });

  it("says when the notes could not be moved", async () => {
    const { api } = client({
      "GET /api/memory/sort": SUGGESTIONS,
      "POST /api/memory/sort": () => jsonResponse({ detail: "down" }, 503),
    });
    render(<SortBanner api={api} tick={0} onDone={vi.fn()} />);
    fireEvent.click(await screen.findByRole("button", { name: /verschieben/ }));
    expect(await screen.findByRole("alert")).toBeTruthy();
  });
});

function tag(
  over: Partial<MemoryTag> & { id: string; name: string },
): MemoryTag {
  return {
    parent_id: null,
    description: "",
    responsible: null,
    sensitive: null,
    released: null,
    effective: { responsible: "person", sensitive: false, released: [] },
    created_by: "seed",
    reviewed: true,
    root: false,
    notes: 0,
    ...over,
  };
}

const TREE: MemoryTagTree = {
  tags: [
    tag({ id: "p", name: "person", root: true, responsible: "person" }),
    tag({
      id: "h",
      name: "health",
      parent_id: "p",
      sensitive: true,
      effective: { responsible: "person", sensitive: true, released: [] },
    }),
    tag({
      id: "i",
      name: "infrastructure",
      root: true,
      responsible: "admin",
      effective: { responsible: "admin", sensitive: false, released: [] },
    }),
    tag({
      id: "n",
      name: "nas",
      parent_id: "i",
      created_by: "agent",
      reviewed: false,
      notes: 2,
      effective: { responsible: "admin", sensitive: false, released: [] },
    }),
  ],
  programs: ["chat", "research", "dev", "admin"],
  responsible: ["person", "chat", "research", "dev", "admin"],
  max_depth: 4,
};

describe("TagTree", () => {
  it("shows the tree with who decides, what is sensitive and what an assistant added", async () => {
    const { api } = client({ "GET /api/memory/tags": TREE });
    render(<TagTree api={api} />);
    const nodes = await screen.findAllByTestId("tag-node");
    expect(nodes).toHaveLength(4);
    expect(nodes[0]?.textContent).toContain("Person");
    expect(screen.getByTestId("tags-unreviewed").textContent).toContain("1");
    expect(screen.getByTestId("tag-sensitive").textContent).toContain("heikel");
    const responsible = screen
      .getAllByTestId("tag-responsible")
      .map((x) => x.textContent);
    expect(responsible.some((x) => x?.includes("Admin"))).toBe(true);
  });

  it("keeps a new tag, adds a child and merges", async () => {
    const { api, mock } = client({
      "GET /api/memory/tags": TREE,
      "PATCH /api/memory/tags/n": TREE,
      "POST /api/memory/tags": TREE,
      "POST /api/memory/tags/n/merge": TREE,
    });
    render(<TagTree api={api} />);
    fireEvent.click(await screen.findByRole("button", { name: "behalten" }));
    await waitFor(() =>
      expect(mock.calls.find((c) => c.method === "PATCH")?.body).toEqual({
        reviewed: true,
      }),
    );
    const edits = screen.getAllByRole("button", { name: "bearbeiten" });
    fireEvent.click(edits[3] as HTMLElement); // nas
    const box = screen.getByTestId("tag-edit");
    fireEvent.change(within(box).getByPlaceholderText("Neuer Tag unter nas"), {
      target: { value: "backup" },
    });
    fireEvent.submit(within(box).getByPlaceholderText("Neuer Tag unter nas"));
    await waitFor(() =>
      expect(
        mock.calls.find(
          (c) => c.url === "/api/memory/tags" && c.method === "POST",
        )?.body,
      ).toEqual({
        name: "backup",
        parent_id: "n",
      }),
    );
    fireEvent.change(within(box).getByLabelText("Zusammenführen mit"), {
      target: { value: "i" },
    });
    fireEvent.click(
      within(box).getByRole("button", { name: "Zusammenführen" }),
    );
    await waitFor(() =>
      expect(
        mock.calls.find((c) => c.url === "/api/memory/tags/n/merge")?.body,
      ).toEqual({ into: "i" }),
    );
  });
});

describe("SensitiveReleases", () => {
  it("releases a sensitive branch for this program only", async () => {
    const { api, mock } = client({
      "GET /api/memory/tags": TREE,
      "PATCH /api/memory/tags/h": TREE,
    });
    render(<SensitiveReleases api={api} program="admin" />);
    const box = (await screen.findByRole("checkbox")) as HTMLInputElement;
    expect(box.checked).toBe(false);
    fireEvent.click(box);
    await waitFor(() =>
      expect(mock.calls.find((c) => c.method === "PATCH")?.body).toEqual({
        released: ["admin"],
      }),
    );
  });

  it("says when the branches cannot be loaded", async () => {
    const { api } = client({
      "GET /api/memory/tags": () => jsonResponse({ detail: "down" }, 503),
    });
    render(<SensitiveReleases api={api} program="chat" />);
    expect(await screen.findByRole("alert")).toBeTruthy();
  });
});

describe("Reports", () => {
  const DECIDED = {
    id: "r1",
    kind: "discrepancy",
    recipient: "admin",
    sender: null,
    topic: "backup",
    status: "decided",
    reason: "",
    message: "",
    decision: { outcome: "decided", why: "checked on the NAS" },
    created_at: null,
    decided_at: null,
    a: { id: "a", text: "um drei", status: "active", application: "chat" },
    b: { id: "b", text: "um zwei", status: "active", application: "admin" },
  };
  const NOTICE = {
    ...DECIDED,
    id: "n1",
    kind: "notice",
    recipient: "chat",
    sender: "admin",
    status: "open",
    message: "Die Uhrzeit war falsch.",
    b: null,
  };

  it("shows a program the differences it decided", async () => {
    const { api } = client({ "GET /api/memory/reports?recipient=admin": { reports: [DECIDED] } });
    render(<Reports api={api} recipient="admin" />);
    const item = await screen.findByTestId("report-discrepancy");
    expect(item.textContent).toContain("um drei");
    expect(within(item).getByTestId("report-outcome").textContent).toContain("checked on the NAS");
  });

  it("shows a notice from another program until it is read", async () => {
    const { api, mock } = client({
      "GET /api/memory/reports?recipient=chat": { reports: [NOTICE] },
      "POST /api/memory/reports/n1/seen": { id: "n1", status: "seen" },
    });
    render(<Reports api={api} recipient="chat" />);
    const notice = await screen.findByTestId("report-notice");
    expect(notice.textContent).toContain("Die Uhrzeit war falsch.");
    fireEvent.click(within(notice).getByRole("button", { name: "Gelesen" }));
    await waitFor(() =>
      expect(mock.calls.some((c) => c.url === "/api/memory/reports/n1/seen")).toBe(true),
    );
  });
});
