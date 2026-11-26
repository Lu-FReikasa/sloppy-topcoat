import { describe, expect, it } from "vitest";
import {
  EMPTY,
  closeProgram,
  loadOwnDesk,
  minimise,
  openProgram,
  overlayScreenOf,
  overlayStoreKey,
  programFor,
  saveOwnDesk,
  settle,
  setView,
  soloHref,
  overlayOf,
  soloOf,
  soloState,
  soloStep,
  soloViewOf,
  startKey,
  taskbarClick,
  toggleMaximise,
  viewOf,
  visible,
  withStart,
  type DesktopState,
  type ProgramInfo,
} from "./state";
import { fits, inTiles, tile } from "./tiling";

const PROGRAMS: ProgramInfo[] = [
  { key: "inbox", modules: ["page.inbox", "page.board"], first: "page.inbox" },
  { key: "chat", modules: ["page.chat", "page.library"], first: "page.chat" },
  { key: "admin", modules: ["page.chat", "page.overview", "page.models"], first: "page.chat" },
];
const known = () => true;

describe("the desktop state", () => {
  it("opens a program at the end and brings it forward", () => {
    let s = openProgram(EMPTY, "chat");
    s = openProgram(s, "inbox");
    expect(s.open).toEqual(["chat", "inbox"]);
    expect(s.focus).toBe("inbox");
    s = openProgram(s, "chat");
    expect(s.open).toEqual(["chat", "inbox"]); // not twice
    expect(s.focus).toBe("chat");
  });

  it("minimises to the taskbar and focuses what is left", () => {
    let s = openProgram(openProgram(EMPTY, "chat"), "inbox");
    s = minimise(s, "inbox");
    expect(visible(s)).toEqual(["chat"]);
    expect(s.focus).toBe("chat");
    s = openProgram(s, "inbox");
    expect(visible(s)).toEqual(["chat", "inbox"]);
  });

  it("the taskbar button opens, brings forward, and minimises what is in front", () => {
    let s = taskbarClick(EMPTY, "chat");
    expect(s.focus).toBe("chat");
    s = taskbarClick(s, "inbox");
    expect(s.focus).toBe("inbox");
    s = taskbarClick(s, "chat"); // in the back: forward
    expect(s.focus).toBe("chat");
    s = taskbarClick(s, "chat"); // in front: minimised
    expect(s.minimised).toEqual(["chat"]);
    expect(s.focus).toBe("inbox");
  });

  it("closes a program and forgets its view", () => {
    let s = setView(openProgram(EMPTY, "inbox"), "chat", { module: "page.library", props: {} });
    s = closeProgram(s, "chat");
    expect(s.open).toEqual(["inbox"]);
    expect(s.views.chat).toBeUndefined();
    expect(s.focus).toBe("inbox");
  });

  it("maximises one program; another one brought forward is shown maximised instead", () => {
    let s = openProgram(openProgram(EMPTY, "chat"), "inbox");
    s = toggleMaximise(s, "chat");
    expect(s.maximised).toBe("chat");
    s = openProgram(s, "inbox");
    expect(s.maximised).toBe("inbox");
    s = toggleMaximise(s, "inbox");
    expect(s.maximised).toBeNull();
    s = toggleMaximise(s, "chat");
    s = minimise(s, "chat");
    expect(s.maximised).toBeNull();
  });

  it("a program shows its own view, else its first", () => {
    const chat = PROGRAMS[1] as ProgramInfo;
    expect(viewOf(EMPTY, chat)).toEqual({ module: "page.chat", props: {} });
    const s = setView(EMPTY, "chat", { module: "page.library", props: { id: "r1" } });
    expect(viewOf(s, chat)).toEqual({ module: "page.library", props: { id: "r1" } });
    expect(s.open).toEqual(["chat"]);
  });

  it("keeps only what exists for the person", () => {
    const stored: DesktopState = {
      open: ["chat", "gone", "admin"],
      minimised: ["admin", "gone"],
      focus: "gone",
      maximised: "admin",
      views: { chat: { module: "page.nope", props: {} }, gone: { module: "page.chat", props: {} } },
    };
    const s = settle(stored, PROGRAMS, (m) => m !== "page.nope");
    expect(s.open).toEqual(["chat", "admin"]);
    expect(s.minimised).toEqual(["admin"]);
    expect(s.focus).toBe("chat");
    expect(s.maximised).toBeNull(); // a minimised program is not in front
    expect(s.views).toEqual({});
    expect(settle(stored, PROGRAMS, known).views.chat?.module).toBe("page.nope");
  });

  it("finds the program a view belongs in", () => {
    expect(programFor(PROGRAMS, "page.board", null)).toBe("inbox");
    expect(programFor(PROGRAMS, "page.chat", null)).toBe("chat");
    expect(programFor(PROGRAMS, "page.chat", "admin")).toBe("admin"); // the one in front holds it
    expect(programFor(PROGRAMS, "page.node", null)).toBe("admin"); // with the machines
    expect(programFor(PROGRAMS, "page.model", "chat")).toBe("admin"); // with the models
    expect(programFor(PROGRAMS, "page.nope", null)).toBeNull();
  });
});

