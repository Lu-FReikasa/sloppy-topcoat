// Auto-tiling (docs/DESKTOP.md): where each shown program stands, as fractions of the desktop.
// One fills it, two stand side by side, three are one large and two small, four and more a grid
// whose last row takes the width that is left.

export interface Rect {
  x: number;
  y: number;
  w: number;
  h: number;
}

export function tile(n: number): Rect[] {
  if (n <= 0) return [];
  if (n === 1) return [{ x: 0, y: 0, w: 1, h: 1 }];
  if (n === 2)
    return [
      { x: 0, y: 0, w: 0.5, h: 1 },
      { x: 0.5, y: 0, w: 0.5, h: 1 },
    ];
  if (n === 3)
    return [
      { x: 0, y: 0, w: 0.5, h: 1 },
      { x: 0.5, y: 0, w: 0.5, h: 0.5 },
      { x: 0.5, y: 0.5, w: 0.5, h: 0.5 },
    ];
  const cols = Math.ceil(Math.sqrt(n));
  const rows = Math.ceil(n / cols);
  const out: Rect[] = [];
  for (let i = 0; i < n; i += 1) {
    const row = Math.floor(i / cols);
    const inRow = row === rows - 1 ? n - cols * (rows - 1) : cols;
    const col = i - row * cols;
    out.push({ x: col / inRow, y: row / rows, w: 1 / inRow, h: 1 / rows });
  }
  return out;
}

/** The smallest a tiled program should be, in CSS pixels: below it a view needs a scrollbar. */
export const MIN_TILE = { w: 480, h: 320 };

/** How many programs fit side by side on a desktop of this size (at least one, at most six). */
export function fits(width: number, height: number): number {
  if (width <= 0 || height <= 0) return 6; // not measured yet (tests, the first paint)
  const cols = Math.max(1, Math.floor(width / MIN_TILE.w));
  const rows = Math.max(1, Math.floor(height / MIN_TILE.h));
  return Math.max(1, Math.min(6, cols * rows));
}

/** Of the programs shown, the ones that get a tile when only `max` fit: the one in front, and the
 *  ones opened last; the others wait on the taskbar. The order stays the order of the taskbar. */
export function inTiles(shown: string[], focus: string | null, max: number): string[] {
  if (shown.length <= max) return shown;
  const keep = new Set<string>();
  if (focus && shown.includes(focus)) keep.add(focus);
  for (let i = shown.length - 1; i >= 0 && keep.size < max; i -= 1) keep.add(shown[i] as string);
  return shown.filter((k) => keep.has(k));
}

/** Percent for a style. */
export function pct(v: number): string {
  return `${Math.round(v * 10000) / 100}%`;
}
