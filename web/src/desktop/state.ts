// The desktop as data (docs/DESKTOP.md): which programs are open, minimised, focused or maximised
// and which view each shows. Pure functions, so the rules are tested without a browser; the hub
// stores the same shape in the person's preferences and clamps it.
import type { PropValue } from "../layout/types";

export interface ProgramView {
  module: string;
  props: Record<string, PropValue>;
}

export interface DesktopState {
  /** The order on the taskbar's left part and in the tiling. */
  open: string[];
  minimised: string[];
  focus: string | null;
  maximised: string | null;
  views: Record<string, ProgramView>;
}

/** What a program is to the desktop: its key and its views in order. */
export interface ProgramInfo {
  key: string;
  modules: string[];
  /** The view it starts with. */
  first: string;
}

export const EMPTY: DesktopState = { open: [], minimised: [], focus: null, maximised: null, views: {} };

/** The programs that are shown on the desktop (open and not minimised), in order. */
export function visible(s: DesktopState): string[] {
  return s.open.filter((k) => !s.minimised.includes(k));
}

/** Only what exists for the person: unknown programs leave, a view that the program does not hold
 *  and that is not a view opened by a link (a node, a model) falls back to the first. */
export function settle(s: DesktopState, programs: ProgramInfo[], known: (module: string) => boolean): DesktopState {
  const keys = new Set(programs.map((p) => p.key));
  const open = s.open.filter((k) => keys.has(k));
  const views: Record<string, ProgramView> = {};
  for (const k of open) {
    const v = s.views[k];
    if (v && known(v.module)) views[k] = v;
  }
  const minimised = s.minimised.filter((k) => open.includes(k));
  const shown = open.filter((k) => !minimised.includes(k));
  const focus = s.focus && shown.includes(s.focus) ? s.focus : (shown[shown.length - 1] ?? null);
  const maximised = s.maximised && shown.includes(s.maximised) ? s.maximised : null;
  return { open, minimised, focus, maximised, views };
}

/** Open a program (or bring it forward), optionally on one view. */
export function openProgram(s: DesktopState, key: string, view?: ProgramView): DesktopState {
  const open = s.open.includes(key) ? s.open : [...s.open, key];
  const views = view ? { ...s.views, [key]: view } : s.views;
  const maximised = s.maximised && s.maximised !== key ? key : s.maximised;
  return { open, minimised: s.minimised.filter((k) => k !== key), focus: key, maximised, views };
}

export function closeProgram(s: DesktopState, key: string): DesktopState {
  const open = s.open.filter((k) => k !== key);
  const views = { ...s.views };
  delete views[key];
  const next = { ...s, open, views, minimised: s.minimised.filter((k) => k !== key) };
  return refocus(next, key);
}

export function minimise(s: DesktopState, key: string): DesktopState {
  if (!s.open.includes(key) || s.minimised.includes(key)) return s;
  return refocus({ ...s, minimised: [...s.minimised, key] }, key);
}

/** After `gone` left the desktop: focus the last program still shown, and no maximised one that
 *  is not shown. */
function refocus(s: DesktopState, gone: string): DesktopState {
  const shown = visible(s);
  const focus = s.focus === gone || (s.focus && !shown.includes(s.focus)) ? (shown[shown.length - 1] ?? null) : s.focus;
  const maximised = s.maximised === gone ? null : s.maximised;
  return { ...s, focus, maximised };
}

export function toggleMaximise(s: DesktopState, key: string): DesktopState {
  if (!s.open.includes(key)) return s;
  const base = openProgram(s, key);
  return { ...base, maximised: s.maximised === key ? null : key };
}

/** The taskbar button: open what is closed, minimise what is in front, bring forward the rest. */
export function taskbarClick(s: DesktopState, key: string): DesktopState {
  if (s.open.includes(key) && !s.minimised.includes(key) && s.focus === key) return minimise(s, key);
  return openProgram(s, key);
}