// a load with no program used last (#115)
describe("where a load goes when no program was used last", () => {
  const research = { key: "research", modules: ["page.research"], first: "page.research" };
  it("is Start wherever it stands, else the first program", () => {
    expect(startKey([research, ...PROGRAMS])).toBe("chat");
    expect(startKey([research, ...PROGRAMS.slice(0, 1)])).toBe("research");
    expect(startKey([])).toBeNull();
  });
  it("opens Start only when nothing is in front, also when everything is minimised", () => {
    const front = openProgram(EMPTY, "inbox");
    expect(withStart(front, PROGRAMS)).toBe(front);
    const down = minimise(front, "inbox");
    expect(withStart(down, PROGRAMS)).toMatchObject({ open: ["inbox", "chat"], minimised: ["inbox"], focus: "chat" });
    const startDown = minimise(openProgram(EMPTY, "chat"), "chat");
    expect(withStart(startDown, PROGRAMS)).toMatchObject({ open: ["chat"], minimised: [], focus: "chat" });
  });
  it("leaves the desktop alone for a person without Start", () => {
    expect(withStart(EMPTY, PROGRAMS.slice(0, 1))).toBe(EMPTY);
  });
});

describe("auto-tiling", () => {
  it("fills the desktop and leaves no gap", () => {
    for (let n = 1; n <= 9; n += 1) {
      const rects = tile(n);
      expect(rects).toHaveLength(n);
      const area = rects.reduce((sum, r) => sum + r.w * r.h, 0);
      expect(area).toBeCloseTo(1, 6);
      for (const r of rects) {
        expect(r.x + r.w).toBeLessThanOrEqual(1 + 1e-9);
        expect(r.y + r.h).toBeLessThanOrEqual(1 + 1e-9);
      }
    }
    expect(tile(0)).toEqual([]);
  });

  it("puts one large and two small for three", () => {
    expect(tile(3)).toEqual([
      { x: 0, y: 0, w: 0.5, h: 1 },
      { x: 0.5, y: 0, w: 0.5, h: 0.5 },
      { x: 0.5, y: 0.5, w: 0.5, h: 0.5 },
    ]);
  });
});

describe("how many programs fit", () => {
  it("counts tiles of at least 480 by 320, between one and six", () => {
    expect(fits(1024, 734)).toBe(4);
    expect(fits(1920, 1000)).toBe(6);
    expect(fits(700, 500)).toBe(1);
    expect(fits(0, 0)).toBe(6); // not measured yet
  });

  it("keeps the one in front and the ones opened last, in the taskbar's order", () => {
    expect(inTiles(["a", "b", "c", "d", "e"], "a", 3)).toEqual(["a", "d", "e"]);
    expect(inTiles(["a", "b", "c"], "b", 1)).toEqual(["b"]);
    expect(inTiles(["a", "b"], null, 4)).toEqual(["a", "b"]);
  });
});

describe("the overlay (the desktop client)", () => {
  it("is asked for with overlay=1 and never in a solo window", () => {
    expect(overlayOf("?overlay=1")).toBe(true);
    expect(overlayOf("?overlay=0")).toBe(false);
    expect(overlayOf("")).toBe(false);
    expect(overlayOf("?overlay=1&solo=dev")).toBe(false);
  });
});

describe("a solo window (the desktop client)", () => {
  it("reads its program from the address and nothing else", () => {
    expect(soloOf("?solo=research")).toBe("research");
    expect(soloOf("?solo=Research")).toBeNull();
    expect(soloOf("?solo=../x")).toBeNull();
    expect(soloOf("")).toBeNull();
  });
  it("shows that program in front and maximised, with the person's views", () => {
    const s = soloState("dev", { dev: { module: "page.repos", props: {} } });
    expect(s).toEqual({ open: ["dev"], minimised: [], focus: "dev", maximised: "dev", views: { dev: { module: "page.repos", props: {} } } });
  });
  it("carries a view in its address and reads back only that shape", () => {
    const view = { module: "page.project", props: { project: "p-1", deep: true } };
    const href = soloHref("project", view);
    expect(href.endsWith("#/w/project")).toBe(true);
    const search = href.slice(0, href.indexOf("#"));
    expect(soloOf(search)).toBe("project");
    expect(soloViewOf(search)).toEqual(view);
    expect(soloViewOf("?view=" + encodeURIComponent('{"module":"m","props":{"x":{"y":1}}}'))).toEqual({ module: "m", props: {} });
    expect(soloViewOf("?view=nope")).toBeNull();
    expect(soloViewOf("?solo=dev")).toBeNull();
  });
  it("keeps its own program and hands another one to a window of its own", () => {
    const before = soloState("chat", { chat: { module: "page.chat", props: {} } });
    const handed = soloStep("chat", before, setView(before, "research", { module: "page.library", props: { id: "r1" } }));
    expect(handed.state).toEqual(before);
    expect(handed.handoff).toEqual({ key: "research", view: { module: "page.library", props: { id: "r1" } } });
    // a program opened without a view opens as the person left it
    expect(soloStep("chat", before, openProgram(before, "dev")).handoff).toEqual({ key: "dev" });
  });
  it("changes its own view without opening anything", () => {
    const before = soloState("chat", {});
    const step = soloStep("chat", before, setView(before, "chat", { module: "page.inbox", props: {} }));
    expect(step.handoff).toBeNull();
    expect(step.state.views).toEqual({ chat: { module: "page.inbox", props: {} } });
    // closing its own program leaves the window as it was
    expect(soloStep("chat", step.state, closeProgram(step.state, "chat"))).toEqual({ state: step.state, handoff: null });
  });
});

