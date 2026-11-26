import { act, cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { ApiClient } from "../api/client";
import { requestOpen } from "../layout/intent";
import type { ModuleCtx } from "../layout/modules/render";
import type { DesktopPrefs, UiBootstrap } from "../layout/types";
import { bootstrap, setNarrow, workspace } from "../test/layoutFixtures";
import { jsonResponse, mockFetch } from "../test/mockFetch";
import { sseResponse } from "../test/sse";
import { library, persona } from "../instructions/fixtures";
import { setTileNew } from "./dwindle";
import { matches } from "./CommandPalette";
import { Desktop } from "./Desktop";
import { viewFor } from "./state";
import type { ThemeManifest } from "./theme";

/** A hub that keeps the person's preferences like the real one (no clamping needed here). */
function hub(boot: UiBootstrap, counts = { unread: 0, urgent: 0, applications: {} }, extra: Record<string, unknown> = {}) {
  let prefs = boot.prefs;
  const mock = mockFetch({
    "GET /api/inbox/counts": () => jsonResponse(counts),
    "GET /api/ui/bootstrap": () => jsonResponse({ ...boot, prefs }),
    "PUT /api/ui/prefs": (init?: RequestInit) => {
      prefs = { ...prefs, ...(JSON.parse(String(init?.body)) as Partial<typeof prefs>) };
      return jsonResponse(prefs);
    },
    "GET /api/keys": [],
    "GET /api/artifacts": { artifacts: [] },
    ...extra,
  });
  return { mock, api: new ApiClient({ token: "t", fetchImpl: mock.fetch }), prefs: () => prefs };
}

function ctx(api: ApiClient, admin = true): ModuleCtx {
  return {
    api,
    me: { id: "u1", name: "Lu", role: admin ? "admin" : "user" },
    isAdmin: admin,
    nodes: [],
    nodesLoaded: true,
    nodesError: null,
    refreshNodes: () => undefined,
    tick: 0,
    inboxTick: 0,
    dispatch: () => undefined,
  };
}

// Programs whose views render without the hub: the API access page and the archive just say they
// are loading or empty, which is all these tests need.
const PROGRAMS = [
  workspace("settings", ["page.access", "page.library"], { icon: "::", title: "Settings" }),
  workspace("archive", ["page.library"], { icon: "#", title: "Archive" }),
];

function desk(open: string[], extra: Partial<DesktopPrefs> = {}): UiBootstrap {
  const b = bootstrap(PROGRAMS);
  return {
    ...b,
    prefs: {
      hue: 140,
      look: "terminal",
      desktop: { open, minimised: [], focus: open[open.length - 1] ?? null, maximised: null, views: {}, ...extra },
    },
  };
}

function show(boot: UiBootstrap, admin = true, counts?: { unread: number; urgent: number; applications: Record<string, number> }) {
  const h = hub(boot, counts);
  render(
    <Desktop
      api={h.api}
      ctx={ctx(h.api, admin)}
      uiTick={0}
      status="live"
      onSaveLanguage={() => undefined}
      onLogout={() => undefined}
    />,
  );
  return h;
}

describe("Desktop", () => {
  beforeEach(() => {
    setNarrow(false);
    window.location.hash = "#/w";
  });
  afterEach(() => vi.restoreAllMocks());

  it("opens an application's frame and tools from the gear in its title bar, where it has them", async () => {
    const b = bootstrap([
      workspace("research", ["page.library", "page.program"], { icon: "~", title: "Research" }),
      ...PROGRAMS,
    ]);
    const h = show({ ...b, prefs: { hue: 140, look: "terminal", desktop: { open: ["research", "archive"], minimised: [], focus: "archive", maximised: null, views: {} } } });
    const research = await screen.findByRole("region", { name: "Research" });
    expect(within(screen.getByRole("region", { name: "Archive" })).queryByRole("button", { name: /Einstellungen von/ })).toBeNull();
    fireEvent.click(within(research).getByRole("button", { name: "Einstellungen von Research" }));
    await waitFor(() => expect(h.prefs().desktop.views.research?.module).toBe("page.program"));
  });

  it("attaches Clipper to the window in front with AltGr+C and starts the talk with what it shows", async () => {
    const boot = desk(["settings", "archive"]);
    const h = hub(boot, undefined, {
      "POST /api/conversations": () => jsonResponse({ id: "c1", title: "x", application: "chat" }),
      "POST /api/conversations/c1/messages": () =>
        sseResponse([
          ["delta", { text: "Hier " }],
          ["done", { message: { id: 9, role: "assistant", content: "Hier steht das Archiv.", status: "complete" } }],
        ]),
    });
    render(
      <Desktop api={h.api} ctx={ctx(h.api)} uiTick={0} status="live" onSaveLanguage={() => undefined} onLogout={() => undefined} />,
    );
    await screen.findByRole("region", { name: "Archive" });
    fireEvent.keyDown(window, { key: "c", code: "KeyC", ctrlKey: true, altKey: true });
    const dock = await screen.findByTestId("clipper-dock");
    expect(dock.getAttribute("aria-label")).toBe("Clipper an Archive");
    const box = within(dock).getByLabelText("Frag Clipper …");
    fireEvent.change(box, { target: { value: "Was liegt hier?" } });
    // the perch's one-line field sends with Enter, as a form does
    fireEvent.submit(box.closest("form")!);
    expect(await within(dock).findByText("Hier steht das Archiv.", {}, { timeout: 3000 })).toBeTruthy();
    const created = h.mock.calls.find((c) => c.method === "POST" && c.url === "/api/conversations");
    expect(created?.body).toMatchObject({ application: "chat", context: { module: "page.library" } });
    // the same key takes him away again
    fireEvent.keyDown(window, { key: "c", code: "KeyC", ctrlKey: true, altKey: true });
    await waitFor(() => expect(screen.queryByTestId("clipper-dock")).toBeNull());
  });

  it("hears AltGr+C as soon as the window in front is on screen, not only a moment later (#73)", async () => {
    const h = hub(desk(["settings", "archive"]));
    // press the key the instant the windows are drawn: a MutationObserver runs right after React
    // has put them on the page, before anything React schedules for later
    let pressed = false;
    const seen = new MutationObserver(() => {
      if (pressed || !screen.queryByRole("region", { name: "Archive" })) return;
      pressed = true;
      fireEvent.keyDown(window, { key: "c", code: "KeyC", ctrlKey: true, altKey: true });
    });
    seen.observe(document.body, { childList: true, subtree: true });
    render(
      <Desktop api={h.api} ctx={ctx(h.api)} uiTick={0} status="live" onSaveLanguage={() => undefined} onLogout={() => undefined} />,
    );
    try {
      await waitFor(() => expect(pressed).toBe(true));
      expect((await screen.findByTestId("clipper-dock")).getAttribute("aria-label")).toBe("Clipper an Archive");
    } finally {
      seen.disconnect();
    }
  });

  it("shows the open programs side by side and every program on the taskbar", async () => {
    show(desk(["settings", "archive"]));
    const settings = await screen.findByRole("region", { name: "Settings" });
    const archive = screen.getByRole("region", { name: "Archive" });
    expect(settings.style.width).toBe("50%");
    expect(archive.style.left).toBe("50%");
    const bar = screen.getByRole("navigation", { name: "Anwendungen" });
    expect(within(bar).getAllByRole("button").map((b) => b.textContent?.trim())).toEqual([
      ":: Settings",
      "# Archive",
    ]);
  });

  it("minimises, brings back and closes, and stores it for the person", async () => {
    const h = show(desk(["settings", "archive"]));
    const archive = await screen.findByRole("region", { name: "Archive" });
    fireEvent.click(within(archive).getByRole("button", { name: "Archive minimieren" }));
    await waitFor(() => expect(screen.queryByRole("region", { name: "Archive" })).toBeNull());
    expect(h.prefs().desktop.minimised).toEqual(["archive"]);
    // one program left: it takes the whole desktop
    expect(screen.getByRole("region", { name: "Settings" }).style.width).toBe("100%");
    const bar = screen.getByRole("navigation", { name: "Anwendungen" });
    fireEvent.click(within(bar).getByRole("button", { name: "Archive" }));
    expect(await screen.findByRole("region", { name: "Archive" })).toBeTruthy();
    fireEvent.click(within(screen.getByRole("region", { name: "Archive" })).getByRole("button", { name: "Archive schließen" }));
    await waitFor(() => expect(h.prefs().desktop.open).toEqual(["settings"]));
  });

  it("switches the view of a program from its navigation", async () => {
    const h = show(desk(["settings"]));
    const settings = await screen.findByRole("region", { name: "Settings" });
    const nav = within(settings).getByRole("navigation");
    fireEvent.click(within(nav).getByRole("button", { name: "Ablage" }));
    await waitFor(() => expect(h.prefs().desktop.views.settings?.module).toBe("page.library"));
    expect(within(nav).getByRole("button", { name: "Ablage" }).getAttribute("aria-current")).toBe("page");
  });

  it("maximises one program: it only grows, the others stay beneath it", async () => {
    show(desk(["settings", "archive"]));
    const archive = await screen.findByRole("region", { name: "Archive" });
    fireEvent.click(within(archive).getByRole("button", { name: "Archive maximieren" }));
    await waitFor(() => expect(screen.getByRole("region", { name: "Archive" }).style.width).toBe("100%"));
    const settings = screen.getByRole("region", { name: "Settings" });
    expect(Number(archive.style.zIndex)).toBeGreaterThan(Number(settings.style.zIndex));
    // restored, it goes back to its tile
    fireEvent.click(within(archive).getByRole("button", { name: /Archive wiederherstellen/ }));
    await waitFor(() => expect(screen.getByRole("region", { name: "Archive" }).style.width).toBe("50%"));
  });

  it("opens the palette with Ctrl+K and opens a view by its name", async () => {
    const h = show(desk([]));
    expect(await screen.findByText(/Keine Anwendung ist offen/)).toBeTruthy();
    fireEvent.keyDown(window, { key: "k", ctrlKey: true });
    const box = await screen.findByRole("combobox", { name: "Anwendung, Ansicht, Einstellung …" });
    fireEvent.change(box, { target: { value: "ablage sett" } });
    const options = screen.getAllByRole("option");
    expect(options.map((o) => o.textContent)).toEqual(["Ablage Settings"]);
    fireEvent.keyDown(box, { key: "Enter" });
    await waitFor(() => expect(h.prefs().desktop.views.settings?.module).toBe("page.library"));
    expect(screen.queryByRole("dialog")).toBeNull();
    expect(await screen.findByRole("region", { name: "Settings" })).toBeTruthy();
  });

  it("Alt+number brings a program forward, Escape minimises the one in front", async () => {
    const h = show(desk(["settings"]));
    await screen.findByRole("region", { name: "Settings" });
    fireEvent.keyDown(window, { key: "2", altKey: true });
    expect(await screen.findByRole("region", { name: "Archive" })).toBeTruthy();
    expect(h.prefs().desktop.focus).toBe("archive");
    fireEvent.keyDown(window, { key: "Escape" });
    await waitFor(() => expect(h.prefs().desktop.minimised).toEqual(["archive"]));
  });

  it("opens the program that holds a view another window asks for", async () => {
    const h = show(desk(["archive"]));
    await screen.findByRole("region", { name: "Archive" });
    act(() => requestOpen({ module: "page.access", props: {} }));
    expect(await screen.findByRole("region", { name: "Settings" })).toBeTruthy();
    expect(h.prefs().desktop.views.settings?.module).toBe("page.access");
  });

  it("opens a page that is a tab of another view on that tab", () => {
    expect(viewFor("page.inventory", {})).toEqual({ module: "page.overview", props: { tab: "inventory" } });
    expect(viewFor("page.chat", { id: "c1" })).toEqual({ module: "page.chat", props: { id: "c1" } });
  });

  it("stores one change once, and the address does not push it back (no loop)", async () => {
    window.location.hash = "#/w/settings";
    const h = show(desk(["settings"]));
    await screen.findByRole("region", { name: "Settings" });
    const bar = screen.getByRole("navigation", { name: "Anwendungen" });
    fireEvent.click(within(bar).getByRole("button", { name: "Archive" }));
    await screen.findByRole("region", { name: "Archive" });
    await new Promise((r) => setTimeout(r, 200));
    const puts = h.mock.calls.filter((c) => c.method === "PUT" && c.url === "/api/ui/prefs");
    expect(puts).toHaveLength(1);
    expect(window.location.hash).toBe("#/w/archive");
  });

  it("switches to the control room: the look is stored, the clock runs in universal time", async () => {
    const h = show(desk(["settings"]));
    await screen.findByRole("region", { name: "Settings" });
    // the look is chosen in Settings › General or the palette (phase 4), no longer the name menu
    fireEvent.keyDown(window, { key: "k", ctrlKey: true });
    fireEvent.change(await screen.findByRole("combobox"), { target: { value: "Leitstand" } });
    fireEvent.keyDown(await screen.findByRole("combobox"), { key: "Enter" });
    await waitFor(() => expect(h.prefs().look).toBe("leitstand"));
    expect(document.querySelector(".dt-root")?.classList.contains("dt-look-leitstand")).toBe(true);
    expect((await screen.findByTestId("mission-clock")).textContent).toMatch(/^\d\d:\d\d:\d\d$/);
    // each program carries its code, as a display names its page
    expect(screen.getByRole("region", { name: "Settings" }).querySelector(".dt-code")?.textContent).toBe("SET");
  });

  it("offers all three looks and lays the programs on the desk as slips of paper", async () => {
    const h = show(desk(["settings"]));
    await screen.findByRole("region", { name: "Settings" });
    fireEvent.keyDown(window, { key: "k", ctrlKey: true });
    fireEvent.change(await screen.findByRole("combobox"), { target: { value: "Optik" } });
    // the themes (hint "Optik"); the option of Settings by that name is found too
    const looks = (await screen.findAllByRole("option")).filter((o) => /\sOptik$/.test(o.textContent ?? ""));
    expect(looks.map((o) => o.textContent?.replace(/\s*Optik$/, ""))).toEqual(["Terminal", "Leitstand", "Schreibtisch"]);
    fireEvent.click(looks[2]!);
    await waitFor(() => expect(h.prefs().look).toBe("schreibtisch"));
    expect(document.querySelector(".dt-root")?.classList.contains("dt-look-schreibtisch")).toBe(true);
    const slip = screen.getByRole("region", { name: "Settings" });
    expect(slip.getAttribute("data-paper")).toBe("green");
    expect(slip.style.getPropertyValue("--rot")).toMatch(/^-?\d+(\.\d+)?deg$/);
    expect(screen.queryByTestId("mission-clock")).toBeNull(); // the clock is the control room's
  });

  describe("a custom theme", () => {
    const WALL: ThemeManifest = {
      key: "wall",
      name: "Wall",
      base: "terminal",
      layout: { kind: "columns" },
      bar: { position: "top", items: ["programs", "start"] },
      chrome: { buttons: ["close", "minimise"], titlebar: "top" },
      motion: { style: "slide", ms: 200 },
      css: "--accent: hotpink;\n.dt-task { letter-spacing: 1px; }",
    };

    function withWall(look = "wall", over: Partial<ThemeManifest> = {}) {
      const b = desk(["settings", "archive"]);
      return { ...b, themes: [{ ...WALL, ...over }], prefs: { ...b.prefs, look } };
    }

    it("lays the programs out, moves the bar, orders the buttons and puts its style sheet on the page", async () => {
      show(withWall());
      await screen.findByRole("region", { name: "Settings" });
      const root = document.querySelector(".dt-root") as HTMLElement;
      expect(root.classList.contains("dt-theme-wall")).toBe(true);
      expect(root.classList.contains("dt-bar-top")).toBe(true);
      expect(root.getAttribute("data-motion")).toBe("slide");
      expect(root.style.getPropertyValue("--dt-ms")).toBe("200ms");
      // columns: the two programs side by side, whatever tile() would do
      expect(screen.getByRole("region", { name: "Archive" }).style.left).toBe("50%");
      // the taskbar holds what the theme lists, in its order: programs first, then the start button;
      // the name menu the theme left out comes last (language, look and sign-out live there)
      const bar = document.querySelector(".dt-taskbar") as HTMLElement;
      expect([...bar.children].map((c) => c.className.split(" ")[0])).toEqual(["dt-tasks", "dt-start", "dt-me"]);
      expect(bar.querySelector(".conn")).toBeNull();
      // the title bar has the two buttons the theme asks for, in its order
      const buttons = within(screen.getByRole("region", { name: "Settings" }))
        .getAllByRole("button")
        .filter((b) => b.closest(".dt-titlebar-buttons"));
      expect(buttons.map((b) => b.getAttribute("aria-label"))).toEqual([
        "Settings schließen",
        "Settings minimieren",
      ]);
      expect(screen.getByTestId("theme-css").textContent).toContain(".dt-theme-wall:is(.dt-root, #dt-theme) {");
    });

    it("lets a closing window play the theme's leaving before it goes", async () => {
      const h = show(withWall("wall", { motion: { style: "fade", ms: 120, close: "zoom", overlay: "fade" } }));
      const archive = await screen.findByRole("region", { name: "Archive" });
      const root = document.querySelector(".dt-root") as HTMLElement;
      expect(root.getAttribute("data-close")).toBe("zoom");
      expect(root.getAttribute("data-overlay-motion")).toBe("fade");
      fireEvent.click(within(archive).getByRole("button", { name: "Archive schließen" }));
      // still there, on its way out; the person's state has not changed yet
      expect(screen.getByRole("region", { name: "Archive" }).classList.contains("dt-closing")).toBe(true);
      expect(h.prefs().desktop.open).toContain("archive");
      await waitFor(() => expect(h.prefs().desktop.open).toEqual(["settings"]));
      await waitFor(() => expect(screen.queryByRole("region", { name: "Archive" })).toBeNull());
    });

    it("can hide the title bar and the taskbar; the palette still switches themes", async () => {
      const h = show(
        withWall("wall", {
          bar: { position: "hidden", items: ["programs"] },
          chrome: { buttons: [], titlebar: "hidden" },
        }),
      );
      await screen.findByRole("region", { name: "Settings" });
      expect(document.querySelector(".dt-root")?.classList.contains("dt-bar-hidden")).toBe(true);
      expect(document.querySelector(".dt-titlebar")).toBeNull();
      fireEvent.keyDown(window, { key: "k", ctrlKey: true });
      fireEvent.change(await screen.findByRole("combobox"), { target: { value: "Schreibtisch" } });
      fireEvent.keyDown(await screen.findByRole("combobox"), { key: "Enter" });
      await waitFor(() => expect(h.prefs().look).toBe("schreibtisch"));
    });

    it("draws with the terminal when the chosen theme is gone, and refuses an unsafe style sheet", async () => {
      show(withWall("missing"));
      await screen.findByRole("region", { name: "Settings" });
      expect(document.querySelector(".dt-root")?.classList.contains("dt-theme-terminal")).toBe(true);
      cleanup();
      show(withWall("wall", { css: "@import url(https://evil.example/x.css);" }));
      await screen.findByRole("region", { name: "Settings" });
      expect(screen.queryByTestId("theme-css")).toBeNull();
    });
  });

  it("lights the inbox amber for unread items and red for urgent ones", async () => {
    const programs = bootstrap([
      workspace("inbox", ["page.board"], { icon: "@", title: "Inbox" }),
      ...PROGRAMS,
    ]);
    show({ ...programs, prefs: { hue: 140, look: "terminal", desktop: { open: ["settings"], minimised: [], focus: "settings", maximised: null, views: {} } } }, true, {
      unread: 2,
      urgent: 1,
      applications: {},
    });
    const bar = await screen.findByRole("navigation", { name: "Anwendungen" });
    await waitFor(() =>
      expect(within(bar).getByRole("button", { name: "Inbox" }).className).toContain("dt-signal-red"),
    );
  });

  it("shows one program at a time on a narrow screen", async () => {
    setNarrow(true);
    show(desk(["settings", "archive"]));
    expect(await screen.findByRole("region", { name: "Archive" })).toBeTruthy();
    expect(screen.queryByRole("region", { name: "Settings" })).toBeNull();
  });
});

describe("the palette's search", () => {
  it("finds every word in the label or the hint, in any order and case", () => {
    const entry = { id: "x", label: "Modelle", hint: "Verwaltung", run: () => undefined };
    expect(matches(entry, "verw mod")).toBe(true);
    expect(matches(entry, "")).toBe(true);
    expect(matches(entry, "chat")).toBe(false);
  });
});

describe("the overlay of the desktop client (?overlay=1)", () => {
  afterEach(() => {
    window.history.replaceState(null, "", "/");
    vi.restoreAllMocks();
  });

  it("is the whole desktop on a see-through page, with the taskbar and the way out; no Clipper standing in the corner", async () => {
    setNarrow(false);
    window.history.replaceState(null, "", "/?overlay=1#/w");
    const clipper = persona({ id: "p-clip", name: "Clipper", key: "clipper" });
    const h = hub(desk(["settings", "archive"]), undefined, {
      "GET /api/personas": library({ templates: [clipper], default_id: "p-clip" }),
    });
    const { container } = render(
      <Desktop api={h.api} ctx={ctx(h.api)} uiTick={0} status="live" onSaveLanguage={() => undefined} onLogout={() => undefined} />,
    );
    await screen.findByRole("region", { name: "Settings" });
    expect(container.querySelector(".dt-root")?.classList.contains("dt-overlay")).toBe(true);
    expect(document.documentElement.classList.contains("tc-overlay")).toBe(true);
    expect(screen.getByRole("navigation", { name: "Anwendungen" })).toBeTruthy();
    // no hint over the desktop: the keys are in the client's settings (2026-11-20)
    expect(screen.queryByTestId("overlay-hint")).toBeNull();
    // Clipper comes to a window when called (AltGr+C), he does not stand in the corner (2026-11-13)
    await new Promise((r) => setTimeout(r, 50));
    expect(screen.queryByRole("figure")).toBeNull();
    cleanup();
    // the page leaves no trace once the desktop is gone
    expect(document.documentElement.classList.contains("tc-overlay")).toBe(false);
  });

  it("is not there in the browser: no tint, no hint", async () => {
    setNarrow(false);
    window.location.hash = "#/w";
    show(desk(["settings"]));
    await screen.findByRole("region", { name: "Settings" });
    expect(document.querySelector(".dt-overlay")).toBeNull();
    expect(screen.queryByTestId("overlay-hint")).toBeNull();
    expect(screen.queryByRole("button", { name: /an den Desktop heften/ })).toBeNull();
  });

  it("lets the windows float: dragged by the title bar, sized at an edge, kept for the next time", async () => {
    setNarrow(false);
    window.localStorage.clear();
    window.history.replaceState(null, "", "/?overlay=1#/w");
    vi.spyOn(Element.prototype, "getBoundingClientRect").mockReturnValue(
      { x: 0, y: 0, left: 0, top: 0, width: 1000, height: 500, right: 1000, bottom: 500, toJSON: () => ({}) } as DOMRect,
    );
    show(desk(["settings", "archive"]));
    const settings = await screen.findByRole("region", { name: "Settings" });
    // both float, none is tiled away
    expect(screen.getByRole("region", { name: "Archive" })).toBeTruthy();
    const left = parseFloat(settings.style.left);
    const title = settings.querySelector(".dt-titlebar") as HTMLElement;
    fireEvent.pointerDown(title, { button: 0, clientX: 100, clientY: 100 });
    fireEvent.pointerMove(window, { clientX: 200, clientY: 150 });
    fireEvent.pointerUp(window);
    await waitFor(() => expect(parseFloat(settings.style.left)).toBeCloseTo(left + 10, 1));
    const width = parseFloat(settings.style.width);
    fireEvent.pointerDown(settings.querySelector(".dt-grip-e") as HTMLElement, { button: 0, clientX: 0, clientY: 0 });
    fireEvent.pointerMove(window, { clientX: -100, clientY: 0 });
    fireEvent.pointerUp(window);
    await waitFor(() => expect(parseFloat(settings.style.width)).toBeCloseTo(width - 10, 1));
    const kept = JSON.parse(window.localStorage.getItem("topcoat.overlay.boxes") ?? "{}") as Record<string, { x: number }>;
    expect(kept.settings?.x).toBeCloseTo((left + 10) / 100, 3);
  });

  it("moves Clipper with his window while it is dragged, not only when it is let go", async () => {
    setNarrow(false);
    window.localStorage.clear();
    window.history.replaceState(null, "", "/?overlay=1#/w");
    vi.spyOn(Element.prototype, "getBoundingClientRect").mockReturnValue(
      { x: 0, y: 0, left: 0, top: 0, width: 1000, height: 500, right: 1000, bottom: 500, toJSON: () => ({}) } as DOMRect,
    );
    show(desk(["settings"]));
    const settings = await screen.findByRole("region", { name: "Settings" });
    fireEvent.keyDown(window, { key: "c", code: "KeyC", ctrlKey: true, altKey: true });
    const dock = await screen.findByTestId("clipper-dock");
    const title = settings.querySelector(".dt-titlebar") as HTMLElement;
    fireEvent.pointerDown(title, { button: 0, clientX: 300, clientY: 100 });
    fireEvent.pointerMove(window, { clientX: 400, clientY: 150, altKey: true });
    // still held: the window and Clipper have moved by the same amount
    await waitFor(() => expect(settings.style.getPropertyValue("translate")).toMatch(/^(99\.9+\d*|100)px 50px/));
    expect(dock.style.getPropertyValue("translate")).toBe(settings.style.getPropertyValue("translate"));
    // the move is not `transform`, which a theme's animation may hold (live 2026-11-16)
    expect(settings.style.transform).toBe("");
    fireEvent.pointerUp(window, { clientX: 400, clientY: 150 });
    await waitFor(() => expect(dock.style.getPropertyValue("translate")).toBe(""));
    // the perch lies inside the window (2026-11-20)
    expect(parseFloat(dock.style.left)).toBeCloseTo(parseFloat(settings.style.left));
    expect(parseFloat(dock.style.width)).toBeCloseTo(parseFloat(settings.style.width));
  });

  it("snaps a window dragged to a screen's edge to that half of the screen", async () => {
    setNarrow(false);
    window.localStorage.clear();
    window.history.replaceState(null, "", "/?overlay=1#/w");
    vi.spyOn(Element.prototype, "getBoundingClientRect").mockReturnValue(
      { x: 0, y: 0, left: 0, top: 0, width: 1000, height: 500, right: 1000, bottom: 500, toJSON: () => ({}) } as DOMRect,
    );
    show(desk(["settings"]));
    const settings = await screen.findByRole("region", { name: "Settings" });
    const title = settings.querySelector(".dt-titlebar") as HTMLElement;
    fireEvent.pointerDown(title, { button: 0, clientX: 300, clientY: 100 });
    fireEvent.pointerMove(window, { clientX: 2, clientY: 250 });
    // the zone shows before the window is let go
    await waitFor(() => expect(document.querySelector(".dt-snap-preview")).not.toBeNull());
    fireEvent.pointerUp(window, { clientX: 2, clientY: 250 });
    await waitFor(() => expect(settings.style.width).toBe("50%"));
    expect(settings.style.left).toBe("0%");
    expect(settings.style.height).toBe("100%");
    expect(document.querySelector(".dt-snap-preview")).toBeNull();
  });

  it("tiles every window by the switch in the settings, and lets them float where their tiles were", async () => {
    setNarrow(false);
    window.localStorage.clear();
    window.history.replaceState(null, "", "/?overlay=1#/w");
    show(desk(["settings", "archive"]));
    const settings = await screen.findByRole("region", { name: "Settings" });
    const archive = screen.getByRole("region", { name: "Archive" });
    // no switch per window any more
    expect(within(settings).queryByRole("button", { name: /kacheln/ })).toBeNull();
    expect(settings.hasAttribute("data-tile-screen")).toBe(false);
    act(() => setTileNew(true));
    await waitFor(() => expect(settings.getAttribute("data-tile-screen")).toBe("0"));
    expect(archive.getAttribute("data-tile-screen")).toBe("0");
    expect(parseFloat(settings.style.width)).toBeLessThan(51);
    // only the divider can be dragged on a tiled window
    expect(settings.querySelectorAll(".dt-grip")).toHaveLength(1);
    act(() => setTileNew(false));
    await waitFor(() => expect(settings.hasAttribute("data-tile-screen")).toBe(false));
    expect(archive.hasAttribute("data-tile-screen")).toBe(false);
    // each keeps the place of its tile
    expect(parseFloat(settings.style.width)).toBeLessThan(51);
    expect(JSON.parse(window.localStorage.getItem("topcoat.overlay.tiles") ?? "{}")).toEqual({});
    window.localStorage.clear();
  });

  it("spans all screens: the taskbar on the primary one, a maximised program on its own screen", async () => {
    setNarrow(false);
    window.localStorage.setItem(
      "topcoat.overlay.boxes",
      JSON.stringify({ archive: { x: 0.7, y: 0.4, w: 0.2, h: 0.3 } }),
    );
    window.history.replaceState(null, "", "/?overlay=1&screens=0,0,0.6,1;0.6,0.3,0.4,0.7#/w");
    show(desk(["settings", "archive"], { maximised: "archive", focus: "archive" }));
    const archive = await screen.findByRole("region", { name: "Archive" });
    // maximised on the right screen, not over both
    expect(archive.style.left).toBe("60%");
    expect(archive.style.top).toBe("30%");
    expect(archive.style.width).toBe("40%");
    const bar = screen.getByRole("navigation", { name: "Anwendungen" });
    const area = bar.closest(".dt-screen") as HTMLElement;
    expect(area.style.width).toBe("60%");
    expect(area.style.left).toBe("0%");
    expect(document.documentElement.style.getPropertyValue("--scr-w")).toBe("60vw");
    window.localStorage.clear();
  });

  it("keeps a desktop of its own on a screen beside the primary one and sends a window on to the next screen", async () => {
    // one overlay window per screen (2026-11-20): the primary screen shows the person's desktop from
    // the hub, another screen its own, kept in its web view
    setNarrow(false);
    window.localStorage.clear();
    const own = { open: ["archive"], minimised: [], focus: "archive", maximised: null, views: {} };
    window.localStorage.setItem("topcoat.overlay.desk.display2", JSON.stringify(own));
    window.history.replaceState(null, "", "/?overlay=1&screen=display2&own=1#/w");
    const client = window as unknown as { topcoatClient?: object };
    client.topcoatClient = { config: { version: "0.2.0", key: "ctrl+alt+Space", effect: "blur", screens: 2 } };
    vi.spyOn(console, "error").mockImplementation(() => undefined); // jsdom cannot navigate
    try {
      const h = show(desk(["settings"]));
      const archive = await screen.findByRole("region", { name: "Archive" });
      expect(screen.queryByRole("region", { name: "Settings" })).toBeNull();
      fireEvent.click(within(archive).getByRole("button", { name: "Archive auf den nächsten Bildschirm" }));
      await waitFor(() => expect(screen.queryByRole("region", { name: "Archive" })).toBeNull());
      const kept = JSON.parse(window.localStorage.getItem("topcoat.overlay.desk.display2") ?? "{}") as { open: string[] };
      expect(kept.open).toEqual([]);
      // the person's desktop on the hub is not this screen's
      expect(h.prefs().desktop.open).toEqual(["settings"]);
    } finally {
      delete client.topcoatClient;
    }
  });

  it("offers no way to another screen when there is one screen only", async () => {
    setNarrow(false);
    window.history.replaceState(null, "", "/?overlay=1&screen=display1#/w");
    show(desk(["settings"]));
    const settings = await screen.findByRole("region", { name: "Settings" });
    expect(within(settings).queryByRole("button", { name: /nächsten Bildschirm/ })).toBeNull();
  });

  it("pins a window: it leaves the overlay for the taskbar, the client opens it natively", async () => {
    setNarrow(false);
    window.history.replaceState(null, "", "/?overlay=1#/w");
    vi.spyOn(console, "error").mockImplementation(() => undefined); // jsdom cannot navigate
    show(desk(["settings", "archive"]));
    await screen.findByRole("region", { name: "Settings" });
    fireEvent.click(screen.getByRole("button", { name: "Settings an den Desktop heften" }));
    await waitFor(() => expect(screen.queryByRole("region", { name: "Settings" })).toBeNull());
    expect(screen.getByRole("region", { name: "Archive" })).toBeTruthy();
  });

  it("keeps Start's open conversations out of its row of views, so the row stays one line high (#77)", async () => {
    const b = bootstrap([workspace("chat", ["page.library", "page.chat"], { icon: "tc", title: "Start" })]);
    const h = hub(
      { ...b, prefs: { hue: 140, look: "terminal", desktop: { open: ["chat"], minimised: [], focus: "chat", maximised: null, views: {} } } },
      undefined,
      {
        "GET /api/conversations": [
          { id: "c1", title: "Why does the container restart at night?", application: "chat", open: true, created_at: new Date().toISOString(), updated_at: new Date().toISOString() },
        ],
      },
    );
    render(<Desktop api={h.api} ctx={ctx(h.api)} uiTick={0} status="live" onSaveLanguage={() => undefined} onLogout={() => undefined} />);
    const start = await screen.findByRole("region", { name: "Start" });
    await within(start).findByRole("button", { name: "Why does the container restart at night?" });
    const open = within(start).getByRole("group", { name: "Offen" });
    const views = within(start).getByRole("navigation");
    // the views are buttons of their own row; the list is a row (or the rail's lower part) of its own
    expect(views.contains(open)).toBe(false);
    expect(open.parentElement).toBe(views.parentElement);
    expect(within(views).getAllByRole("button").map((x) => x.textContent)).toHaveLength(2);
  });
});
