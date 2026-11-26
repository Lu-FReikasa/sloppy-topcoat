import {
  Fragment,
  useCallback,
  useEffect,
  useLayoutEffect,
  useMemo,
  useRef,
  useState,
  type CSSProperties,
  type PointerEvent as ReactPointerEvent,
} from "react";
import { useTranslation } from "react-i18next";
import type { ApiClient } from "../api/client";
import type { StreamStatus } from "../api/sse";
import { AddNodeDialog } from "../components/AddNodeDialog";
import type { Language } from "../i18n";
import { PersonaPrompt } from "../instructions/PersonaPrompt";
import { ClipperDock } from "../persona/ClipperDock";
import { TALK_SHARE, talkPlace } from "./talkPlace";
import { INTENT_EVENT, requestOpen, takeIntent } from "../layout/intent";
import { DOC_MODULE, docKey, docPrograms } from "./docs";
import type { ModuleCtx } from "../layout/modules/render";
import { moduleMeta } from "../layout/modules/meta";
import type { Workspace } from "../layout/types";
import { useNarrow } from "../layout/useNarrow";
import { useUi } from "../layout/useUi";
import { workspaceHref } from "../route";
import { CommandPalette, type PaletteEntry } from "./CommandPalette";
import { APPLICATIONS } from "../chat/applications";
import { LookContext, type LookControls } from "../settings/lookContext";
import { SETTINGS } from "../settings/registry";
import {
  barInset,
  firstBox,
  loadBoxes,
  MIN,
  onScreens,
  pinHref,
  saveBoxes,
  screenOf,
  screensOf,
  sizeBox,
  snapLines,
  snapMove,
  snapSize,
  WHOLE,
  zoneAt,
  type Box,
  type Grip,
} from "./floating";
import {
  dwindle,
  inset,
  loadTiles,
  ratioAt,
  saveTiles,
  setRatio,
  swap,
  tile,
  onTileNew,
  tileNew,
  tileOf,
  type Tiles,
} from "./dwindle";
import { ShellFailure, ShellLoading } from "../components/ShellPage";
import { FrameMeter } from "./FrameMeter";
import { clientCommand, clientConfig, installClientHandlers, installEscape, installOverlayMotion } from "./client";
import { countRender } from "./frames";
import { ProgramWindow, type TileInfo, type WindowHandlers } from "./ProgramWindow";
import { navViews, programHue, programInfo, programTitle, viewTitle } from "./programs";
import {
  closeProgram,
  EMPTY,
  loadOwnDesk,
  minimise,
  openProgram,
  overlayOf,
  overlayScreenOf,
  programFor,
  saveOwnDesk,
  settle,
  setView,
  soloHref,
  soloOf,
  soloTalkOf,
  type Talk,
  soloState,
  soloStep,
  soloViewOf,
  taskbarClick,
  TAB_OF,
  toggleMaximise,
  viewFor,
  viewOf,
  visible,
  withStart,
  type DesktopState,
} from "./state";
import { Taskbar, type Signal } from "./Taskbar";
import { usePreviewTheme } from "./preview";
import { allThemes, layoutRects, leaveMs, resolveTheme, safeCss } from "./theme";
import { MIN_TILE, fits, inTiles, pct } from "./tiling";
import {
  keysOf,
  loadNests,
  followZoom,
  rects as nestRects,
  reveal,
  saveNests,
  syncNest,
  validZoom,
  zoomIn,
  zoomOut,
  zoomToFit,
  type NestState,
  type Placed,
  type ZoomPath,
} from "./nest";

const sameZoom = (a: readonly number[], b: readonly number[]) => a.length === b.length && a.every((v, i) => b[i] === v);

interface Props {
  api: ApiClient;
  ctx: ModuleCtx;
  /** The program in the address (`#/w/<key>`): it is opened and brought forward. */
  routeKey?: string;
  /** The app is moving another address (`#/wiki/<id>`) here: the address is not the desktop's yet. */
  moving?: boolean;
  /** Bumped by the stream when the hub says the interface data changed. */
  uiTick: number;
  status: StreamStatus;
  onSaveLanguage: (lang: Language) => void;
  onLogout: () => void;
}

/** Is the person typing somewhere a key belongs to (a field, an editor)? */
function typing(target: EventTarget | null): boolean {
  if (!(target instanceof HTMLElement)) return false;
  return target.isContentEditable || ["INPUT", "TEXTAREA", "SELECT"].includes(target.tagName);
}

/** The desktop (docs/DESKTOP.md, D70): programs that open, minimise, maximise and tile themselves,
 *  the taskbar at the bottom and the command palette. Replaces the shells and the menu bar. */
