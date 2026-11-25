// Every address of route.ts, loaded fresh (a bookmark, a shared link, a reload) in the navigation
// shell: the page the address names wins over the workspace the person used last (issue #76).
import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { App } from "../App";
import { TOKEN_KEY } from "../auth";
import { bootstrap, setNarrow, workspace } from "../test/layoutFixtures";
import { jsonResponse, mockFetch } from "../test/mockFetch";

/** The built-in programs of the hub (hub/src/topcoat_hub/ui/seeds.py), the person last used Sheets. */
function boot() {
  const b = bootstrap([
    workspace("chat", ["page.home", "page.inbox", "page.chat", "page.handovers", "page.library", "page.archive"], {
      title: "Start",
      position: 10,
    }),
    workspace("research", ["page.research", "page.knowledge", "page.library"], { title: "Research", position: 20 }),
    workspace("project", ["page.project"], { title: "Projects", position: 25 }),
    workspace("wiki", ["page.wiki"], { title: "Wiki", position: 27 }),
    workspace("sheets", ["page.sheets"], { title: "Sheets", position: 28 }),
    workspace("dev", ["page.workshop", "page.repos", "page.library"], { title: "Development", position: 40 }),
    workspace(
      "admin",
      [
        "page.overview",
        "page.models",
        "page.applications",
        "page.programs",
        "layout.workspaces",
        "page.sources",
        "page.care",
        "page.themes",
      ],
      { title: "Administration", position: 90 },
    ),
    workspace("settings", ["page.settings"], { title: "Settings", position: 95 }),
  ]);
  b.prefs.desktop = { ...b.prefs.desktop!, open: ["sheets"], focus: "sheets" };
  return b;
}

// the hub answers slower than the first render, as it does for real: the shell is ready only after
// the address has been read
const slow = (body: unknown) => () =>
  new Promise<Response>((done) => setTimeout(() => done(jsonResponse(body)), 30));

async function load(address: string) {
  window.history.replaceState(null, "", `/${address}`);
  const mock = mockFetch({
    "GET /api/me": { id: "u1", name: "Admin", role: "admin", language: "en" },
    "GET /api/nodes": [],
    "GET /api/stream": () => new Promise<Response>(() => undefined),
    "GET /api/ui/bootstrap": slow(boot()),
    "GET /api/inbox/counts": { unread: 0, urgent: 0, applications: {} },
  });
  vi.stubGlobal("fetch", mock.fetch);
  render(<App />);
  await screen.findByRole("navigation", { name: "Navigation" });
  // let the shell settle: the address it writes, a view opened a moment later
  await new Promise((r) => setTimeout(r, 80));
  return {
    crumb: document.querySelector(".ws-crumb > span")?.textContent ?? null,
    hash: window.location.hash,
    tab: document.querySelector('.ws-main [role="tab"][aria-selected="true"]')?.textContent ?? null,
    section: document.querySelector('.ws-main [aria-current="page"]')?.textContent ?? null,
    moved: document.querySelector(".ws-moved")?.textContent ?? null,
    asked: () => mock.calls.map((c) => c.url),
  };
}

interface Shown {
  hash: string;
  crumb: string;
  /** The tab or the section of Settings the page opens on. */
  tab?: string;
  /** What the page asked the hub for: the item the address names. */
  asks?: string;
  moved?: true;
}

const MOVED = "This page has moved or does not exist.";

