/** The `nest` layout (#12, #13): programs in splits that nest to any depth, and a view that zooms to
 *  one part of them while the rest keeps running. Each split is a level; the zoom path names the
 *  part that is framed (the whole tree when it is empty). This file is the model only: the tree,
 *  what can be done to it, where each program stands at a zoom, and how it is kept. No drawing.
 *
 *  The idea comes from Trellis (github.com/DanFessler/trellis); this is an independent
 *  implementation of the behaviour, no code of it (its licence does not fit the AGPL, see #12). */
import type { Box } from "./floating";
import { overlayStoreKey } from "./state";
import { MIN_TILE } from "./tiling";

/** `x`: the children stand side by side; `y`: above each other. */
export type Axis = "x" | "y";

export interface NestLeaf {
  key: string;
}

export interface NestSplit {
  axis: Axis;
  children: NestNode[];
  /** One per child, positive; a child's share is its weight over the sum. */
  weights: number[];
}

export type NestNode = NestLeaf | NestSplit;

/** A tree, or null when no program is in it. */
export type NestTree = NestNode | null;

/** Child indices from the root to the framed node: [] frames the whole tree (the overview). */
export type ZoomPath = number[];

/** Where the programs of `side` go next to a target. */
export type Side = "left" | "right" | "top" | "bottom";

export const isSplit = (n: NestNode): n is NestSplit => "children" in n;

/** The smallest share a child keeps when weights are changed. */
export const WEIGHT_MIN = 0.05;

/** The aspect of a stage that was not measured yet. */
const WIDE = 16 / 9;
const HALF = 0.5;

// ---------- reading a tree ----------

/** The programs of the tree in their order (left to right, top to bottom). */
export function keysOf(tree: NestTree): string[] {
  if (!tree) return [];
  if (!isSplit(tree)) return [tree.key];
  return tree.children.flatMap(keysOf);
}

/** The path to a program's leaf, or null. */
export function pathOf(tree: NestTree, key: string): ZoomPath | null {
  if (!tree) return null;
  if (!isSplit(tree)) return tree.key === key ? [] : null;
  return tree.children.reduce<ZoomPath | null>((found, c, i) => {
    if (found) return found;
    const p = pathOf(c, key);
    return p ? [i, ...p] : null;
  }, null);
}

/** The node at a path, or null when the path leads nowhere. */
export function nodeAt(tree: NestTree, path: readonly number[]): NestNode | null {
  return path.reduce<NestNode | null>((n, i) => (n && isSplit(n) ? (n.children[i] ?? null) : null), tree);
}

// ---------- normalising ----------

const goodWeight = (w: unknown): w is number => typeof w === "number" && Number.isFinite(w) && w > 0;

/** The tree in its plain form: a split with one child is that child, a split without children is
 *  gone, a split directly inside a split on the same axis gives its children to the outer one
 *  (their shares stay as they were drawn), and the weights of a split sum to 1. */
export function normalise(tree: NestTree): NestTree {
  if (!tree) return null;
  if (!isSplit(tree)) return { key: tree.key };
  const children: NestNode[] = [];
  const weights: number[] = [];
  const own = tree.children.map((_, i) => (goodWeight(tree.weights[i]) ? (tree.weights[i] as number) : 1));
  const sum = own.reduce((a, b) => a + b, 0);
  tree.children.forEach((c, i) => {
    const n = normalise(c);
    if (!n) return;
    const share = (own[i] as number) / sum;
    if (isSplit(n) && n.axis === tree.axis) {
      n.children.forEach((cc, j) => {
        children.push(cc);
        weights.push(share * (n.weights[j] as number));
      });
    } else {
      children.push(n);
      weights.push(share);
    }
  });
  if (children.length === 0) return null;
  if (children.length === 1) return children[0] as NestNode;
  const total = weights.reduce((a, b) => a + b, 0);
  return { axis: tree.axis, children, weights: weights.map((w) => w / total) };
}

