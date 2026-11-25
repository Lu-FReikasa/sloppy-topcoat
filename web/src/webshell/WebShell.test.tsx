import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { beforeEach, describe, expect, it } from "vitest";
import { ApiClient } from "../api/client";
import { requestOpen } from "../layout/intent";
import type { ModuleCtx } from "../layout/modules/render";
import type { UiBootstrap } from "../layout/types";
import { bootstrap, setNarrow, workspace } from "../test/layoutFixtures";
import { jsonResponse, mockFetch } from "../test/mockFetch";
import { WebShell } from "./WebShell";
import { shellChoice, wantsNav } from "./shell";

function boot(): UiBootstrap {
  const b = bootstrap([
    workspace("chat", ["page.inbox", "page.library"], { icon: ">_", title: "Chat", position: 10 }),
    workspace("research", ["page.library", "page.access"], { icon: "~", title: "Research", position: 20 }),
    workspace("settings", ["page.access"], { icon: "::", title: "Settings", position: 95 }),
  ]);
  return { ...b, prefs: { hue: 140, look: "terminal", desktop: { open: [], minimised: [], focus: null, maximised: null, views: {} } } };
}

function show() {
  const mock = mockFetch({
    "GET /api/ui/bootstrap": () => jsonResponse(boot()),
    "GET /api/inbox/counts": { unread: 3, urgent: 0, applications: {} },
    "GET /api/inbox?view=inbox": { items: [] },
    "GET /api/keys": [],
    "GET /api/artifacts": { artifacts: [] },
  });
  const api = new ApiClient({ token: "t", fetchImpl: mock.fetch });
  const ctx: ModuleCtx = {
    api,
    me: { id: "u1", name: "Lu", role: "admin" },
    isAdmin: true,
    nodes: [],
    nodesLoaded: true,
    nodesError: null,
    refreshNodes: () => undefined,
    tick: 0,
    inboxTick: 0,
    epoch: 0,
    detail: null,
    dispatch: () => undefined,
  };
  render(<WebShell api={api} ctx={ctx} status="live" onSaveLanguage={() => undefined} onLogout={() => undefined} />);
}

describe("the web shell", () => {
  beforeEach(() => {
    setNarrow(false);
    window.location.hash = "#/w";
  });

  it("is the browser's default; windows only when chosen, never inside the client", () => {
    window.localStorage.removeItem("topcoat.shell");
    expect(shellChoice()).toBe("nav");
    expect(wantsNav("")).toBe(true);
    expect(wantsNav("?solo=chat")).toBe(false);
    expect(wantsNav("?overlay=1")).toBe(false);
    window.localStorage.setItem("topcoat.shell", "windows");
    expect(wantsNav("")).toBe(false);
  });

  it("lists the applications and the system apart, shows the views of the open one and moves between them", async () => {
    show();
    const side = await screen.findByRole("navigation", { name: "Navigation" });
    expect(within(side).getByRole("button", { name: /Posteingang/ }).textContent).toContain("3");
    // the inbox is on top, not again under Chat; Research opened lists its two views under it
    expect(within(side).getAllByRole("button", { name: /Posteingang/ })).toHaveLength(1);
    expect(within(side).queryByRole("button", { name: "Ablage" })).toBeNull();
    fireEvent.click(within(side).getByRole("button", { name: /Research/ }));
    await waitFor(() => expect(window.location.hash).toBe("#/w/research"));
    expect(within(side).getByRole("button", { name: "Ablage" })).toBeTruthy();
    expect(within(side).getByText("System")).toBeTruthy();
    // an application has its ⚙ to Settings; a view of Settings is one click away
    fireEvent.click(screen.getByRole("button", { name: /Einstellungen von Research/ }));
    await waitFor(() => expect(window.location.hash).toBe("#/w/settings"));
  });

  it("opens what a page asks for, in the program that has it", async () => {
    show();
    await screen.findByRole("navigation", { name: "Navigation" });
    requestOpen({ module: "page.access", props: {}, program: "research" });
    await waitFor(() => expect(window.location.hash).toBe("#/w/research"));
  });
});