// address -> what it shows. The person last used Sheets: only "#/", "#/w" and Sheets' own
// addresses may show it.
const ROUTES: Record<string, Shown> = {
  "#/": { hash: "#/w/sheets", crumb: "Sheets" },
  "#/w": { hash: "#/w/sheets", crumb: "Sheets" },
  "#/w/wiki": { hash: "#/w/wiki", crumb: "Wiki" },
  "#/talk": { hash: "#/w/chat", crumb: "Start › Chat" },
  // retired: the menu of everything is Start's overview of the applications
  "#/more": { hash: "#/w/chat", crumb: "Start › Applications" },
  "#/archive": { hash: "#/w/chat", crumb: "Start › Archive" },
  "#/nodes": { hash: "#/w/admin", crumb: "Administration › Machines", tab: "Machines" },
  "#/admin/nodes": { hash: "#/w/admin", crumb: "Administration › Machines", tab: "Machines" },
  "#/admin/nodes/n1": { hash: "#/w/admin", crumb: "Administration › Node", asks: "/api/nodes/n1" },
  "#/models": { hash: "#/w/admin", crumb: "Administration › Models", tab: "Models" },
  "#/admin/models/m1": { hash: "#/w/admin", crumb: "Administration › Model" },
  "#/access": { hash: "#/w/admin", crumb: "Administration › Access", tab: "API access" },
  "#/repos": { hash: "#/w/dev", crumb: "Development › Repositories" },
  "#/dev": { hash: "#/w/dev", crumb: "Development › Workshop" },
  "#/care": { hash: "#/w/admin", crumb: "Administration › Maintenance" },
  // retired pages that became a tab or a section
  "#/scheduling": { hash: "#/w/admin", crumb: "Administration › Models", tab: "Distribution" },
  "#/params": { hash: "#/w/admin", crumb: "Administration › Models", tab: "Parameters" },
  "#/identity": { hash: "#/w/admin", crumb: "Administration › Access", tab: "Identity" },
  "#/inventory": { hash: "#/w/admin", crumb: "Administration › Machines", tab: "Inventory" },
  "#/releases": { hash: "#/w/admin", crumb: "Administration › Machines", tab: "Releases" },
  "#/memory": { hash: "#/w/settings", crumb: "Settings", tab: "Memory" },
  "#/instructions": { hash: "#/w/settings", crumb: "Settings", tab: "Assistant" },
  "#/programs": { hash: "#/w/admin", crumb: "Administration › Flows" },
  "#/chat": { hash: "#/w/chat", crumb: "Start › Chat" },
  "#/chat/c1": { hash: "#/w/chat", crumb: "Start › Chat", asks: "c1" },
  "#/settings": { hash: "#/w/settings", crumb: "Settings", tab: "General" },
  "#/knowledge": { hash: "#/w/research", crumb: "Research › Knowledge" },
  "#/research": { hash: "#/w/research", crumb: "Research › Research" },
  "#/project": { hash: "#/w/project", crumb: "Projects" },
  "#/tasks": { hash: "#/w/chat", crumb: "Start › Tasks" },
  "#/tasks/t1": { hash: "#/w/chat", crumb: "Start › Tasks", asks: "t1" },
  "#/library": { hash: "#/w/chat", crumb: "Start › Library" },
  "#/wiki": { hash: "#/w/wiki", crumb: "Wiki" },
  "#/wiki/p1": { hash: "#/w/wiki", crumb: "Wiki", asks: "/api/wiki/pages/p1" },
  "#/sheets/s1": { hash: "#/w/sheets", crumb: "Sheets", asks: "s1" },
  "#/handovers": { hash: "#/w/chat", crumb: "Start › Hand-overs" },
  "#/handovers/h1": { hash: "#/w/chat", crumb: "Start › Hand-overs" },
  // nobody knows it: Start, with a note
  "#/nowhere": { hash: "#/w/chat", crumb: "Start › Applications", moved: true },
};

describe("an address loaded fresh in the navigation shell shows the page it names", () => {
  beforeEach(() => {
    window.localStorage.clear();
    window.localStorage.setItem(TOKEN_KEY, "tok");
    setNarrow(false);
  });
  afterEach(() => {
    cleanup();
    vi.unstubAllGlobals();
  });

  for (const [address, want] of Object.entries(ROUTES)) {
    it(address, async () => {
      const got = await load(address);
      expect(got.crumb).toBe(want.crumb);
      expect(got.hash).toBe(want.hash);
      if (want.tab) expect(got.tab ?? got.section).toBe(want.tab);
      if (want.asks) expect(got.asked().some((u) => u.includes(want.asks ?? ""))).toBe(true);
      expect(got.moved).toBe(want.moved ? MOVED : null);
    });
  }

  it("drops the note about a moved page once the person moves on", async () => {
    await load("#/nowhere");
    expect(screen.getByText(MOVED)).toBeTruthy();
    const side = screen.getByRole("navigation", { name: "Navigation" });
    fireEvent.click(within(side).getByRole("button", { name: /Wiki/ }));
    await waitFor(() => expect(window.location.hash).toBe("#/w/wiki"));
    expect(screen.queryByText(MOVED)).toBeNull();
  });

  it("follows a link inside an open page, to another program and within the one shown", async () => {
    const got = await load("#/w/wiki");
    window.location.hash = "#/sheets/s1";
    await waitFor(() => expect(got.asked().some((u) => u.includes("s1"))).toBe(true));
    await waitFor(() => expect(window.location.hash).toBe("#/w/sheets"));
    window.location.hash = "#/sheets/s2";
    await waitFor(() => expect(got.asked().some((u) => u.includes("s2"))).toBe(true));
    await waitFor(() => expect(window.location.hash).toBe("#/w/sheets"));
    expect(document.querySelector(".ws-crumb > span")?.textContent).toBe("Sheets");
  });

  it("tells where tasks come from on the bare tasks address", async () => {
    await load("#/tasks");
    expect(screen.getByRole("heading", { name: "No task open" })).toBeTruthy();
  });
});
