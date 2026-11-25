/** Free windows of the overlay (`?overlay=1`): each program has a box of its own that the person
 *  moves by its title bar and sizes at its edges. Boxes are fractions of the stage, so they keep
 *  their place when the screen changes; they live in this web view's storage (the overlay's own
 *  arrangement, not the browser desktop's). */
import { overlayStoreKey } from "./state";

/** The room the taskbar takes from a side of the overlay's stage, in px (2026-11-20: the windows
 *  reached under the bar and Clipper at their foot was cut off). The bar's side is the one it is
 *  nearest to; a bar that is hidden or not there takes nothing. */
export function barInset(
  bar: { left: number; top: number; right: number; bottom: number; width: number; height: number } | null,
  root: { left: number; top: number; right: number; bottom: number; width: number; height: number },
): { top: number; right: number; bottom: number; left: number } {
  const none = { top: 0, right: 0, bottom: 0, left: 0 };
  if (!bar || bar.width <= 0 || bar.height <= 0) return none;
  if (bar.width >= bar.height) {
    // across the screen: at its top or its bottom
    return bar.top - root.top < root.bottom - bar.bottom
      ? { ...none, top: Math.max(0, bar.bottom - root.top) }
      : { ...none, bottom: Math.max(0, root.bottom - bar.top) };
  }
  return bar.left - root.left < root.right - bar.right
    ? { ...none, left: Math.max(0, bar.right - root.left) }
    : { ...none, right: Math.max(0, root.right - bar.left) };
}

export interface Box {
  x: number;
  y: number;
  w: number;
  h: number;
}

/** Where a pointer took hold: the title bar (move) or an edge or corner (size). */
export type Grip = "move" | "n" | "s" | "e" | "w" | "ne" | "nw" | "se" | "sw";

export const GRIPS: readonly Exclude<Grip, "move">[] = ["n", "s", "e", "w", "ne", "nw", "se", "sw"];

/** The smallest box, as a fraction of one screen (the overlay scales it to its primary screen). */
export const MIN = { w: 0.15, h: 0.15 };

const KEY = "topcoat.overlay.boxes";

const clamp = (n: number, lo: number, hi: number) => Math.min(hi, Math.max(lo, n));

/** The whole stage: one screen. */
export const WHOLE: Box = { x: 0, y: 0, w: 1, h: 1 };

/** A box for a program that has none yet: cascaded from the top left of `area` (the primary
 *  screen), one step per program. */
export function firstBox(i: number, area: Box = WHOLE): Box {
  const step = (i % 8) * 0.035;
  return {
    x: area.x + (0.08 + step) * area.w,
    y: area.y + (0.06 + step) * area.h,
    w: 0.56 * area.w,
    h: 0.72 * area.h,
  };
}

/** The screens the overlay spans (`screens=x,y,w,h;...`, fractions of the window, the primary one
 *  first; written by the desktop client). Without them the window is one screen. */
export function screensOf(search: string): Box[] {
  const raw = new URLSearchParams(search).get("screens");
  if (!raw || raw.length > 2000) return [WHOLE];
  const out: Box[] = [];
  for (const part of raw.split(";")) {
    const n = part.split(",").map(Number);
    if (n.length !== 4 || !n.every((v) => Number.isFinite(v))) return [WHOLE];
    const [x, y, w, h] = n as [number, number, number, number];
    if (x < 0 || y < 0 || w <= 0 || h <= 0 || x + w > 1.001 || y + h > 1.001) return [WHOLE];
    out.push({ x, y, w, h });
  }
  return out.length > 0 && out.length <= 16 ? out : [WHOLE];
}

const inside = (s: Box, px: number, py: number) => px >= s.x && px <= s.x + s.w && py >= s.y && py <= s.y + s.h;

function distance(s: Box, px: number, py: number): number {
  const dx = Math.max(s.x - px, 0, px - (s.x + s.w));
  const dy = Math.max(s.y - py, 0, py - (s.y + s.h));
  return dx * dx + dy * dy;
}

/** The point a window is held by: the middle of its title bar. */
const holdPoint = (b: Box): [number, number] => [b.x + b.w / 2, b.y + Math.min(0.01, b.h / 2)];

