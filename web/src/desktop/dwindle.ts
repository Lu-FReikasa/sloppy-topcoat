/** Auto-tiling of the overlay (the desktop plan, phase 3): windows with their tile switch on share
 *  their screen. Each screen has a group; the first window takes a part of the screen along its
 *  longer side, the next one a part of what is left, and so on ("dwindle"): a window that joins or
 *  leaves rearranges the others at once. A divider is moved by dragging the edge of the window
 *  before it (its ratio). Kept per machine in this web view's storage, like the free boxes. */
import type { Box } from "./floating";
import { overlayStoreKey } from "./state";

export interface TileGroup {
  /** The windows in their order on the screen. */
  keys: string[];
  /** The split ratio after each window (0.5 when none was dragged). */
  ratios: number[];
}

/** Tile groups by the index of their screen (the primary screen is 0). */
export type Tiles = Record<number, TileGroup>;

const KEY = "topcoat.overlay.tiles";
export const RATIO_MIN = 0.15;
export const RATIO_MAX = 0.85;

export interface Layout {
  /** The window's place, before the gap. */
  rects: Box[];
  /** The area that was split at each step (to turn a dragged divider into a ratio). */
  areas: Box[];
  /** Was step i split side by side (true) or above each other (false)? */
  vertical: boolean[];
}

/** Places `n` windows in `area`. `aspect` is the stage's width over its height in pixels, so
 *  "longer side" means longer on the screen, not in fractions. */
export function dwindle(n: number, area: Box, ratios: readonly number[], aspect: number): Layout {
  const out: Layout = { rects: [], areas: [], vertical: [] };
  let rest = { ...area };
  for (let i = 0; i < n; i += 1) {
    if (i === n - 1) {
      out.rects.push(rest);
      break;
    }
    const r = Math.min(RATIO_MAX, Math.max(RATIO_MIN, ratios[i] ?? 0.5));
    const vertical = rest.w * aspect >= rest.h;
    out.areas.push(rest);
    out.vertical.push(vertical);
    if (vertical) {
      out.rects.push({ x: rest.x, y: rest.y, w: rest.w * r, h: rest.h });
      rest = { x: rest.x + rest.w * r, y: rest.y, w: rest.w * (1 - r), h: rest.h };
    } else {
      out.rects.push({ x: rest.x, y: rest.y, w: rest.w, h: rest.h * r });
      rest = { x: rest.x, y: rest.y + rest.h * r, w: rest.w, h: rest.h * (1 - r) };
    }
  }
  return out;
}

/** A rect with a gap all round (gx, gy: half the gap between two windows, in fractions). */
export function inset(b: Box, gx: number, gy: number): Box {
  return { x: b.x + gx, y: b.y + gy, w: Math.max(0, b.w - 2 * gx), h: Math.max(0, b.h - 2 * gy) };
}

/** The ratio a divider gets when it is dragged to the pointer (px, py). */
export function ratioAt(area: Box, vertical: boolean, px: number, py: number): number {
  const r = vertical ? (px - area.x) / area.w : (py - area.y) / area.h;
  return Math.min(RATIO_MAX, Math.max(RATIO_MIN, r));
}

/** Where a window is tiled: its screen and its place there, or null. */
export function tileOf(tiles: Tiles, key: string): { screen: number; index: number } | null {
  for (const [s, g] of Object.entries(tiles)) {
    const index = g.keys.indexOf(key);
    if (index >= 0) return { screen: Number(s), index };
  }
  return null;
}

/** The window leaves its group (its ratio with it). */
export function untile(tiles: Tiles, key: string): Tiles {
  const at = tileOf(tiles, key);
  if (!at) return tiles;
  const g = tiles[at.screen] as TileGroup;
  const keys = g.keys.filter((k) => k !== key);
  const ratios = g.ratios.filter((_, i) => i !== at.index);
  const next = { ...tiles };
  if (keys.length === 0) delete next[at.screen];
  else next[at.screen] = { keys, ratios };
  return next;
}