// ---------- where things stand ----------

export interface Placed {
  key: string;
  /** Where the program is drawn, in the unit of the area. Outside the area when the program lies
   *  outside the framed part. */
  rect: Box;
  /** 1, or less when the rect is smaller than the program's minimum size: the program is then
   *  laid out at `rect / scale` and drawn scaled down by `scale`, not squeezed. */
  scale: number;
  /** Is the program inside the framed part (on screen)? */
  framed: boolean;
}

function layout(node: NestNode, rect: Box, path: number[], out: Map<string, { rect: Box; path: number[] }>): void {
  if (!isSplit(node)) {
    out.set(node.key, { rect, path });
    return;
  }
  const sum = node.weights.reduce((a, b) => a + b, 0) || 1;
  let at = 0;
  node.children.forEach((c, i) => {
    const share = (node.weights[i] ?? 0) / sum;
    const r =
      node.axis === "x"
        ? { x: rect.x + rect.w * at, y: rect.y, w: rect.w * share, h: rect.h }
        : { x: rect.x, y: rect.y + rect.h * at, w: rect.w, h: rect.h * share };
    at += share;
    layout(c, r, [...path, i], out);
  });
}

/** The longest part of `path` that leads to a node of the tree. */
export function validZoom(tree: NestTree, zoom: readonly number[]): ZoomPath {
  const out: number[] = [];
  let n: NestNode | null = tree;
  for (const i of zoom) {
    if (!n || !isSplit(n) || !Number.isInteger(i) || i < 0 || i >= n.children.length) break;
    out.push(i);
    n = n.children[i] as NestNode;
  }
  return out;
}

/** The rect of the framed node in the overview of `area` (the whole tree laid out in it). */
function frameRect(tree: NestNode, area: Box, zoom: ZoomPath): Box {
  return zoom.reduce(
    ({ rect, n }, i) => {
      if (!isSplit(n)) return { rect, n };
      const sum = n.weights.reduce((a, b) => a + b, 0) || 1;
      const before = n.weights.slice(0, i).reduce((a, b) => a + b, 0) / sum;
      const share = (n.weights[i] ?? 0) / sum;
      const next =
        n.axis === "x"
          ? { x: rect.x + rect.w * before, y: rect.y, w: rect.w * share, h: rect.h }
          : { x: rect.x, y: rect.y + rect.h * before, w: rect.w, h: rect.h * share };
      return { rect: next, n: n.children[i] as NestNode };
    },
    { rect: area, n: tree },
  ).rect;
}

/** Where each program stands when `zoom` is framed in `area` (pixels, or any unit `min` is given
 *  in). The view works like a camera: the framed node fills the area, and the rest of the tree lies
 *  around it, outside the area, where it was in the overview (so a zoom can be animated as one
 *  move, and nothing that leaves the frame has to stop). The order is the order of the tree. */
export function rects(
  tree: NestTree,
  area: Box,
  zoom: readonly number[] = [],
  min: { w: number; h: number } = MIN_TILE,
): Placed[] {
  if (!tree) return [];
  const z = validZoom(tree, zoom);
  const unit = { x: 0, y: 0, w: 1, h: 1 };
  const frame = frameRect(tree, unit, z);
  // the camera: the framed rect of the unit overview is stretched onto the area
  const sx = area.w / (frame.w || 1);
  const sy = area.h / (frame.h || 1);
  const placed = new Map<string, { rect: Box; path: number[] }>();
  layout(tree, unit, [], placed);
  return [...placed].map(([key, { rect: r, path }]) => {
    const rect = {
      x: area.x + (r.x - frame.x) * sx,
      y: area.y + (r.y - frame.y) * sy,
      w: r.w * sx,
      h: r.h * sy,
    };
    // a rect without size (a stage not measured yet) is not scaled: rect / scale must stay finite
    const scale =
      rect.w > 0 && rect.h > 0 ? Math.min(1, min.w > 0 ? rect.w / min.w : 1, min.h > 0 ? rect.h / min.h : 1) : 1;
    const framed = z.every((v, i) => path[i] === v);
    return { key, rect, scale, framed };
  });
}