/** The screen a window is on (the one under its title bar, else the nearest one). */
export function screenOf(b: Box, screens: readonly Box[]): Box {
  const [px, py] = holdPoint(b);
  let best = screens[0] ?? WHOLE;
  for (const s of screens) {
    if (inside(s, px, py)) return s;
    if (distance(s, px, py) < distance(best, px, py)) best = s;
  }
  return best;
}

/** A window whose title bar is on no screen (between screens of different sizes, where nobody
 *  sees it) goes onto the nearest one, whole if it fits. */
export function onScreens(b: Box, screens: readonly Box[]): Box {
  const [px, py] = holdPoint(b);
  if (screens.some((s) => inside(s, px, py))) return b;
  const s = screenOf(b, screens);
  const w = Math.min(b.w, s.w);
  const h = Math.min(b.h, s.h);
  return { x: clamp(b.x, s.x, s.x + s.w - w), y: clamp(b.y, s.y, s.y + s.h - h), w, h };
}

/** The box moved by (dx, dy), kept inside the stage. */
export function moveBox(b: Box, dx: number, dy: number): Box {
  return { ...b, x: clamp(b.x + dx, 0, 1 - b.w), y: clamp(b.y + dy, 0, 1 - b.h) };
}

/** The box with the edges of `grip` dragged by (dx, dy): never smaller than MIN, never out of the
 *  stage; the opposite edge stays where it is. */
export function sizeBox(b: Box, grip: Grip, dx: number, dy: number, min: { w: number; h: number } = MIN): Box {
  if (grip === "move") return moveBox(b, dx, dy);
  let { x, y, w, h } = b;
  if (grip.includes("e")) w = clamp(w + dx, min.w, 1 - x);
  if (grip.includes("s")) h = clamp(h + dy, min.h, 1 - y);
  if (grip.includes("w")) {
    const right = x + w;
    x = clamp(x + dx, 0, right - min.w);
    w = right - x;
  }
  if (grip.includes("n")) {
    const bottom = y + h;
    y = clamp(y + dy, 0, bottom - min.h);
    h = bottom - y;
  }
  return { x, y, w, h };
}

function isBox(v: unknown): v is Box {
  if (!v || typeof v !== "object") return false;
  const b = v as Record<string, unknown>;
  return ["x", "y", "w", "h"].every((k) => typeof b[k] === "number" && Number.isFinite(b[k]));
}

/** The boxes kept in this web view, cleaned of anything that is not a box. */
export function loadBoxes(): Record<string, Box> {
  try {
    const raw: unknown = JSON.parse(window.localStorage.getItem(overlayStoreKey(KEY, window.location.search)) ?? "{}");
    const out: Record<string, Box> = {};
    if (raw && typeof raw === "object") {
      for (const [k, v] of Object.entries(raw)) {
        if (!isBox(v)) continue;
        // a box from another screen size: not vanishing, no larger than the stage, inside it
        const fit = { ...v, w: clamp(v.w, 0.02, 1), h: clamp(v.h, 0.02, 1) };
        out[k] = moveBox(fit, 0, 0);
      }
    }
    return out;
  } catch {
    return {};
  }
}

export function saveBoxes(boxes: Record<string, Box>): void {
  try {
    window.localStorage.setItem(overlayStoreKey(KEY, window.location.search), JSON.stringify(boxes));
  } catch {
    /* a web view without storage: the boxes last until the overlay is rebuilt */
  }
}

/** The address that pins a program to the normal desktop: the client sees the overlay's page go
 *  to the program's solo address and opens a native window there instead, at `rect` (CSS pixels
 *  of the overlay page); the page itself stays. */
export function pinHref(soloHref: string, rect: { left: number; top: number; width: number; height: number }): string {
  const r = [rect.left, rect.top, rect.width, rect.height].map((n) => Math.round(n)).join(",");
  const [query, hash] = soloHref.split("#");
  return `${query}&pin=${r}${hash !== undefined ? `#${hash}` : ""}`;
}

// ---------- snapping (2026-11-12) ----------
// A window that is moved or sized lands on a screen edge or on another window's edge when it comes
// close (`snap*`), and a window dragged to a screen's edge takes a half, a quarter or the whole
// screen (`zoneAt`, like Windows Snap). All in fractions of the stage; the thresholds are pixels
// converted by the caller (tx, ty).

