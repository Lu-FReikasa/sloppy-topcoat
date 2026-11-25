/** The frame meter's arithmetic (P0 of the desktop plan): how smooth the page draws, from the gaps
 *  between animation frames, and which renderer the web view uses (a GPU, or software). */

export interface FrameStats {
  /** Frames per second over the window measured. */
  fps: number;
  /** The longest gap, in milliseconds. */
  worstMs: number;
  /** The gap that 99 % of the frames stay under (the "1 % low" as a time). */
  p99Ms: number;
  /** Frames that took longer than one and a half budgets (a missed refresh). */
  slow: number;
}

/** Stats of the gaps between frames; `budgetMs` is one refresh (8.33 ms at 120 Hz). */
export function frameStats(gaps: readonly number[], budgetMs: number): FrameStats {
  const valid = gaps.filter((g) => Number.isFinite(g) && g > 0);
  if (valid.length === 0) return { fps: 0, worstMs: 0, p99Ms: 0, slow: 0 };
  const total = valid.reduce((a, b) => a + b, 0);
  const sorted = [...valid].sort((a, b) => a - b);
  const p99 = sorted[Math.min(sorted.length - 1, Math.floor(sorted.length * 0.99))] ?? 0;
  return {
    fps: Math.round((valid.length * 1000) / total),
    worstMs: Math.round((sorted[sorted.length - 1] ?? 0) * 10) / 10,
    p99Ms: Math.round(p99 * 10) / 10,
    slow: valid.filter((g) => g > budgetMs * 1.5).length,
  };
}

/** The refresh a run of gaps suggests: the median gap, rounded to a common rate (60, 120, 144 …). */
export function refreshOf(gaps: readonly number[]): number {
  const valid = gaps.filter((g) => Number.isFinite(g) && g > 0).sort((a, b) => a - b);
  const mid = valid[Math.floor(valid.length / 2)];
  if (!mid) return 60;
  const hz = 1000 / mid;
  const common = [30, 60, 75, 90, 100, 120, 144, 165, 240];
  return common.reduce((best, c) => (Math.abs(c - hz) < Math.abs(best - hz) ? c : best), 60);
}

/** Does a renderer string name a software rasteriser (no GPU acceleration)? */
export function isSoftware(renderer: string): boolean {
  return /swiftshader|basic render|llvmpipe|software/i.test(renderer);
}

/** How often the desktop rendered (counted by Desktop itself): the meter shows renders per second,
 *  so a stream event that redraws every window is seen at once. */
let renders = 0;
export function countRender(): void {
  renders += 1;
}
export function rendersSoFar(): number {
  return renders;
}

/** The renderer of this web view, as WebGL names it (the GPU, or a software rasteriser). */
export function gpuRenderer(): string {
  try {
    const canvas = document.createElement("canvas");
    const gl = canvas.getContext("webgl2") ?? canvas.getContext("webgl");
    if (!gl) return "";
    const ext = gl.getExtension("WEBGL_debug_renderer_info");
    const name: unknown = ext ? gl.getParameter(ext.UNMASKED_RENDERER_WEBGL) : gl.getParameter(gl.RENDERER);
    gl.getExtension("WEBGL_lose_context")?.loseContext();
    return typeof name === "string" ? name : "";
  } catch {
    return "";
  }
}