// ---------- zoom ----------

/** One level deeper towards `key`: the group framed now, then the group inside it that holds the
 *  program, down to the program itself. A program outside the framed part frames its own group. */
export function zoomIn(tree: NestTree, zoom: readonly number[], key: string): ZoomPath {
  const z = validZoom(tree, zoom);
  const p = pathOf(tree, key);
  if (!p) return z;
  const inside = z.length <= p.length && z.every((v, i) => p[i] === v);
  if (!inside) return p.slice(0, Math.max(0, p.length - 1));
  return p.slice(0, Math.min(p.length, z.length + 1));
}

/** One level up. */
export function zoomOut(tree: NestTree, zoom: readonly number[]): ZoomPath {
  return validZoom(tree, zoom).slice(0, -1);
}

/** Everything at once. */
export function overview(): ZoomPath {
  return [];
}

/** The zoom after the tree changed from `before` to `after` (a program added, removed or moved,
 *  programs dropped): the paths of a tree shift when it changes, so the zoom follows the programs
 *  it framed. It frames the smallest part of `after` that holds all of them that are still there
 *  (one program alone when only one is left); when none are, it steps out in `before` until it
 *  frames some that are (the overview at worst). A program that was added is not framed by this
 *  unless it joined the framed part; whether to show it is the caller's choice (`groupOf`). */
export function followZoom(before: NestTree, zoom: readonly number[], after: NestTree): ZoomPath {
  const left = new Set(keysOf(after));
  let z = validZoom(before, zoom);
  for (;;) {
    const keys = keysOf(nodeAt(before, z)).filter((k) => left.has(k));
    if (keys.length > 0) {
      const paths = keys.map((k) => pathOf(after, k) as ZoomPath);
      const first = paths[0] as ZoomPath;
      let n = 0;
      while (n < first.length && paths.every((p) => p[n] === first[n])) n += 1;
      return first.slice(0, n);
    }
    if (z.length === 0) return [];
    z = z.slice(0, -1);
  }
}

/** The group a program stands in (its innermost split), or [] for a lone program. */
export function groupOf(tree: NestTree, key: string): ZoomPath {
  const p = pathOf(tree, key);
  return p ? p.slice(0, Math.max(0, p.length - 1)) : [];
}

// ---------- changing a tree ----------

function replaceAt(node: NestNode, path: readonly number[], fn: (n: NestNode) => NestNode | null): NestNode | null {
  if (path.length === 0) return fn(node);
  if (!isSplit(node)) return node;
  const [i, ...rest] = path as [number, ...number[]];
  const child = node.children[i];
  if (!child) return node;
  const next = replaceAt(child, rest, fn);
  const children = [...node.children];
  const weights = [...node.weights];
  if (next) children[i] = next;
  else {
    children.splice(i, 1);
    weights.splice(i, 1);
  }
  return { axis: node.axis, children, weights };
}

/** A program joins: the focused program's place is split along its longer side (as it is seen at
 *  `zoom` on an area of `aspect` width over height), the new one after it. Without a focused
 *  program in the tree, the last one is split. A program that is in the tree already stays. */