export function Desktop({ api, ctx, routeKey, moving = false, uiTick, status, onSaveLanguage, onLogout }: Props) {
  countRender();
  const { t } = useTranslation();
  const ui = useUi(api, uiTick);
  // the preferences as they are now (a stable function, for handlers made once)
  const prefsNow = ui.latestPrefs;
  const preview = usePreviewTheme(); // a theme that is being edited (ThemesPage)
  const narrow = useNarrow();
  const [palette, setPalette] = useState(false);
  // the windows Clipper is attached to (AltGr+C on the window in front), with their talk; a solo
  // window brings the talk of the window it was pinned from
  const [talks, setTalks] = useState<Record<string, Talk>>(() => {
    const key = soloOf(window.location.search);
    const talk = soloTalkOf(window.location.search);
    return key && talk ? { [key]: talk } : {};
  });
  const talksNow = useRef(talks);
  useEffect(() => {
    talksNow.current = talks;
  }, [talks]);
  const toggleTalk = useCallback(
    (key: string) =>
      setTalks((all) => {
        if (all[key]) {
          const { [key]: _gone, ...rest } = all;
          return rest;
        }
        return { ...all, [key]: { open: false, conversationId: null } };
      }),
    [],
  );
  const changeTalk = useCallback(
    (key: string, over: Partial<Talk>) => setTalks((all) => (all[key] ? { ...all, [key]: { ...all[key], ...over } } : all)),
    [],
  );
  const [addNode, setAddNode] = useState(false);
  const [intentTick, setIntentTick] = useState(0);
  // what the inbox holds: its button on the taskbar lights up (amber unread, red urgent)
  const [signals, setSignals] = useState<Record<string, Signal>>({});
  useEffect(() => {
    let alive = true;
    api.inboxCounts().then(
      (c) => {
        if (!alive) return;
        const urgent = (c.urgent ?? 0) > 0;
        setSignals(urgent ? { inbox: "red" } : c.unread > 0 ? { inbox: "amber" } : {});
      },
      () => undefined,
    );
    return () => {
      alive = false;
    };
  }, [api, ctx.inboxTick]);
  // the size of the desktop: how many programs fit side by side
  const stage = useRef<HTMLElement>(null);
  const [size, setSize] = useState({ w: 0, h: 0 });
  useEffect(() => {
    const el = stage.current;
    if (!el || typeof ResizeObserver === "undefined") return;
    const watch = new ResizeObserver(([entry]) => {
      if (entry) setSize({ w: entry.contentRect.width, h: entry.contentRect.height });
    });
    watch.observe(el);
    return () => watch.disconnect();
  }, [ui.boot]);

  // the programs, and the documents of the Ablage that are open in windows of their own (docs.ts)
  const docFallback = t("shelf.document");
  const soloKey = useMemo(() => soloOf(window.location.search), []);
  const soloDocView = useMemo(() => (soloKey ? soloViewOf(window.location.search) : null), [soloKey]);
  const deskNow = ui.prefs?.desktop;
  const docWindows = useMemo(
    () =>
      docPrograms(
        [deskNow, soloKey && soloDocView ? { open: [], views: { [soloKey]: soloDocView } } : null],
        [soloKey],
        docFallback,
      ),
    [deskNow, soloKey, soloDocView, docFallback],
  );
  const programs: Workspace[] = useMemo(() => [...ui.workspaces, ...docWindows], [ui.workspaces, docWindows]);
  // by what is offered, not by the whole interface data: that changes with every preference
  const offered = ui.boot?.modules;
  const modules = useMemo(() => new Map((offered ?? []).map((m) => [m.id, m])), [offered]);
  const infos = useMemo(() => programs.map((p) => programInfo(p, modules)), [programs, modules]);
  // Settings is a program of its own: the ⚙ of an application, the name menu and the palette open it
  const hasSettings = programs.some((p) => p.key === "settings");
  // the program with the overview of the applications (Start): the taskbar's start button
  const startProgram = programs.find((p) => p.modules.includes("page.home"))?.key ?? null;
  const known = useCallback((m: string) => moduleMeta(m) !== undefined, []);
  const prefs = ui.prefs;
  // a window of the desktop client that shows one program (`?solo=<key>`): its state is its own
  const solo = useMemo(() => soloOf(window.location.search), []);
  // the view another window sent this one to (`soloHref`), else the person's last one
  // the overlay of the desktop client (`?overlay=1`): the same desktop, on a see-through page
  const overlay = useMemo(() => overlayOf(window.location.search), []);
  // the screens the overlay spans (one window over all of them): the taskbar, Clipper and the
  // dialogs stay on the primary one, the programs go wherever the person drags them
  const screens = useMemo(() => (overlay ? screensOf(window.location.search) : [WHOLE]), [overlay]);
  const primary = screens[0] ?? WHOLE;
  // one overlay window per screen: its windows end where its taskbar begins (they lay under it)
  const screenArea = useRef<HTMLDivElement>(null);
  const [barRoom, setBarRoom] = useState({ top: 0, right: 0, bottom: 0, left: 0 });
  const ownScreenOnly = overlay && screens.length === 1;
  useLayoutEffect(() => {
    const area = screenArea.current;
    if (!ownScreenOnly || !area) return;
    const measure = () => {
      const bar = area.querySelector<HTMLElement>(".dt-taskbar");
      const shown = bar && bar.offsetParent !== null ? bar.getBoundingClientRect() : null;
      const next = barInset(shown, area.getBoundingClientRect());
      setBarRoom((was) =>
        was.top === next.top && was.right === next.right && was.bottom === next.bottom && was.left === next.left ? was : next,
      );
    };
    measure();
    if (typeof ResizeObserver === "undefined") return;
    const watch = new ResizeObserver(measure);
    watch.observe(area);
    const bar = area.querySelector<HTMLElement>(".dt-taskbar");
    if (bar) watch.observe(bar);
    return () => watch.disconnect();
  }, [ownScreenOnly, ui.boot]);
  // the overlay's windows float: each has its own box (moved and sized by the person)
  const [boxes, setBoxes] = useState<Record<string, Box>>(() =>
    overlay ? Object.fromEntries(Object.entries(loadBoxes()).map(([k, b]) => [k, onScreens(b, screens)])) : {},
  );
  const boxesNow = useRef(boxes);
  useEffect(() => {
    boxesNow.current = boxes;
  }, [boxes]);
  // the overlay's tiles: per screen a group of windows that share it (dwindle.ts)
  const [tileGroups, setTiles] = useState<Tiles>(() => (overlay ? loadTiles() : {}));
  const putTiles = useCallback((fn: (t: Tiles) => Tiles) => {
    setTiles((t) => {
      const next = fn(t);
      saveTiles(next);
      return next;
    });
  }, []);
  // where a window would land if let go now (a snap zone, a new divider): drawn under the windows
  const [snapPreview, setPreview] = useState<Box | null>(null);

  // Moving and sizing a window does not go through React: the pointer's moves are gathered to one
  // per frame and written to this window's element only (a move as a transform, which the
  // compositor does without layout); the box is set once, when the pointer lets go.
  // A free window snaps to screen and window edges and, dragged to a screen's edge, takes a half,
  // a quarter or the whole screen (Alt held: no snapping). A tiled window swaps places with the
  // tile it is let go on, or joins the tiles of another screen; its divider moves the split.
  const grab = useCallback((key: string, e: ReactPointerEvent<HTMLElement>, grip: Grip) => {
    const area = stage.current?.getBoundingClientRect();
    const el = e.currentTarget.closest<HTMLElement>(".dt-program");
    if (e.button !== 0 || !el || !area || !area.width || !area.height) return;
    // where the window stands now, as drawn (its own box, or the first place it got): the
    // fractions of its style, read without a layout
    const frac = (v: string) => parseFloat(v) / 100;
    const boxOf = (node: HTMLElement): Box => ({
      x: frac(node.style.left),
      y: frac(node.style.top),
      w: frac(node.style.width),
      h: frac(node.style.height),
    });
    const start = boxOf(el);
    if (![start.x, start.y, start.w, start.h].every(Number.isFinite)) return;
    e.preventDefault();
    const tiled = el.dataset.tileScreen !== undefined;
    const others = [...(stage.current?.querySelectorAll<HTMLElement>(".dt-program") ?? [])].filter((n) => n !== el);
    const lines = snapLines(others.map(boxOf), screens);
    const t = { x: 12 / area.width, y: 12 / area.height }; // snap within 12 px
    const edge = { x: 6 / area.width, y: 6 / area.height }; // a zone within 6 px of a screen edge
    const min = { w: MIN.w * primary.w, h: MIN.h * primary.h };
    const [x0, y0] = [e.clientX, e.clientY];
    const at = (ev: PointerEvent) => [(ev.clientX - area.left) / area.width, (ev.clientY - area.top) / area.height] as const;
    let last = start;
    let zone: Box | null = null;
    let ratio: number | null = null;
    let frame = 0;
    let shown = "";
    const show = (b: Box | null) => {
      const k = b ? `${b.x},${b.y},${b.w},${b.h}` : "";
      if (k !== shown) {
        shown = k;
        setPreview(b);
      }
    };
    // Clipper attached to this window moves with it in the same frame (he hung behind until the
    // window was let go, 2026-11-12): at its upper right corner, or the column inside its frame
    const docks = [...(stage.current?.querySelectorAll<HTMLElement>(`[data-dock-for="${key}"]`) ?? [])];
    const placeDocks = (b: Box) => {
      for (const d of docks) Object.assign(d.style, talkPlace(b, d.dataset.talk === "column"));
    };
    const draw = () => {
      frame = 0;
      if (grip === "move") {
        // the property `translate`, not `transform`: a theme's animation that keeps its last frame
        // holds `transform` and would keep the window in place until it is let go (live 2026-11-16)
        const move = `${(last.x - start.x) * area.width}px ${(last.y - start.y) * area.height}px`;
        el.style.setProperty("translate", move);
        for (const d of docks) d.style.setProperty("translate", move);
      } else if (!tiled) {
        el.style.left = pct(last.x);
        el.style.top = pct(last.y);
        el.style.width = pct(last.w);
        el.style.height = pct(last.h);
        placeDocks(last);
      }
    };
    const onMove = (ev: PointerEvent) => {
      const [px, py] = at(ev);
      const free = sizeBox(start, grip, (ev.clientX - x0) / area.width, (ev.clientY - y0) / area.height, min);
      if (tiled && grip !== "move") {
        // the divider after this tile: a preview of the tile's new size, set on letting go
        const [ax, ay, aw, ah] = (el.dataset.tileArea ?? "").split(",").map(Number) as [number, number, number, number];
        const split = { x: ax, y: ay, w: aw, h: ah };
        const vertical = el.dataset.tileSplit === "v";
        ratio = ratioAt(split, vertical, px, py);
        show(vertical ? { ...split, w: split.w * ratio } : { ...split, h: split.h * ratio });
        return;
      }
      if (tiled || ev.altKey) {
        last = free;
        zone = null;
        show(null);
      } else {
        const z = grip === "move" ? zoneAt(px, py, screens, edge.x, edge.y) : null;
        zone = z?.box ?? null;
        last = z ? free : snapSize(free, grip, lines, t.x, t.y, min);
        if (grip === "move" && !z) last = snapMove(free, lines, t.x, t.y);
        show(zone);
      }
      if (!frame) frame = requestAnimationFrame(draw);
    };
    const onUp = (ev: PointerEvent) => {
      window.removeEventListener("pointermove", onMove);
      window.removeEventListener("pointerup", onUp);
      window.removeEventListener("pointercancel", onUp);
      if (frame) cancelAnimationFrame(frame);
      document.documentElement.classList.remove("tc-dragging");
      el.classList.remove("dt-moving");
      el.style.removeProperty("translate");
      for (const d of docks) d.style.removeProperty("translate");
      show(null);
      if (tiled) {
        const screen = Number(el.dataset.tileScreen);
        if (grip !== "move") {
          const index = Number(el.dataset.tileIndex);
          if (ratio !== null) putTiles((all) => setRatio(all, screen, index, ratio as number));
          return;
        }
        // let go on another tile: the two change places; on another screen: join its tiles
        const [px, py] = at(ev);
        const target = others.find((n) => {
          if (n.dataset.tileScreen === undefined) return false;
          const b = boxOf(n);
          return px >= b.x && px <= b.x + b.w && py >= b.y && py <= b.y + b.h;
        });
        const other = target?.dataset.program;
        if (other) {
          putTiles((all) => swap(all, key, other));
          return;
        }
        const to = screens.findIndex((sc) => px >= sc.x && px <= sc.x + sc.w && py >= sc.y && py <= sc.y + sc.h);
        if (to >= 0 && to !== screen) putTiles((all) => tile(all, key, to));
        return;
      }
      // a zone, or let go between two screens (where nobody sees it): onto the nearest one
      last = zone ?? onScreens(last, screens);
      // the element stands where React will put it, before React does (no jump back for a frame)
      el.style.left = pct(last.x);
      el.style.top = pct(last.y);
      el.style.width = pct(last.w);
      el.style.height = pct(last.h);
      placeDocks(last);
      setBoxes((b) => ({ ...b, [key]: last }));
      saveBoxes({ ...boxesNow.current, [key]: last });
    };
    document.documentElement.classList.add("tc-dragging");
    el.classList.add("dt-moving");
    window.addEventListener("pointermove", onMove);
    window.addEventListener("pointerup", onUp);
    window.addEventListener("pointercancel", onUp);
  }, [screens, primary, putTiles]);

  // Esc in the overlay belongs to the client; it hands it to the page (client.ts)
  useEffect(() => (overlay ? installEscape() : undefined), [overlay]);
  // the client's Clipper key and the herald's "open this task"
  useEffect(
    () =>
      overlay
        ? installClientHandlers(
            () => {
              const key = prefsNow()?.desktop?.focus;
              if (key) toggleTalk(key);
            },
            (o) => requestOpen({ module: o.module, props: o.props }),
          )
        : undefined,
    [overlay, prefsNow, toggleTalk],
  );
  useEffect(() => {
    if (!overlay) return;
    // the page itself lets the blurred screen through; the programs keep their own background
    const root = document.documentElement;
    root.classList.add("tc-overlay");
    // where the primary screen lies in the window: the palette and the dialogs center there
    const vars: [string, string][] = [
      ["--scr-l", `${primary.x * 100}vw`],
      ["--scr-t", `${primary.y * 100}vh`],
      ["--scr-w", `${primary.w * 100}vw`],
      ["--scr-h", `${primary.h * 100}vh`],
    ];
    for (const [k, v] of vars) root.style.setProperty(k, v);
    return () => {
      root.classList.remove("tc-overlay");
      for (const [k] of vars) root.style.removeProperty(k);
    };
  }, [overlay, primary]);
  const soloView = useMemo(() => (solo ? soloViewOf(window.location.search) : null), [solo]);
  const [soloDesk, setSoloDesk] = useState<DesktopState | null>(null);
  // an overlay window on a screen beside the primary one keeps a desktop of its own (state.ts)
  const ownScreen = useMemo(() => {
    const place = overlay ? overlayScreenOf(window.location.search) : null;
    return place?.own ? place.id : null;
  }, [overlay]);
  const [ownDesk, setOwnDesk] = useState<DesktopState | null>(() => (ownScreen ? loadOwnDesk(ownScreen) : null));
  const state: DesktopState = useMemo(
    () =>
      solo
        ? settle(
            soloDesk ??
              soloState(solo, soloView ? { ...prefs?.desktop?.views, [solo]: soloView } : (prefs?.desktop?.views ?? {})),
            infos,
            known,
          )
        : ownDesk
        ? settle(ownDesk, infos, known)
        : prefs
        ? settle(prefs.desktop, infos, known)
        : settle(
            {
              open: [],
              minimised: [],
              focus: null,
              maximised: null,
              views: {},
            },
            infos,
            known,
          ),
    [prefs, infos, known, solo, soloDesk, soloView, ownDesk],
  );
  const { setPrefs, latestPrefs } = ui;
  // A window of the desktop changes the person's preferences; it reads them as they are when it
  // runs, so `change` stays the same function and the windows' handlers with it (a window draws
  // again only for what it shows, redraw.ts). A solo window keeps its own state.
  const changeDesk = useCallback(
    (next: (s: DesktopState) => DesktopState, lazy = false) => {
      const raw = latestPrefs()?.desktop ?? { open: [], minimised: [], focus: null, maximised: null, views: {} };
      void setPrefs({ desktop: next(settle(raw, infos, known)) }, { lazy });
    },
    [setPrefs, latestPrefs, infos, known],
  );
  const changeSolo = useCallback(
    (next: (s: DesktopState) => DesktopState) => {
      if (!solo) return;
      // another program goes to a window of its own: the desktop client sees the address and
      // opens that window, this one stays as it is
      const step = soloStep(solo, state, next(state));
      if (step.handoff) window.location.assign(soloHref(step.handoff.key, step.handoff.view));
      // like a saved preference, the solo state arrives after this render, not within it
      queueMicrotask(() => setSoloDesk(step.state));
    },
    [state, solo],
  );
  const changeOwn = useCallback(
    (next: (s: DesktopState) => DesktopState) => {
      if (!ownScreen) return;
      setOwnDesk((now) => {
        const after = next(settle(now ?? loadOwnDesk(ownScreen), infos, known));
        saveOwnDesk(ownScreen, after); // the same each time it runs
        return after;
      });
    },
    [ownScreen, infos, known],
  );
  /** `lazy`: the hub hears of it a moment later (the focus: shown at once, one write for many). */
  const change: (next: (s: DesktopState) => DesktopState, lazy?: boolean) => void = solo
    ? changeSolo
    : ownScreen
      ? changeOwn
      : changeDesk;
  const ready = ui.status === "ready";

  // a window asked for a view (`requestOpen`): the effect below looks again
  useEffect(() => {
    const onIntent = () => setIntentTick((n) => n + 1);
    window.addEventListener(INTENT_EVENT, onIntent);
    return () => window.removeEventListener(INTENT_EVENT, onIntent);
  }, []);
  useEffect(() => {
    if (!ready) return;
    const intent = takeIntent();
    if (!intent) return;
    // a program the window named shows the view; else the one that lists the page itself, else
    // the view it is a tab of
    // a document of the Ablage opens in a window of its own, one per document
    if (intent.module === DOC_MODULE) {
      const kind = intent.props.kind === "report" ? "report" : "artifact";
      const key = typeof intent.props.id === "string" ? docKey(kind, intent.props.id) : null;
      if (key) change((s) => openProgram(s, key, { module: DOC_MODULE, props: intent.props }));
      return;
    }
    const named = intent.program;
    if (named && infos.some((p) => p.key === named)) {
      change((s) => setView(s, named, { module: intent.module, props: intent.props }));
      return;
    }
    const direct = programFor(infos, intent.module, state.focus);
    const view = direct ? { module: intent.module, props: intent.props } : viewFor(intent.module, intent.props);
    const key = direct ?? programFor(infos, view.module, state.focus);
    if (key) change((s) => setView(s, key, view));
    // only a new intent opens something; the state it changes is not a reason to look again
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [ready, intentTick, infos]);

  // The address follows the program in front; an address the person brings (a bookmark, a link,
  // the back button) brings its program forward. What the desktop wrote itself is not read back.
  const written = useRef<string | null | undefined>(undefined);
  useEffect(() => {
    if (solo) {
      // a solo window keeps its program; a link to another one opens that program's own window
      if (!ready || !routeKey || routeKey === solo || !infos.some((p) => p.key === routeKey)) return;
      window.location.replace(`#/w/${solo}`);
      window.location.assign(soloHref(routeKey));
      return;
    }
    if (!ready || !routeKey || routeKey === written.current || state.focus === routeKey) return;
    if (infos.some((p) => p.key === routeKey)) change((s) => openProgram(s, routeKey));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [ready, routeKey, infos, solo]);
  // Nothing in front when the desktop is first ready (a fresh account, cleared preferences, a
  // program that is gone): Start opens, once per load (#115). A focus the person has, an address
  // that names a program or a view asked for (opened just above, its change already in the
  // preferences) wins. A solo window has its program; a screen beside the primary one keeps its own.
  const started = useRef(false);
  useEffect(() => {
    if (started.current || !ready || moving || solo || ownScreen) return;
    started.current = true;
    if (routeKey && infos.some((p) => p.key === routeKey)) return;
    const now = settle(latestPrefs()?.desktop ?? EMPTY, infos, known);
    if (withStart(now, infos) !== now) change((s) => withStart(s, infos));
    // only the first ready load decides
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [ready, moving]);
  useEffect(() => {
    if (solo) return;
    if (moving) return;
    if (!ready || (routeKey ?? null) === state.focus) return;
    written.current = state.focus;
    window.location.replace(workspaceHref(state.focus ?? undefined));
  }, [ready, routeKey, state.focus, solo, moving]);
  // a solo window names its program; the desktop client puts that into the window's title bar
  const soloProgram = solo ? programs.find((p) => p.key === solo) : undefined;
  const soloTitle = soloProgram ? programTitle(soloProgram, t) : null;
  useEffect(() => {
    if (soloTitle) document.title = `${soloTitle} · Topcoat`;
  }, [soloTitle]);

  const focusChat = useCallback(
    (key: string) => {
      const p = infos.find((x) => x.key === key);
      if (!p?.modules.includes("page.chat")) return;
      change((s) => setView(s, key, { module: "page.chat", props: {} }));
      window.requestAnimationFrame(() =>
        document.querySelector<HTMLTextAreaElement>(`[data-program="${key}"] textarea`)?.focus(),
      );
    },
    [change, infos],
  );

  // A window that closes or is minimised plays the theme's leaving first (.dt-closing for its
  // length, closeMs), then the state changes; the lengths are read when it happens (refs, set below
  // where the theme is known), so the handlers stay the same functions.
  const closeMs = useRef(0);
  const overlayMs = useRef(0);
  const [closing, setClosing] = useState<ReadonlySet<string>>(() => new Set());
  const [overlayMark, setOverlayMark] = useState<"arriving" | "leaving" | null>(null);
  const leave = useCallback(
    (key: string, next: (s: DesktopState) => DesktopState) => {
      const wait = closeMs.current;
      if (!wait) return change(next);
      setClosing((now) => new Set(now).add(key));
      window.setTimeout(() => {
        change(next);
        setClosing((now) => {
          const rest = new Set(now);
          rest.delete(key);
          return rest;
        });
      }, wait);
    },
    [change],
  );
  useEffect(() => (overlay ? installOverlayMotion(() => overlayMs.current, setOverlayMark) : undefined), [overlay]);

  // The nest layout (nest.ts, #14): a tree of the shown programs and the part of it that is framed;
  // the rest lies outside the stage and keeps running. One tree per screen: the browser has one, and
  // the overlay has one window per screen, each with its own storage.
  const themeNow = preview ?? resolveTheme(prefs?.look, ui.boot?.themes ?? []);
  const nesting = themeNow.layout.kind === "nest";
  const [nest, setNest] = useState<NestState>(() => loadNests(() => true)[0] ?? { tree: null, zoom: [] });
  useEffect(() => {
    if (nesting) saveNests({ 0: nest });
  }, [nest, nesting]);
  // A zoom moves every window at once: where each was is read from the page just before, and the
  // windows play the move from there to their new place as a transform (FLIP), for the theme's
  // length. The windows are not drawn again for it, nor mounted again.
  const nestFrom = useRef<Map<string, DOMRect> | null>(null);
  const nestMs = useRef(0);
  const zoomNest = useCallback(
    (fn: (n: NestState) => ZoomPath) => {
      const el = stage.current;
      nestFrom.current = el
        ? new Map([...el.querySelectorAll<HTMLElement>(".dt-program")].map((w) => [w.dataset.program ?? "", w.getBoundingClientRect()]))
        : null;
      setNest((n) => {
        const zoom = fn(n);
        return sameZoom(zoom, n.zoom) ? n : { ...n, zoom };
      });
    },
    [],
  );
  // a program drawn scaled down is zoomed to where it has its full size when it is clicked
  const zoomToProgram = useCallback(
    (key: string) =>
      zoomNest((n) => {
        const el = stage.current;
        const w = el?.clientWidth ?? 0;
        const h = el?.clientHeight ?? 0;
        if (!w || !h) return n.zoom;
        const area = { x: primary.x * w, y: primary.y * h, w: primary.w * w, h: primary.h * h };
        return zoomToFit(n.tree, key, area, MIN_TILE);
      }),
    [zoomNest, primary],
  );

  // The handlers of each program's window, made once per program: they read the state when they
  // run, so a window does not draw again only because the desktop did.
  const pin = useCallback(
    (key: string, el: HTMLElement) => {
      // the client sees the solo address with a pin, opens a native window at this place and
      // keeps the overlay's page where it is
      // the talk goes along: the new window goes on with it (it was lost here before, 2026-11-13)
      const talk = talksNow.current[key];
      const href = pinHref(soloHref(key, latestPrefs()?.desktop?.views?.[key], talk), el.getBoundingClientRect());
      if (talk) setTalks(({ [key]: _gone, ...rest }) => rest);
      change((s) => minimise(s, key));
      window.location.assign(href);
    },
    [change, latestPrefs],
  );
  // one overlay window per screen: a program goes to the next screen's window (the client opens it
  // there on the view it shows here) and leaves this one
  const canSend = overlay && (clientConfig()?.screens ?? 1) > 1;
  const send = useCallback(
    (key: string) => {
      const p = programs.find((x) => x.key === key);
      if (!p) return;
      let asked = false; // a state updater may run twice: the client is asked once
      change((s) => {
        const { module } = viewOf(s, { key, modules: p.modules, first: p.modules[0] ?? "" });
        if (!asked) {
          asked = true;
          queueMicrotask(() => clientCommand("send", { module }));
        }
        return closeProgram(s, key);
      });
    },
    [programs, change],
  );
  const handlers = useMemo(
    () =>
      new Map<string, WindowHandlers>(
        programs.map(({ key, modules: views }) => [
          key,
          {
            // an application's ⚙ opens Settings › Applications › it (phase 4); a program that
            // still lists its own settings view (a template an admin changed) keeps that
            onSettings: views.includes("page.program")
              ? () => change((s) => setView(s, key, { module: "page.program", props: {} }))
              : hasSettings && (APPLICATIONS as readonly string[]).includes(key)
                ? () => change((s) => setView(s, "settings", { module: "page.settings", props: { section: "apps", app: key } }))
                : undefined,
            // a click into a window brings it forward at once; the hub hears of it a moment later
            onFocus: () => change((s) => openProgram(s, key), true),
            onView: (m) => change((s) => setView(s, key, { module: m, props: {} })),
            // eslint-disable-next-line react-hooks/refs -- handler: leave() reads its refs when it runs
            onMinimise: () => leave(key, (s) => minimise(s, key)),
            onMaximise: () => change((s) => toggleMaximise(s, key)),
            onClose: () => leave(key, (s) => closeProgram(s, key)),
            onZoom: () => {
              change((s) => openProgram(s, key), true);
              // eslint-disable-next-line react-hooks/refs -- handler: the zoom reads its refs when it runs
              zoomToProgram(key);
            },
            free: {
              // eslint-disable-next-line react-hooks/refs -- handlers: the refs are read when they run, not during render
              onGrab: (e, grip) => grab(key, e, grip),
              // eslint-disable-next-line react-hooks/refs -- handler, see above
              onPin: (el) => pin(key, el),
              onSend: canSend ? () => send(key) : undefined,
            },
          },
        ]),
      ),
    [programs, change, leave, grab, pin, hasSettings, canSend, send, zoomToProgram],
  );

  // The tile switch (the client's settings, 2026-11-20: no longer one per window): on, every shown
  // window joins the tiles of the screen it stands on, new ones too; off, every window leaves the
  // tiles and floats where its tile was.
  const [tiling, setTiling] = useState(tileNew);
  useEffect(() => (overlay ? onTileNew(setTiling) : undefined), [overlay]);
  useEffect(() => {
    if (!overlay) return;
    const shown = visible(state);
    if (tiling) {
      const loose = shown.filter((k) => !tileOf(tileGroups, k));
      if (!loose.length) return;
      putTiles((all) =>
        loose.reduce((acc, k) => {
          const own = boxesNow.current[k];
          const at = own ? Math.max(0, screens.indexOf(screenOf(own, screens))) : 0;
          return tileOf(acc, k) ? acc : tile(acc, k, at);
        }, all),
      );
      return;
    }
    if (!Object.keys(tileGroups).length) return;
    // off: each tiled window keeps the place its tile had (read from the page, where it is drawn)
    const frac = (v: string) => parseFloat(v) / 100;
    const kept: Record<string, Box> = {};
    for (const g of Object.values(tileGroups)) {
      for (const k of g.keys) {
        const el = document.querySelector<HTMLElement>(`[data-program="${k}"]`);
        if (!el) continue;
        const here: Box = { x: frac(el.style.left), y: frac(el.style.top), w: frac(el.style.width), h: frac(el.style.height) };
        if ([here.x, here.y, here.w, here.h].every(Number.isFinite)) kept[k] = here;
      }
    }
    putTiles(() => ({}));
    if (Object.keys(kept).length) {
      setBoxes((b) => ({ ...b, ...kept }));
      saveBoxes({ ...boxesNow.current, ...kept });
    }
  }, [overlay, tiling, state, tileGroups, screens, putTiles]);

  // The order the windows lie in, the one in front last: a window brought forward goes on top
  // and the others keep their order beneath it (only the one in front was raised before, so
  // overlapping windows changed places on every click).
  const [stack, setStack] = useState<string[]>(() => (state.focus ? [state.focus] : []));
  const [stackTop, setStackTop] = useState(state.focus);
  if (stackTop !== state.focus) {
    // adjusted while drawing (React's pattern for state that follows other state)
    setStackTop(state.focus);
    const top = state.focus;
    if (top) setStack((s) => [...s.filter((k) => k !== top), top]);
  }

  const nestAspect = size.w > 0 && size.h > 0 ? (size.w * primary.w) / (size.h * primary.h) : 16 / 9;
  // a new program splits the place of the one that was in front before it (the stack's top that is
  // in the tree already)
  const inNest = keysOf(nest.tree);
  const nestTarget = [...stack].reverse().find((k) => inNest.includes(k)) ?? null;
  const nestTree = nesting
    ? syncNest(nest.tree, visible(state), { focus: nestTarget, aspect: nestAspect, zoom: nest.zoom })
    : nest.tree;
  // the program in front is always in view: brought forward from outside the frame, the view zooms
  // out just far enough
  // (the zoom path is made of indices: when the tree changed, it follows the programs it framed)
  const followed = nestTree === nest.tree ? nest.zoom : followZoom(nest.tree, nest.zoom, nestTree);
  const nestZoom = nesting && state.focus ? reveal(nestTree, followed, state.focus) : validZoom(nestTree, followed);
  if (nesting && (nestTree !== nest.tree || !sameZoom(nestZoom, nest.zoom))) {
    // adjusted while drawing, like the stack above: the next drawing has the tree it shows
    setNest({ tree: nestTree, zoom: nestZoom });
  }
  useLayoutEffect(() => {
    const from = nestFrom.current;
    nestFrom.current = null;
    const el = stage.current;
    if (!from || !el || !nestMs.current) return;
    for (const w of el.querySelectorAll<HTMLElement>(".dt-program")) {
      const was = from.get(w.dataset.program ?? "");
      const now = w.getBoundingClientRect();
      if (!was || !now.width || !now.height || typeof w.animate !== "function") continue;
      // the window's own scale (a program drawn smaller than its minimum) stays outside the move
      const s = parseFloat(w.style.getPropertyValue("scale")) || 1;
      const dx = (was.left - now.left) / s;
      const dy = (was.top - now.top) / s;
      w.animate([{ transform: `translate(${dx}px, ${dy}px) scale(${was.width / now.width}, ${was.height / now.height})` }], {
        duration: nestMs.current,
        easing: "ease-out",
      });
    }
  }, [nest.zoom]);
  const nestStep = useCallback(
    (go: "in" | "out" | "all") =>
      zoomNest((n) =>
        go === "all" ? [] : go === "out" ? zoomOut(n.tree, n.zoom) : state.focus ? zoomIn(n.tree, n.zoom, state.focus) : n.zoom,
      ),
    [zoomNest, state.focus],
  );

  // The keys of the desktop. Attached while the drawing is committed (layout effect), not after
  // it: a passive effect can run a moment after the windows are already on screen, and a key
  // pressed in that gap reached a listener that still knew the earlier state (no window in front
  // while loading, or the window that was in front before), so AltGr+C was lost (#73).
  useLayoutEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      const mod = e.ctrlKey || e.metaKey;
      if (mod && !e.altKey && (e.key === "k" || e.key === "K")) {
        e.preventDefault();
        setPalette((p) => !p);
        return;
      }
      if (palette) return;
      // AltGr+C (on Windows Ctrl+Alt): Clipper comes to the window in front, or leaves it
      if (e.ctrlKey && e.altKey && !e.metaKey && e.code === "KeyC" && state.focus) {
        e.preventDefault();
        toggleTalk(state.focus);
        return;
      }
      if (mod && e.key === "Enter" && state.focus) {
        e.preventDefault();
        focusChat(state.focus);
        return;
      }
      // the nest's levels: Alt+Down in towards the program in front, Alt+Up out, Alt+0 everything
      if (nesting && e.altKey && !mod && !e.shiftKey && !typing(e.target)) {
        const go = e.key === "ArrowDown" ? "in" : e.key === "ArrowUp" ? "out" : e.key === "0" ? "all" : null;
        if (go) {
          e.preventDefault();
          nestStep(go);
          return;
        }
      }
      if (e.altKey && !mod && /^[1-9]$/.test(e.key)) {
        const p = programs[Number(e.key) - 1];
        if (p) {
          e.preventDefault();
          change((s) => openProgram(s, p.key));
        }
        return;
      }
      // zoomed into the nest, Esc steps out one level before it minimises anything
      if (
        e.key === "Escape" &&
        nesting &&
        !overlay &&
        nestZoom.length > 0 &&
        !typing(e.target) &&
        !document.querySelector("[aria-modal='true']")
      ) {
        e.preventDefault();
        nestStep("out");
        return;
      }
      // in the overlay Esc hides the overlay (the client's key), it does not minimise
      if (
        e.key === "Escape" &&
        !overlay &&
        state.focus &&
        !typing(e.target) &&
        !document.querySelector("[aria-modal='true']")
      ) {
        const key = state.focus;
        change((s) => minimise(s, key));
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [palette, state.focus, programs, change, focusChat, overlay, toggleTalk, nesting, nestStep, nestZoom.length]);

  const entries = useMemo<PaletteEntry[]>(() => {
    const out: PaletteEntry[] = [];
    for (const p of programs) {
      const title = programTitle(p, t);
      out.push({
        id: `p-${p.key}`,
        label: title,
        hint: t("desktop.program"),
        run: () => change((s) => openProgram(s, p.key)),
      });
      for (const m of navViews(p, modules)) {
        out.push({
          id: `v-${p.key}-${m}`,
          label: viewTitle(m, t),
          hint: title,
          run: () => change((s) => setView(s, p.key, { module: m, props: {} })),
        });
        // the pages that are a tab of this view are found by their own name
        for (const [tab, home] of Object.entries(TAB_OF)) {
          if (home.module !== m || !(modules.get(tab)?.allowed ?? false)) continue;
          out.push({
            id: `v-${p.key}-${tab}`,
            label: viewTitle(tab, t),
            hint: title,
            run: () => change((s) => setView(s, p.key, viewFor(tab, {}))),
          });
        }
      }
    }
    // every option of Settings by its name: "temperatur" jumps to it (phase 4)
    if (hasSettings) {
      const settingsTitle = t("workspace.settings");
      const inClient = clientConfig() !== null;
      for (const e of SETTINGS) {
        if ((e.section === "client" && !inClient) || (e.id === "shell" && inClient)) continue;
        out.push({
          id: `s-${e.id}`,
          label: t(`settingsApp.item.${e.id}`),
          hint: `${settingsTitle} › ${t(`settingsApp.section.${e.section}`)}`,
          words: e.words,
          quiet: true,
          run: () =>
            change((s) => setView(s, "settings", { module: "page.settings", props: { section: e.section, focus: e.id } })),
        });
      }
    }
    for (const th of allThemes(ui.boot?.themes ?? [])) {
      out.push({
        id: `t-${th.key}`,
        label: th.name,
        hint: t("desktop.look"),
        run: () => void setPrefs({ look: th.key }),
      });
    }
    if (ctx.isAdmin)
      out.push({
        id: "a-add-node",
        label: t("header.addNode"),
        hint: t("desktop.action"),
        run: () => setAddNode(true),
      });
    // Clipper to the window in front (AltGr+C), for whoever does not know the key
    if (state.focus) {
      const front = state.focus;
      out.push({
        id: "a-clipper",
        label: t("clipper.dock.call"),
        hint: t("desktop.action"),
        words: "clipper chat fragen ask altgr",
        run: () => toggleTalk(front),
      });
    }
    if (nesting) {
      const words = "nest zoom ebene level gruppe group";
      for (const [id, go] of [["zoomIn", "in"], ["zoomOut", "out"], ["overview", "all"]] as const)
        // eslint-disable-next-line react-hooks/refs -- handler: the zoom reads its refs when it runs
        out.push({ id: `n-${id}`, label: t(`desktop.nest.${id}`), hint: t("desktop.action"), words, run: () => nestStep(go) });
    }
    out.push({
      id: "a-logout",
      label: t("header.logout"),
      hint: t("desktop.action"),
      run: onLogout,
    });
    return out;
  }, [programs, modules, ctx.isAdmin, t, change, onLogout, ui.boot, setPrefs, hasSettings, state.focus, toggleTalk, nesting, nestStep]);

  if (ui.status === "loading" && !ui.boot) return <ShellLoading />;
  if (!ui.boot) return <ShellFailure unreachable={ui.unreachable} detail={ui.error} onRetry={ui.reload} />;

  const shown = visible(state);
  // a narrow screen shows the one in front; a maximised window only grows (D: the others stay
  // where they are, beneath it)
  const only = narrow ? state.focus : null;
  const maxi = state.maximised && shown.includes(state.maximised) ? state.maximised : null;
  // the nest draws every shown program (too small ones scaled), so none waits for room
  const tiles = overlay || nesting ? shown : inTiles(shown, state.focus, fits(size.w, size.h));
  const drawn = only
    ? shown.filter((k) => k === only)
    : // free windows lie over each other, none waits for room; tiles keep their place under a
      // maximised window, so it goes back there when it is restored
      maxi && !tiles.includes(maxi)
      ? [...tiles, maxi]
      : tiles;
  const hue = prefs?.hue ?? 140;
  const customThemes = ui.boot.themes ?? [];
  const themes = allThemes(customThemes);
  const theme = themeNow;
  const reduced = window.matchMedia?.("(prefers-reduced-motion: reduce)")?.matches === true;
  // eslint-disable-next-line react-hooks/refs -- the timing of the closing animation is read in event handlers; it follows the theme of this render
  closeMs.current = leaveMs(theme, "close", reduced);
  // eslint-disable-next-line react-hooks/refs -- as above
  overlayMs.current = overlay ? leaveMs(theme, "overlay", reduced) : 0;
  // eslint-disable-next-line react-hooks/refs -- as above: the zoom's length, read when it plays
  nestMs.current = reduced ? 0 : Math.min(Math.max(theme.motion.ms, 0), 2000);
  const rects = layoutRects(theme.layout.kind, drawn.length);
  // the tiled windows' places: per screen, the shown windows of its group (dwindle.ts), with a
  // small gap between them
  const placed = new Map<string, { box: Box; info: TileInfo }>();
  // the nest's places (fractions of the stage): the framed part fills the stage (in the overlay the
  // primary screen), the rest lies outside it; measured in pixels, so a program below its minimum
  // size is scaled rather than squeezed
  const nestPlaced = new Map<string, Placed>();
  if (nesting && !only) {
    const measured = size.w > 0 && size.h > 0;
    const [uw, uh] = measured ? [size.w, size.h] : [1, 1];
    const area = { x: primary.x * uw, y: primary.y * uh, w: primary.w * uw, h: primary.h * uh };
    for (const p of nestRects(nestTree, area, nestZoom, measured ? MIN_TILE : { w: 0, h: 0 }))
      nestPlaced.set(p.key, { ...p, rect: { x: p.rect.x / uw, y: p.rect.y / uh, w: p.rect.w / uw, h: p.rect.h / uh } });
  }
  if (overlay && !nesting) {
    const aspect = size.w > 0 && size.h > 0 ? size.w / size.h : 16 / 9;
    const gx = size.w > 0 ? 3 / size.w : 0.002;
    const gy = size.h > 0 ? 3 / size.h : 0.003;
    for (const [sk, g] of Object.entries(tileGroups)) {
      const scr = screens[Number(sk)];
      if (!scr) continue;
      const keys = g.keys.filter((k) => drawn.includes(k) && k !== maxi);
      const lay = dwindle(keys.length, scr, keys.map((k) => g.ratios[g.keys.indexOf(k)] ?? 0.5), aspect);
      keys.forEach((k, i) => {
        const rect = lay.rects[i];
        if (!rect) return;
        const split = lay.areas[i];
        placed.set(k, {
          box: inset(rect, gx, gy),
          info: {
            screen: Number(sk),
            index: g.keys.indexOf(k),
            split: split ? (lay.vertical[i] ? "v" : "h") : null,
            area: split ?? null,
          },
        });
      });
    }
  }
  const css = safeCss(theme);

  const taskbar = solo ? null : (
    <Taskbar
          programs={programs}
          state={state}
          status={status}
          me={ctx.me}
          theme={theme}
          signals={signals}
          onProgram={(key) => {
            // a click on the program in front minimises it: that plays the leaving too
            const next = (s: DesktopState) => taskbarClick(s, key);
            const goes = next(state).minimised.includes(key) && !state.minimised.includes(key);
            if (goes) leave(key, next);
            else change(next);
          }}
          onPalette={() => setPalette(true)}
          start={startProgram ? { key: startProgram, open: () => change((s) => setView(s, startProgram, { module: "page.home", props: {} })) } : undefined}
          onSettings={
            hasSettings ? () => change((s) => setView(s, "settings", { module: "page.settings", props: {} })) : undefined
          }
          onSaveLanguage={onSaveLanguage}
          onLogout={onLogout}
        />
  );
  const look: LookControls = {
    theme,
    themes,
    hue,
    onTheme: (l) => void setPrefs({ look: l }),
    onHue: (h) => void setPrefs({ hue: h }, { lazy: true }),
    onSaveLanguage,
  };
  const banner = ui.error ? (
    <p className="bad dt-banner" role="alert">
      {ui.error}
    </p>
  ) : null;

  return (
    <LookContext.Provider value={look}>
    <div
      className={`dt-root dt-look-${theme.base ?? "terminal"} dt-theme-${theme.key} dt-bar-${theme.bar.position}${solo ? " dt-solo" : ""}${overlay ? " dt-overlay" : ""}${overlayMark ? ` dt-${overlayMark}` : ""}`}
      data-motion={theme.motion.style}
      data-close={theme.motion.close ?? "none"}
      data-overlay-motion={theme.motion.overlay ?? "none"}
      style={{
        ["--ws-hue" as string]: String(hue),
        ["--ws-sat" as string]: "70%",
        ["--dt-ms" as string]: `${theme.motion.ms}ms`,
      }}
    >
      {css && <style data-testid="theme-css">{css}</style>}
      {!overlay && banner}
      {!overlay && <PersonaPrompt api={api} quiet />}
      <main
        className="dt-stage"
        ref={stage}
        style={ownScreenOnly ? barRoom : undefined}
      >
        {drawn.map((key, i) => {
          const program = programs.find((p) => p.key === key);
          const info = infos.find((p) => p.key === key);
          const own = overlay ? (boxes[key] ?? firstBox(i, primary)) : null;
          const big = key === maxi && !only;
          // the overlay maximises a program on the screen it stands on, not over all of them
          const tiledHere = placed.get(key);
          const nestHere = big ? undefined : nestPlaced.get(key);
          const r = big ? (own ? screenOf(own, screens) : WHOLE) : (nestHere?.rect ?? tiledHere?.box ?? own ?? rects[i]);
          if (!program || !info || !r) return null;
          // a program smaller than its minimum is laid out at that size and drawn scaled down
          const scale = nestHere ? Math.max(0.001, Math.round(nestHere.scale * 1000) / 1000) : 1;
          const style: CSSProperties = {
            left: pct(r.x),
            top: pct(r.y),
            width: pct(r.w / scale),
            height: pct(r.h / scale),
            ...(nesting ? { transformOrigin: "0 0", scale: scale < 1 ? String(scale) : undefined } : {}),
            // the order they lie in; a maximised window lies above the rest
            zIndex: 1 + Math.max(0, stack.indexOf(key)) + (big ? stack.length + 1 : 0),
          };
          const h = handlers.get(key);
          if (!h) return null;
          const view = viewOf(state, info);
          const talk = talks[key];
          // the folded-out talk takes the right part of the window's frame, below its title bar
          if (talk?.open) (style as Record<string, string | number>)["--talk-share"] = TALK_SHARE;
          return (
            <Fragment key={key}>
            <ProgramWindow
              program={program}
              view={view}
              focused={state.focus === key}
              maximised={big}
              closing={closing.has(key)}
              modules={modules}
              ctx={ctx}
              boot={ui.boot}
              chrome={theme.chrome}
              style={style}
              onFocus={h.onFocus}
              onView={h.onView}
              onMinimise={h.onMinimise}
              onMaximise={h.onMaximise}
              onClose={h.onClose}
              onSettings={h.onSettings}
              free={overlay && !nesting ? h.free : undefined}
              scaled={scale < 1}
              unframed={nestHere ? !nestHere.framed : false}
              onZoom={h.onZoom}
              tile={tiledHere?.info}
            />
            {talk && (
              <ClipperDock
                api={api}
                program={key}
                programTitle={programTitle(program, t)}
                module={view.module}
                props={view.props}
                viewTitle={viewTitle(view.module, t)}
                expanded={talk.open}
                // no room above the title bar at the screen's top edge (maximised, upper tiles)
                atTop={r.y < 0.08}
                conversationId={talk.conversationId}
                onConversation={(id) => changeTalk(key, { conversationId: id })}
                onExpand={(open) => changeTalk(key, { open })}
                style={{
                  ...talkPlace(r, talk.open),
                  zIndex: Number(style.zIndex) + 1,
                  ["--pg-hue" as string]: String(programHue(key)),
                }}
                onClose={() => toggleTalk(key)}
              />
            )}
            </Fragment>
          );
        })}
        {snapPreview && (
          <div
            className="dt-snap-preview"
            aria-hidden="true"
            style={{ left: pct(snapPreview.x), top: pct(snapPreview.y), width: pct(snapPreview.w), height: pct(snapPreview.h) }}
          />
        )}
        {drawn.length === 0 && (
          <div className="dt-empty">
            <p>{t("desktop.empty")}</p>
            <button type="button" className="primary" onClick={() => setPalette(true)}>
              {t("desktop.palette")}
            </button>
          </div>
        )}
      </main>
      {overlay ? (
        <div
          className="dt-screen"
          ref={screenArea}
          style={{ left: pct(primary.x), top: pct(primary.y), width: pct(primary.w), height: pct(primary.h) }}
        >
          {banner}
          <PersonaPrompt api={api} quiet />
          {taskbar}
        </div>
      ) : (
        taskbar
      )}
      <FrameMeter />
      {palette && <CommandPalette entries={entries} onClose={() => setPalette(false)} />}
      {addNode && <AddNodeDialog api={api} onClose={() => setAddNode(false)} />}
    </div>
    </LookContext.Provider>
  );
}