export function setView(s: DesktopState, key: string, view: ProgramView): DesktopState {
  return openProgram(s, key, view);
}

/** The view a program shows: its own choice, else its first. */
export function viewOf(s: DesktopState, p: ProgramInfo): ProgramView {
  return s.views[p.key] ?? { module: p.first, props: {} };
}

/** Pages that are a tab of another view (docs/DESKTOP.md): a link to one opens that view on it. */
export const TAB_OF: Record<string, { module: string; tab: string }> = {
  "page.inventory": { module: "page.overview", tab: "inventory" },
  "page.scheduling": { module: "page.models", tab: "scheduling" },
  "page.releases": { module: "page.overview", tab: "releases" },
  "page.access": { module: "layout.workspaces", tab: "keys" },
};

/** The view a request for `module` is shown in: itself, or the view it is a tab of. */
export function viewFor(module: string, props: ProgramView["props"]): ProgramView {
  const home = TAB_OF[module];
  return home ? { module: home.module, props: { ...props, tab: home.tab } } : { module, props };
}

/** Which program a view belongs in: the one in front if it holds it, else the first that does.
 *  A view that is opened by a link and listed nowhere (a node, a model) belongs to the program that
 *  holds its list (the machines, the models). */
const HOME_OF: Record<string, string> = {
  "page.node": "page.overview",
  "page.model": "page.models",
  "page.tasks": "page.inbox",
  "page.theme-workshop": "page.themes",
};

export function programFor(programs: ProgramInfo[], module: string, focus: string | null): string | null {
  const wanted = HOME_OF[module] ?? module;
  const front = programs.find((p) => p.key === focus);
  if (front?.modules.includes(wanted)) return front.key;
  return programs.find((p) => p.modules.includes(wanted))?.key ?? null;
}

/** The one program a window shows on its own (`?solo=<key>`, a window of the desktop client):
 *  no taskbar, no tiling, and its state stays in the window instead of the person's preferences,
 *  so several windows do not overwrite each other's desktop. */
export function soloOf(search: string): string | null {
  const key = new URLSearchParams(search).get("solo");
  return key && /^[a-z0-9][a-z0-9-]{0,39}$/.test(key) ? key : null;
}

/** The overlay of the desktop client (`?overlay=1`): the whole desktop, on a see-through page
 *  above a blurred screen. A solo window is never an overlay. */
export function overlayOf(search: string): boolean {
  const q = new URLSearchParams(search);
  return q.get("overlay") === "1" && !q.has("solo");
}

/** The screen an overlay page stands on (`?screen=<id>`, one window per screen, client 0.2 on)
 *  and whether its desktop is its own (`own=1`): a screen beside the primary one keeps its desktop
 *  in this web view; the primary one shows the person's desktop from the hub, like the browser. */
export function overlayScreenOf(search: string): { id: string; own: boolean } | null {
  const q = new URLSearchParams(search);
  const id = q.get("screen") ?? "";
  if (!overlayOf(search) || !/^[a-z0-9]{1,40}$/.test(id)) return null;
  return { id, own: q.get("own") === "1" };
}

const OWN_KEY = "topcoat.overlay.desk.";

const strings = (v: unknown): string[] => (Array.isArray(v) ? v.filter((x): x is string => typeof x === "string") : []);

/** The own desktop of a screen, as this web view kept it (an empty one when there is none). The
 *  programs and views in it are cleaned up by `settle` like the hub's. */
export function loadOwnDesk(id: string): DesktopState {
  try {
    const raw: unknown = JSON.parse(window.localStorage.getItem(OWN_KEY + id) ?? "null");
    if (!raw || typeof raw !== "object") return EMPTY;
    const r = raw as Record<string, unknown>;
    const views: Record<string, ProgramView> = {};
    if (r.views && typeof r.views === "object") {
      for (const [k, v] of Object.entries(r.views as Record<string, unknown>)) {
        const m = (v as { module?: unknown } | null)?.module;
        if (typeof m === "string") views[k] = { module: m, props: {} };
      }
    }
    return {
      open: strings(r.open),
      minimised: strings(r.minimised),
      focus: typeof r.focus === "string" ? r.focus : null,
      maximised: typeof r.maximised === "string" ? r.maximised : null,
      views,
    };
  } catch {
    return EMPTY;
  }
}