/** The window joins the group of `screen` at `index` (the end when left out); from any group it
 *  was in before. */
export function tile(tiles: Tiles, key: string, screen: number, index?: number): Tiles {
  const without = untile(tiles, key);
  const g = without[screen] ?? { keys: [], ratios: [] };
  const at = Math.max(0, Math.min(index ?? g.keys.length, g.keys.length));
  const keys = [...g.keys.slice(0, at), key, ...g.keys.slice(at)];
  const ratios = [...g.ratios.slice(0, at), 0.5, ...g.ratios.slice(at)];
  return { ...without, [screen]: { keys, ratios } };
}

/** Two windows change places (in one group or across screens). */
export function swap(tiles: Tiles, a: string, b: string): Tiles {
  const pa = tileOf(tiles, a);
  const pb = tileOf(tiles, b);
  if (!pa || !pb || a === b) return tiles;
  const next: Tiles = {};
  for (const [s, g] of Object.entries(tiles)) {
    next[Number(s)] = { keys: g.keys.map((k) => (k === a ? b : k === b ? a : k)), ratios: [...g.ratios] };
  }
  return next;
}

/** The ratio after window `index` of `screen`. */
export function setRatio(tiles: Tiles, screen: number, index: number, ratio: number): Tiles {
  const g = tiles[screen];
  if (!g || index < 0 || index >= g.keys.length - 1) return tiles;
  const ratios = [...g.ratios];
  ratios[index] = Math.min(RATIO_MAX, Math.max(RATIO_MIN, ratio));
  return { ...tiles, [screen]: { ...g, ratios } };
}

function isGroup(v: unknown): v is TileGroup {
  if (!v || typeof v !== "object") return false;
  const g = v as Record<string, unknown>;
  return (
    Array.isArray(g.keys) &&
    g.keys.every((k) => typeof k === "string") &&
    Array.isArray(g.ratios) &&
    g.ratios.every((r) => typeof r === "number" && Number.isFinite(r))
  );
}

export function loadTiles(): Tiles {
  try {
    const raw: unknown = JSON.parse(window.localStorage.getItem(overlayStoreKey(KEY, window.location.search)) ?? "{}");
    const out: Tiles = {};
    if (raw && typeof raw === "object") {
      for (const [s, g] of Object.entries(raw)) {
        if (/^\d+$/.test(s) && isGroup(g) && g.keys.length > 0) out[Number(s)] = g;
      }
    }
    return out;
  } catch {
    return {};
  }
}

export function saveTiles(tiles: Tiles): void {
  try {
    window.localStorage.setItem(overlayStoreKey(KEY, window.location.search), JSON.stringify(tiles));
  } catch {
    /* no storage: the tiles last as long as the page */
  }
}

const NEW_KEY = "topcoat.overlay.tileNew";
const TILE_EVENT = "topcoat:tile-mode";

/** Are the overlay's windows tiled? (the person's switch in the client's settings, per machine;
 *  the key kept its old name, from when it only covered new windows) */
export function tileNew(): boolean {
  try {
    return window.localStorage.getItem(NEW_KEY) === "1";
  } catch {
    return false;
  }
}

export function setTileNew(on: boolean): void {
  try {
    window.localStorage.setItem(NEW_KEY, on ? "1" : "0");
  } catch {
    /* no storage */
  }
  window.dispatchEvent(new Event(TILE_EVENT));
}

/** Calls `fn` when the switch changes: in this page, or in another screen's page (storage event). */
export function onTileNew(fn: (on: boolean) => void): () => void {
  const here = () => fn(tileNew());
  const there = (e: StorageEvent) => {
    if (e.key === NEW_KEY) fn(tileNew());
  };
  window.addEventListener(TILE_EVENT, here);
  window.addEventListener("storage", there);
  return () => {
    window.removeEventListener(TILE_EVENT, here);
    window.removeEventListener("storage", there);
  };
}