export function addProgram(
  tree: NestTree,
  key: string,
  opts: { focus?: string | null; aspect?: number; zoom?: readonly number[] } = {},
): NestTree {
  if (!tree) return { key };
  if (pathOf(tree, key)) return tree;
  const all = keysOf(tree);
  const target = opts.focus && all.includes(opts.focus) ? opts.focus : (all[all.length - 1] as string);
  const aspect = opts.aspect && opts.aspect > 0 ? opts.aspect : WIDE;
  const at = rects(tree, { x: 0, y: 0, w: aspect, h: 1 }, opts.zoom ?? [], { w: 0, h: 0 }).find(
    (p) => p.key === target,
  );
  const axis: Axis = at && at.rect.h > at.rect.w ? "y" : "x";
  const path = pathOf(tree, target) as ZoomPath;
  return normalise(
    replaceAt(tree, path, (leaf) => ({ axis, children: [leaf, { key }], weights: [1, 1] })),
  );
}

/** A program leaves; the others share its room. */
export function removeProgram(tree: NestTree, key: string): NestTree {
  const p = pathOf(tree, key);
  if (!tree || !p) return tree;
  return normalise(replaceAt(tree, p, () => null));
}

/** A program moves to the `side` of another one: into the target's split when that split runs
 *  the same way, else the target's place is split in two. */
export function moveProgram(tree: NestTree, key: string, target: string, side: Side): NestTree {
  if (key === target || !pathOf(tree, key) || !pathOf(tree, target)) return tree;
  const without = removeProgram(tree, key);
  const p = pathOf(without, target);
  if (!without || !p) return tree;
  const axis: Axis = side === "left" || side === "right" ? "x" : "y";
  const before = side === "left" || side === "top";
  const leaf: NestLeaf = { key };
  const parentPath = p.slice(0, -1);
  const parent = p.length > 0 ? nodeAt(without, parentPath) : null;
  if (parent && isSplit(parent) && parent.axis === axis) {
    const i = p[p.length - 1] as number;
    return normalise(
      replaceAt(without, parentPath, (n) => {
        const s = n as NestSplit;
        const half = (s.weights[i] as number) * HALF;
        const children = [...s.children];
        const weights = [...s.weights];
        weights[i] = half;
        const at = before ? i : i + 1;
        children.splice(at, 0, leaf);
        weights.splice(at, 0, half);
        return { axis, children, weights };
      }),
    );
  }
  return normalise(
    replaceAt(without, p, (n) => ({ axis, children: before ? [leaf, n] : [n, leaf], weights: [1, 1] })),
  );
}

/** New weights for the split at `path` (one per child; each share kept at WEIGHT_MIN or more). */
export function setWeights(tree: NestTree, path: readonly number[], weights: readonly number[]): NestTree {
  const n = nodeAt(tree, path);
  if (!tree || !n || !isSplit(n) || weights.length !== n.children.length || !weights.every(goodWeight)) return tree;
  return replaceAt(tree, path, () => ({ ...n, weights: shares(weights) }));
}

function shares(weights: readonly number[]): number[] {
  const sum = weights.reduce((a, b) => a + b, 0);
  const w = weights.map((v) => v / sum);
  // a split of more than 1 / WEIGHT_MIN children cannot give each the minimum: then equal shares at most
  const min = Math.min(WEIGHT_MIN, 1 / w.length);
  // the small ones are lifted to the minimum for good; the others share what is left, in proportion
  const lifted = new Set<number>();
  for (;;) {
    const free = w.map((_, i) => i).filter((i) => !lifted.has(i));
    const room = 1 - lifted.size * min;
    const freeSum = free.reduce((a, i) => a + (w[i] as number), 0);
    const low = free.filter((i) => ((w[i] as number) * room) / freeSum < min);
    if (low.length === 0 || low.length === free.length) {
      return w.map((v, i) => (lifted.has(i) || low.length === free.length ? min : (v * room) / freeSum));
    }
    low.forEach((i) => lifted.add(i));
  }
}

/** The divider after child `index` of the split at `path` moves to `at` (0..1 along the split):
 *  only the two children beside it change. */