export function saveOwnDesk(id: string, s: DesktopState): void {
  try {
    window.localStorage.setItem(OWN_KEY + id, JSON.stringify(s));
  } catch {
    /* a web view without storage: the desktop lasts as long as the page */
  }
}

/** The key under which an overlay page keeps its own things (window places, tiles): per screen
 *  for a screen beside the primary one, the old key for the primary one. */
export function overlayStoreKey(base: string, search: string): string {
  const place = overlayScreenOf(search);
  return place?.own ? `${base}.${place.id}` : base;
}

/** The desktop of a solo window: just that program, in front and maximised. */
export function soloState(key: string, views: DesktopState["views"]): DesktopState {
  return { open: [key], minimised: [], focus: key, maximised: key, views };
}

/** The address of a program's solo window, with the view it is to open on. The desktop client
 *  sees a page go there and opens (or brings forward) that program's own window instead. */
/** Clipper's talk at a window (AltGr+C): open, folded out into the column or not, and its
 *  conversation once it has one. A window that is pinned takes it along (2026-11-13). */
export interface Talk {
  open: boolean;
  conversationId: string | null;
}

export function soloHref(key: string, view?: ProgramView, talk?: Talk): string {
  const q = new URLSearchParams({ solo: key });
  if (view) q.set("view", JSON.stringify(view));
  if (talk) {
    q.set("talk", talk.conversationId ?? "");
    if (talk.open) q.set("talkOpen", "1");
  }
  return `?${q.toString()}#/w/${key}`;
}

/** The talk a solo window's address brings along (written by `soloHref`), or null. */
export function soloTalkOf(search: string): Talk | null {
  const q = new URLSearchParams(search);
  if (!q.has("talk")) return null;
  const id = q.get("talk") ?? "";
  return { open: q.get("talkOpen") === "1", conversationId: /^[0-9a-f-]{36}$/i.test(id) ? id : null };
}

/** The view a solo window's address asks for (written by `soloHref`), or null. */
export function soloViewOf(search: string): ProgramView | null {
  const raw = new URLSearchParams(search).get("view");
  if (!raw || raw.length > 4000) return null;
  try {
    const v: unknown = JSON.parse(raw);
    if (!v || typeof v !== "object") return null;
    const { module, props } = v as { module?: unknown; props?: unknown };
    if (typeof module !== "string") return null;
    const kept: Record<string, PropValue> = {};
    for (const [k, x] of Object.entries(props && typeof props === "object" ? props : {})) {
      if (x === null || ["string", "number", "boolean"].includes(typeof x)) kept[k] = x as PropValue;
    }
    return { module, props: kept };
  } catch {
    return null;
  }
}

/** One change in a solo window: the window keeps its own program, whatever the change did, and a
 *  program the change brought forward goes to a window of its own (with the view it asked for). */
export function soloStep(
  solo: string,
  before: DesktopState,
  after: DesktopState,
): { state: DesktopState; handoff: { key: string; view?: ProgramView } | null } {
  const own = after.views[solo] ?? before.views[solo];
  const state = soloState(solo, own ? { [solo]: own } : {});
  const other =
    after.focus && after.focus !== solo
      ? after.focus
      : (after.open.find((k) => k !== solo && !before.open.includes(k)) ?? null);
  if (!other) return { state, handoff: null };
  // only a view the change set itself travels; else the other window opens as the person left it
  const view = after.views[other];
  const changed = view && JSON.stringify(view) !== JSON.stringify(before.views[other]);
  return { state, handoff: changed ? { key: other, view } : { key: other } };
}
