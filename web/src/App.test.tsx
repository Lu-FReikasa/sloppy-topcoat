import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { App } from "./App";
import { TOKEN_KEY } from "./auth";
import { bootstrap, setNarrow, workspace } from "./test/layoutFixtures";
import { jsonResponse, mockFetch } from "./test/mockFetch";

// The stream endpoint never answers in these tests.
const hangingStream = () => new Promise<Response>(() => undefined);

/** What the hub answers once somebody is signed in: who they are, the machines, the programs. The
 *  hub echoes a change of the preferences the way it stores it. */
function hub(me: Record<string, unknown>, extra: Record<string, unknown> = {}) {
  const boot = bootstrap([workspace("settings", ["page.access"], { icon: "::", title: "Settings" })]);
  return {
    "GET /api/me": me,
    "GET /api/nodes": [],
    "GET /api/stream": hangingStream,
    "GET /api/ui/bootstrap": boot,
    "PUT /api/ui/prefs": (init?: RequestInit) =>
      jsonResponse({ ...boot.prefs, ...(JSON.parse(String(init?.body)) as object) }),
    "GET /api/keys": [],
    ...extra,
  };
}

describe("App", () => {
  beforeEach(() => {
    window.localStorage.clear();
    // these tests are about the window desktop; the web navigation has its own (webshell/)
    window.localStorage.setItem("topcoat.shell", "windows");
    window.location.hash = "#/";
    setNarrow(false);
  });
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("asks for the dev token, stores it and shows the desktop", async () => {
    const mock = mockFetch(hub({ id: "u1", name: "Admin", role: "admin" }));
    vi.stubGlobal("fetch", mock.fetch);
    render(<App />);
    fireEvent.change(screen.getByLabelText("Dev-Token"), { target: { value: "dev-token" } });
    fireEvent.click(screen.getByRole("button", { name: "Anmelden" }));
    expect(await screen.findByRole("region", { name: "Settings" })).toBeTruthy();
    expect(window.localStorage.getItem(TOKEN_KEY)).toBe("dev-token");
    expect(await screen.findByRole("button", { name: "Admin" })).toBeTruthy(); // the person's menu
    const stream = mock.calls.find((c) => c.url === "/api/stream");
    expect(stream?.headers.authorization).toBe("Bearer dev-token");
    await waitFor(() => expect(window.location.hash).toBe("#/w/settings"));
  });

  it("keeps the desktop while an address that is not a program moves to it (no flicker)", async () => {
    window.localStorage.setItem(TOKEN_KEY, "dev-token");
    const mock = mockFetch(hub({ id: "u1", name: "Admin", role: "admin" }));
    vi.stubGlobal("fetch", mock.fetch);
    render(<App />);
    const before = await screen.findByRole("region", { name: "Settings" });
    await waitFor(() => expect(window.location.hash).toBe("#/w/settings"));
    const boots = mock.calls.filter((c) => c.url === "/api/ui/bootstrap").length;
    window.location.hash = "#/wiki/p-1";
    window.dispatchEvent(new HashChangeEvent("hashchange"));
    await waitFor(() => expect(window.location.hash).toBe("#/w/settings"));
    // the same program window, nothing loaded again
    expect(screen.getByRole("region", { name: "Settings" })).toBe(before);
    expect(mock.calls.filter((c) => c.url === "/api/ui/bootstrap").length).toBe(boots);
  });

  it("opens what a moved address stands for while the desktop stays (a wiki link)", async () => {
    window.localStorage.setItem(TOKEN_KEY, "dev-token");
    const boot = bootstrap([
      workspace("settings", ["page.access"], { icon: "::", title: "Settings" }),
      workspace("wiki", ["page.wiki"], { icon: "W", title: "Wiki" }),
    ]);
    const mock = mockFetch(
      hub(
        { id: "u1", name: "Admin", role: "admin" },
        {
          "GET /api/ui/bootstrap": boot,
          "GET /api/wiki/pages": { sections: [] },
          "GET /api/wiki/pages/p-2": () => new Promise<Response>(() => undefined),
        },
      ),
    );
    vi.stubGlobal("fetch", mock.fetch);
    render(<App />);
    await screen.findByRole("region", { name: "Settings" });
    await waitFor(() => expect(window.location.hash).toBe("#/w/settings"));
    // a link in a page (the wiki's tree): the desktop opens that page, it does not stay where it was
    window.location.hash = "#/wiki/p-2";
    window.dispatchEvent(new HashChangeEvent("hashchange"));
    await waitFor(() => expect(mock.calls.some((c) => c.url === "/api/wiki/pages/p-2")).toBe(true));
    await waitFor(() => expect(window.location.hash).toBe("#/w/wiki"));
  });

  it("returns to the token form on 401", async () => {
    window.localStorage.setItem(TOKEN_KEY, "stale");
    const mock = mockFetch({
      "GET /api/me": () => jsonResponse({ detail: "bad token" }, 401),
      "GET /api/nodes": () => jsonResponse({ detail: "bad token" }, 401),
      "GET /api/ui/bootstrap": () => jsonResponse({ detail: "bad token" }, 401),
      "GET /api/stream": () => new Response("", { status: 401 }),
    });
    vi.stubGlobal("fetch", mock.fetch);
    render(<App />);
    expect(await screen.findByLabelText("Dev-Token")).toBeTruthy();
    expect(screen.getByRole("alert").textContent).toMatch(/Token ungültig/);
    expect(window.localStorage.getItem(TOKEN_KEY)).toBeNull();
  });

  it("logs out from the person's menu", async () => {
    window.localStorage.setItem(TOKEN_KEY, "tok");
    vi.stubGlobal("fetch", mockFetch(hub({ id: "u1", name: "Admin", role: "admin" })).fetch);
    render(<App />);
    fireEvent.click(await screen.findByRole("button", { name: "Admin" }));
    fireEvent.click(within(screen.getByRole("menu")).getByRole("menuitem", { name: "Abmelden" }));
    await waitFor(() => expect(screen.getByLabelText("Dev-Token")).toBeTruthy());
    expect(window.localStorage.getItem(TOKEN_KEY)).toBeNull();
  });

  it("applies the language stored for the user", async () => {
    window.localStorage.setItem(TOKEN_KEY, "tok");
    vi.stubGlobal(
      "fetch",
      mockFetch(hub({ id: "u1", name: "Admin", role: "admin", language: "en" })).fetch,
    );
    render(<App />);
    expect(await screen.findByRole("button", { name: /Applications/ })).toBeTruthy();
  });

  it("offers an admin to add a node in the palette, and a plain user not", async () => {
    window.localStorage.setItem(TOKEN_KEY, "tok");
    vi.stubGlobal(
      "fetch",
      mockFetch(hub({ id: "u1", name: "Admin", role: "admin", language: "en" })).fetch,
    );
    const admin = render(<App />);
    fireEvent.click(await screen.findByRole("button", { name: /Applications/ }));
    expect(await screen.findByRole("option", { name: /Add node/ })).toBeTruthy();
    admin.unmount();

    vi.stubGlobal(
      "fetch",
      mockFetch(hub({ id: "u2", name: "Gast", role: "user", language: "en" })).fetch,
    );
    render(<App />);
    fireEvent.click(await screen.findByRole("button", { name: /Applications/ }));
    expect((await screen.findAllByRole("option", { name: /Settings/ })).length).toBeGreaterThan(0);
    expect(screen.queryByRole("option", { name: /Add node/ })).toBeNull();
  });

  it("has no look to choose any more", async () => {
    window.localStorage.setItem(TOKEN_KEY, "tok");
    vi.stubGlobal("fetch", mockFetch(hub({ id: "u1", name: "Admin", role: "admin" })).fetch);
    render(<App />);
    await screen.findByRole("region", { name: "Settings" });
    expect(screen.queryByRole("combobox", { name: "Look" })).toBeNull();
    expect(document.documentElement.dataset.look).toBeUndefined();
  });
});