export function moveDivider(tree: NestTree, path: readonly number[], index: number, at: number): NestTree {
  const n = nodeAt(tree, path);
  if (!tree || !n || !isSplit(n) || index < 0 || index >= n.children.length - 1 || !Number.isFinite(at)) return tree;
  const sum = n.weights.reduce((a, b) => a + b, 0) || 1;
  const w = n.weights.map((v) => v / sum);
  const start = w.slice(0, index).reduce((a, b) => a + b, 0);
  const pair = (w[index] as number) + (w[index + 1] as number);
  const left = Math.min(pair - WEIGHT_MIN, Math.max(WEIGHT_MIN, at - start));
  if (left <= 0 || pair - left <= 0) return tree;
  w[index] = left;
  w[index + 1] = pair - left;
  return replaceAt(tree, path, () => ({ ...n, weights: w }));
}

/** The tree holds exactly `keys`: programs that are gone leave, new ones join (split from the
 *  focused program, in the order of `keys`). */
export function syncNest(
  tree: NestTree,
  keys: readonly string[],
  opts: { focus?: string | null; aspect?: number; zoom?: readonly number[] } = {},
): NestTree {
  const want = new Set(keys);
  const kept = keysOf(tree)
    .filter((k) => !want.has(k))
    .reduce((t, k) => removeProgram(t, k), tree);
  return keys.reduce((t, k) => addProgram(t, k, opts), kept);
}

// ---------- keeping it ----------

export interface NestState {
  tree: NestTree;
  zoom: ZoomPath;
}

/** A nest per screen (the primary screen is 0; the browser's desktop has only that one). */
export type Nests = Record<number, NestState>;

const KEY = "topcoat.nest";
/** A screen's index as stored: digits only ("0", "12"; not "-1" or "x"). */
const SCREEN_INDEX = /^\d+$/;
const MAX_DEPTH = 32;

/** A stored node as a tree, or null when it is not one (a node of a wrong shape is dropped, with
 *  everything under it). Programs that are not in `known`, and repeats, are dropped too. */
function readNode(v: unknown, known: (key: string) => boolean, seen: Set<string>, depth: number): NestTree {
  if (!v || typeof v !== "object" || depth > MAX_DEPTH) return null;
  const n = v as Record<string, unknown>;
  if (typeof n.key === "string") {
    if (!known(n.key) || seen.has(n.key)) return null;
    seen.add(n.key);
    return { key: n.key };
  }
  if ((n.axis !== "x" && n.axis !== "y") || !Array.isArray(n.children) || !Array.isArray(n.weights)) return null;
  const weights = n.weights as unknown[];
  const children: NestNode[] = [];
  const kept: number[] = [];
  n.children.forEach((c, i) => {
    const r = readNode(c, known, seen, depth + 1);
    if (r) {
      children.push(r);
      kept.push(goodWeight(weights[i]) ? weights[i] : 1);
    }
  });
  return { axis: n.axis, children, weights: kept };
}

/** The nests kept in this web view, with only the programs that `known` accepts (the open ones). */
export function loadNests(known: (key: string) => boolean): Nests {
  try {
    const raw: unknown = JSON.parse(window.localStorage.getItem(overlayStoreKey(KEY, window.location.search)) ?? "{}");
    const out: Nests = {};
    if (!raw || typeof raw !== "object") return out;
    const seen = new Set<string>();
    for (const [s, v] of Object.entries(raw)) {
      if (!SCREEN_INDEX.test(s) || !v || typeof v !== "object") continue;
      const st = v as Record<string, unknown>;
      const tree = normalise(readNode(st.tree, known, seen, 0));
      if (!tree) continue;
      const zoom = Array.isArray(st.zoom) ? validZoom(tree, st.zoom as number[]) : [];
      out[Number(s)] = { tree, zoom };
    }
    return out;
  } catch {
    return {};
  }
}

export function saveNests(nests: Nests): void {
  try {
    window.localStorage.setItem(overlayStoreKey(KEY, window.location.search), JSON.stringify(nests));
  } catch {
    /* no storage: the nests last as long as the page */
  }
}