describe("the talk of a pinned window (2026-11-13)", () => {
  it("goes along in the solo address and comes back from it", async () => {
    const { soloHref, soloTalkOf } = await import("./state");
    const id = "3d78718c-7ee8-410b-b65c-1c59bff24176";
    const href = soloHref("research", { module: "page.knowledge", props: {} }, { open: true, conversationId: id });
    const search = href.slice(0, href.indexOf("#"));
    expect(soloTalkOf(search)).toEqual({ open: true, conversationId: id });
    // a talk that has said nothing yet comes along too, closed and without a conversation
    const fresh = soloHref("research", undefined, { open: false, conversationId: null });
    expect(soloTalkOf(fresh.slice(0, fresh.indexOf("#")))).toEqual({ open: false, conversationId: null });
    expect(soloTalkOf("?solo=research")).toBeNull();
    expect(soloTalkOf("?solo=x&talk=not-an-id")).toEqual({ open: false, conversationId: null });
  });
});

describe("talkPlace", () => {
  it("keeps the perch inside the window below its title bar and the column in the right part of its frame", async () => {
    const { talkPlace, TALK_SHARE } = await import("./talkPlace");
    const b = { x: 0.1, y: 0.2, w: 0.5, h: 0.6 };
    // inside the window (2026-11-20): with tiled windows a talk over its edge lay on the neighbour
    const perch = talkPlace(b, false);
    expect(perch).toMatchObject({ left: "10%", width: "50%" });
    expect(perch.top).toContain("1.9rem");
    const col = talkPlace(b, true);
    expect(parseFloat(col.left!)).toBeCloseTo((0.1 + 0.5 * (1 - TALK_SHARE)) * 100);
    expect(parseFloat(col.width!)).toBeCloseTo(0.5 * TALK_SHARE * 100);
    expect(col.top).toContain("1.9rem");
  });
});

describe("one overlay window per screen", () => {
  it("knows its screen and whether its desktop is its own", () => {
    expect(overlayScreenOf("?overlay=1&screen=display2&own=1")).toEqual({ id: "display2", own: true });
    expect(overlayScreenOf("?overlay=1&screen=display1")).toEqual({ id: "display1", own: false });
    // no overlay, no screen, or a name the client would not write
    expect(overlayScreenOf("?screen=display2&own=1")).toBeNull();
    expect(overlayScreenOf("?overlay=1")).toBeNull();
    expect(overlayScreenOf("?overlay=1&screen=../x&own=1")).toBeNull();
  });

  it("keeps a screen's own things under its own keys, the primary one under the old ones", () => {
    expect(overlayStoreKey("topcoat.overlay.boxes", "?overlay=1&screen=display2&own=1")).toBe("topcoat.overlay.boxes.display2");
    expect(overlayStoreKey("topcoat.overlay.boxes", "?overlay=1&screen=display1")).toBe("topcoat.overlay.boxes");
    expect(overlayStoreKey("topcoat.overlay.boxes", "?overlay=1")).toBe("topcoat.overlay.boxes");
  });

  it("reads back the own desktop it stored, and nothing broken", () => {
    window.localStorage.clear();
    expect(loadOwnDesk("display2")).toEqual(EMPTY);
    const s = { open: ["chat"], minimised: [], focus: "chat", maximised: null, views: { chat: { module: "page.chat", props: {} } } };
    saveOwnDesk("display2", s);
    expect(loadOwnDesk("display2")).toEqual(s);
    window.localStorage.setItem("topcoat.overlay.desk.display3", '{"open": "chat", "views": {"x": 1}}');
    expect(loadOwnDesk("display3")).toEqual(EMPTY);
    window.localStorage.setItem("topcoat.overlay.desk.display4", "not json");
    expect(loadOwnDesk("display4")).toEqual(EMPTY);
  });
});