/** The lines a window's edges may land on: the screens' edges and the other windows' edges. */
export interface SnapLines {
  xs: number[];
  ys: number[];
}

export function snapLines(others: readonly Box[], screens: readonly Box[]): SnapLines {
  const xs: number[] = [];
  const ys: number[] = [];
  for (const b of [...screens, ...others]) {
    xs.push(b.x, b.x + b.w);
    ys.push(b.y, b.y + b.h);
  }
  return { xs, ys };
}

/** The shift that brings one of `edges` onto the nearest line within `t`, or 0. */
function pull(edges: number[], lines: number[], t: number): number {
  let best = 0;
  let dist = t;
  for (const e of edges) {
    for (const l of lines) {
      const d = Math.abs(l - e);
      if (d < dist) {
        dist = d;
        best = l - e;
      }
    }
  }
  return best;
}

/** A moved box, shifted so its nearest edge lies on a line when one is within the threshold. */
export function snapMove(b: Box, lines: SnapLines, tx: number, ty: number): Box {
  const dx = pull([b.x, b.x + b.w], lines.xs, tx);
  const dy = pull([b.y, b.y + b.h], lines.ys, ty);
  return moveBox(b, dx, dy);
}

/** A sized box whose moving edges land on a line within the threshold (never below `min`). */
export function snapSize(b: Box, grip: Grip, lines: SnapLines, tx: number, ty: number, min: { w: number; h: number } = MIN): Box {
  if (grip === "move") return snapMove(b, lines, tx, ty);
  let { x, y, w, h } = b;
  if (grip.includes("e")) w = Math.max(min.w, w + pull([x + w], lines.xs, tx));
  if (grip.includes("s")) h = Math.max(min.h, h + pull([y + h], lines.ys, ty));
  if (grip.includes("w")) {
    const right = x + w;
    x = Math.min(right - min.w, x + pull([x], lines.xs, tx));
    w = right - x;
  }
  if (grip.includes("n")) {
    const bottom = y + h;
    y = Math.min(bottom - min.h, y + pull([y], lines.ys, ty));
    h = bottom - y;
  }
  return { x, y, w, h };
}

export type Zone = "left" | "right" | "top" | "tl" | "tr" | "bl" | "br";

/** The part of a screen a zone stands for: a half, a quarter, or the whole (top). */
export function zoneBox(zone: Zone, s: Box): Box {
  const hw = s.w / 2;
  const hh = s.h / 2;
  switch (zone) {
    case "left":
      return { x: s.x, y: s.y, w: hw, h: s.h };
    case "right":
      return { x: s.x + hw, y: s.y, w: hw, h: s.h };
    case "top":
      return { ...s };
    case "tl":
      return { x: s.x, y: s.y, w: hw, h: hh };
    case "tr":
      return { x: s.x + hw, y: s.y, w: hw, h: hh };
    case "bl":
      return { x: s.x, y: s.y + hh, w: hw, h: hh };
    case "br":
      return { x: s.x + hw, y: s.y + hh, w: hw, h: hh };
  }
}

/** The zone the pointer (px, py) is in: within `ex`/`ey` of a screen's edge, corners first. */
export function zoneAt(px: number, py: number, screens: readonly Box[], ex: number, ey: number): { zone: Zone; box: Box } | null {
  const s = screens.find((sc) => inside(sc, px, py));
  if (!s) return null;
  const left = px - s.x < ex;
  const right = s.x + s.w - px < ex;
  const top = py - s.y < ey;
  const bottom = s.y + s.h - py < ey;
  // a corner is a little larger than the edge strip, so it can be hit
  const cx = ex * 4;
  const cy = ey * 4;
  const nearLeft = px - s.x < cx;
  const nearRight = s.x + s.w - px < cx;
  const nearTop = py - s.y < cy;
  const nearBottom = s.y + s.h - py < cy;
  let zone: Zone | null = null;
  if ((left && nearTop) || (top && nearLeft)) zone = "tl";
  else if ((right && nearTop) || (top && nearRight)) zone = "tr";
  else if ((left && nearBottom) || (bottom && nearLeft)) zone = "bl";
  else if ((right && nearBottom) || (bottom && nearRight)) zone = "br";
  else if (top) zone = "top";
  else if (left) zone = "left";
  else if (right) zone = "right";
  return zone ? { zone, box: zoneBox(zone, s) } : null;
}
