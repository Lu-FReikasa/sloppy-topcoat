// The nest layout on the desktop (#14): windows placed by the nest's tree, zoom keys and palette
// entries, Esc steps out, nothing is mounted again when the view zooms, the overlay uses it too.
import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { ApiClient } from "../api/client";
import type { ModuleCtx } from "../layout/modules/render";
import type { UiBootstrap } from "../layout/types";
import { bootstrap, setNarrow, workspace } from "../test/layoutFixtures";
import { jsonResponse, mockFetch } from "../test/mockFetch";
import { Desktop } from "./Desktop";
import type { ThemeManifest } from "./theme";

const NEST: ThemeManifest = {
  key: "nesty",
  name: "Nesty",
  base: "terminal",
  layout: { kind: "nest" },
  bar: { position: "bottom", items: ["start", "programs", "me"] },
  chrome: { buttons: ["minimise", "maximise", "close"], titlebar: "top" },
  motion: { style: "zoom", ms: 120 },
};

const PROGRAMS = [
  workspace("settings", ["page.access", "page.library"], { icon: "::", title: "Settings" }),
  workspace("archive", ["page.library"], { icon: "#", title: "Archive" }),
];

function desk(open: string[], focus = open[open.length - 1] ?? null): UiBootstrap {
  const b = bootstrap(PROGRAMS);
  return {
    ...b,
    themes: [NEST],
    prefs: { hue: 140, look: "nesty", desktop: { open, minimised: [], focus, maximised: null, views: {} } },
  };
}

function show(boot: UiBootstrap) {
  let prefs = boot.prefs;
  const mock = mockFetch({
    "GET /api/inbox/counts": () => jsonResponse({ unread: 0, urgent: 0, applications: {} }),
    "GET /api/ui/bootstrap": () => jsonResponse({ ...boot, prefs }),
    "PUT /api/ui/prefs": (init?: RequestInit) => {
      prefs = { ...prefs, ...(JSON.parse(String(init?.body)) as Partial<typeof prefs>) };
      return jsonResponse(prefs);
    },
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
    dispatch: () => undefined,
  };
  render(<Desktop api={api} ctx={ctx} uiTick={0} status="live" onSaveLanguage={() => undefined} onLogout={() => undefined} />);
  return { prefs: () => prefs };
}

const place = (el: HTMLElement) => [el.style.left, el.style.top, el.style.width, el.style.height];
const stored = () => JSON.parse(window.localStorage.getItem("topcoat.nest") ?? "{}") as Record<string, { zoom: number[] }>;

describe("the nest layout", () => {
  beforeEach(() => {
    setNarrow(false);
    window.localStorage.clear();
    window.location.hash = "#/w";
  });
  afterEach(() => {
    window.history.replaceState(null, "", "/");
    vi.restoreAllMocks();
    vi.unstubAllGlobals();
  });

  it("places the windows by its tree and zooms with the keys, without mounting anything again", async () => {
    const h = show(desk(["settings", "archive"]));
    const settings = await screen.findByRole("region", { name: "Settings" });
    const archive = screen.getByRole("region", { name: "Archive" });
    // a wide stage: side by side, in the order they were opened
    await waitFor(() => expect(place(archive)).toEqual(["50%", "0%", "50%", "100%"]));
    expect(place(settings)).toEqual(["0%", "0%", "50%", "100%"]);
    const settingsView = settings.querySelector(".dt-view")!.firstElementChild;

    // Alt+Down: in towards the program in front (archive); it fills the stage
    fireEvent.keyDown(window, { key: "ArrowDown", altKey: true });
    await waitFor(() => expect(place(archive)).toEqual(["0%", "0%", "100%", "100%"]));
    // settings lies outside the frame: still there, the same element, but out of reach
    expect(screen.getByRole("region", { name: "Settings", hidden: true })).toBe(settings);
    expect(settings.style.left).toBe("-100%");
    expect(settings.classList.contains("dt-unframed")).toBe(true);
    expect(settings.hasAttribute("inert")).toBe(true);
    expect(settings.querySelector(".dt-view")!.firstElementChild).toBe(settingsView);
    expect(stored()[0]!.zoom).toEqual([1]);

    // Esc steps out one level; it does not minimise while zoomed in
    fireEvent.keyDown(window, { key: "Escape" });
    await waitFor(() => expect(place(archive)).toEqual(["50%", "0%", "50%", "100%"]));
    expect(settings.classList.contains("dt-unframed")).toBe(false);
    expect(settings.hasAttribute("inert")).toBe(false);
    expect(h.prefs().desktop.minimised).toEqual([]);
    expect(screen.getByRole("region", { name: "Settings" })).toBe(settings);
    expect(screen.getByRole("region", { name: "Archive" })).toBe(archive);

    // Alt+0: the overview from any level
    fireEvent.keyDown(window, { key: "ArrowDown", altKey: true });
    await waitFor(() => expect(archive.style.width).toBe("100%"));
    fireEvent.keyDown(window, { key: "0", altKey: true });
    await waitFor(() => expect(archive.style.width).toBe("50%"));

    // in the overview Esc minimises as before
    fireEvent.keyDown(window, { key: "Escape" });
    await waitFor(() => expect(h.prefs().desktop.minimised).toEqual(["archive"]));
  });

  it("leaves keys to a field the person types in", async () => {
    show(desk(["settings", "archive"]));
    const archive = await screen.findByRole("region", { name: "Archive" });
    await waitFor(() => expect(archive.style.width).toBe("50%"));
    const field = document.createElement("input");
    document.body.appendChild(field);
    fireEvent.keyDown(field, { key: "ArrowDown", altKey: true });
    await new Promise((r) => setTimeout(r, 20));
    expect(archive.style.width).toBe("50%");
    field.remove();
  });

  it("brings a program from outside the frame into view when it is brought forward", async () => {
    const h = show(desk(["settings", "archive"]));
    const archive = await screen.findByRole("region", { name: "Archive" });
    fireEvent.keyDown(window, { key: "ArrowDown", altKey: true });
    await waitFor(() => expect(archive.style.width).toBe("100%"));
    // Alt+1: settings, outside the frame, comes forward; the view zooms out to show it
    fireEvent.keyDown(window, { key: "1", altKey: true });
    await waitFor(() => expect(h.prefs().desktop.focus).toBe("settings"));
    const settings = screen.getByRole("region", { name: "Settings" });
    await waitFor(() => expect(settings.classList.contains("dt-unframed")).toBe(false));
    expect(archive.style.width).toBe("50%");
  });

  it("offers the zoom in the palette", async () => {
    show(desk(["settings", "archive"]));
    const archive = await screen.findByRole("region", { name: "Archive" });
    await waitFor(() => expect(archive.style.width).toBe("50%"));
    fireEvent.keyDown(window, { key: "k", ctrlKey: true });
    const dialog = await screen.findByRole("dialog");
    const search = within(dialog).getByRole("combobox");
    fireEvent.change(search, { target: { value: "zoom" } });
    const option = await within(dialog).findByText("Hineinzoomen (zum Programm vorn)");
    expect(within(dialog).getByText("Eine Ebene herauszoomen")).toBeTruthy();
    expect(within(dialog).getByText("Übersicht über alle Programme")).toBeTruthy();
    fireEvent.click(option);
    await waitFor(() => expect(archive.style.width).toBe("100%"));
  });

  it("draws a program below its minimum size scaled down, and zooms to it when it is clicked", async () => {
    // a small stage: two programs side by side are 300 px wide, less than the minimum (480)
    let report: ((e: { contentRect: { width: number; height: number } }[]) => void) | null = null;
    vi.stubGlobal(
      "ResizeObserver",
      class {
        constructor(fn: typeof report) {
          report = fn;
        }
        observe() {
          report?.([{ contentRect: { width: 600, height: 400 } }]);
        }
        disconnect() {}
      },
    );
    vi.spyOn(HTMLElement.prototype, "clientWidth", "get").mockReturnValue(600);
    vi.spyOn(HTMLElement.prototype, "clientHeight", "get").mockReturnValue(400);
    show(desk(["settings", "archive"], "archive"));
    const settings = await screen.findByRole("region", { name: "Settings" });
    await waitFor(() => expect(settings.classList.contains("dt-scaled")).toBe(true));
    // laid out at the minimum width and scaled to the half it has
    expect(settings.style.getPropertyValue("scale")).toBe("0.625");
    expect(settings.style.width).toBe("80%");
    expect(settings.querySelector(".dt-program-body")!.hasAttribute("inert")).toBe(true);
    fireEvent.click(settings);
    // zoomed to settings alone: full size, nothing scaled
    await waitFor(() => expect(settings.classList.contains("dt-scaled")).toBe(false));
    expect(settings.style.width).toBe("100%");
    expect(settings.style.getPropertyValue("scale")).toBe("");
  });

  it("keeps its tree for the next time and drops programs that are no longer open", async () => {
    window.localStorage.setItem(
      "topcoat.nest",
      JSON.stringify({
        0: {
          tree: { axis: "y", children: [{ key: "archive" }, { key: "gone" }, { key: "settings" }], weights: [1, 1, 2] },
          zoom: [],
        },
      }),
    );
    show(desk(["settings", "archive"]));
    const settings = await screen.findByRole("region", { name: "Settings" });
    const archive = screen.getByRole("region", { name: "Archive" });
    // above each other as stored, the closed program's room shared
    await waitFor(() => expect(place(archive)).toEqual(["0%", "0%", "100%", "33.33%"]));
    expect(place(settings)).toEqual(["0%", "33.33%", "100%", "66.67%"]);
    await waitFor(() => expect(JSON.stringify(stored())).not.toContain("gone"));
  });

  it("lays out the overlay's windows too, without free moving", async () => {
    window.history.replaceState(null, "", "/?overlay=1#/w");
    show(desk(["settings", "archive"]));
    const archive = await screen.findByRole("region", { name: "Archive" });
    await waitFor(() => expect(place(archive)).toEqual(["50%", "0%", "50%", "100%"]));
    expect(archive.querySelector(".dt-grip")).toBeNull();
    fireEvent.keyDown(window, { key: "ArrowDown", altKey: true });
    await waitFor(() => expect(archive.style.width).toBe("100%"));
    // Esc belongs to the desktop client in the overlay (it hides the overlay): Alt+Up steps out
    fireEvent.keyDown(window, { key: "ArrowUp", altKey: true });
    await waitFor(() => expect(archive.style.width).toBe("50%"));
  });

  it("shows a solo window's one program whole", async () => {
    window.history.replaceState(null, "", "/?solo=archive#/w/archive");
    show(desk(["settings", "archive"]));
    const archive = await screen.findByRole("region", { name: "Archive" });
    await waitFor(() => expect(place(archive)).toEqual(["0%", "0%", "100%", "100%"]));
    fireEvent.keyDown(window, { key: "ArrowDown", altKey: true });
    await act(async () => new Promise((r) => setTimeout(r, 20)));
    expect(place(archive)).toEqual(["0%", "0%", "100%", "100%"]);
  });
});
