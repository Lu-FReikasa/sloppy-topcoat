// browser.audit (E6-02): the checks, the default matrix of sizes, and the loop that sets a size,
// measures, and keeps only what is wrong. The measurements themselves are in audit-page.mjs.
import { auditInPage } from "./audit-page.mjs";

export const CHECKS = [
  "page_scroll",
  "inner_scroll",
  "clipped_text",
  "overlap",
  "target_size",
  "font_size",
  "contrast",
  "focus",
  "reduced_motion",
];

// phone, tablet and desktop in portrait and landscape: widths 320 … 2560 (the layout check's range)
export const DEFAULT_MATRIX = [
  { w: 320, h: 640 },
  { w: 360, h: 740 },
  { w: 390, h: 844 },
  { w: 844, h: 390 },
  { w: 768, h: 1024 },
  { w: 1024, h: 768 },
  { w: 1280, h: 720 },
  { w: 1920, h: 1080 },
  { w: 2560, h: 1440 },
];

export const LIMITS = {
  minTarget: 40, // px, as in layout-check
  minFont: 12, // px
  minContrast: 4.5, // WCAG AA for body text
  maxPerCheck: 8, // violations kept per check and size
  maxTotal: 120, // violations in one answer
};

/**
 * Keep the answer small: at most `maxTotal` violations, the rest only counted. Pure.
 * @param {{w:number,h:number,violations:object[],counts:Record<string,number>}[]} sizes
 */
export function trimResult(sizes, maxTotal = LIMITS.maxTotal) {
  let left = maxTotal;
  const kept = [];
  let dropped = 0;
  for (const size of sizes) {
    if (size.violations.length === 0) continue; // only the sizes with something wrong
    const take = size.violations.slice(0, Math.max(0, left));
    left -= take.length;
    dropped += size.violations.length - take.length;
    kept.push({ w: size.w, h: size.h, counts: size.counts, violations: take });
  }
  return { sizes: kept, dropped };
}

/** Total number of violations per check over all sizes. Pure. */
export function summarise(sizes) {
  const by = {};
  for (const size of sizes) {
    for (const [check, n] of Object.entries(size.counts)) by[check] = (by[check] ?? 0) + n;
  }
  return by;
}

/**
 * Run the audit on an open Playwright page.
 * @param {import("playwright").Page} page
 * @param {{checks?: string[], matrix?: {w:number,h:number,dpr?:number}[]}} args
 * @param {(size: {w:number,h:number,dpr?:number}) => Promise<void>} setSize sets the viewport
 */
export async function runAudit(page, args, setSize) {
  const unknown = (args.checks ?? []).filter((c) => !CHECKS.includes(c));
  if (unknown.length) throw new Error(`unknown check: ${unknown.join(", ")} (known: ${CHECKS.join(", ")})`);
  const checks = args.checks?.length ? args.checks : CHECKS;
  const matrix = args.matrix?.length ? args.matrix : DEFAULT_MATRIX;
  const inPage = checks.filter((c) => c !== "reduced_motion");
  const sizes = [];
  for (const size of matrix) {
    await setSize(size);
    await page.waitForTimeout(120); // let the layout settle after the resize
    const { violations, counts } = inPage.length
      ? await page.evaluate(auditInPage, { checks: inPage, opts: LIMITS })
      : { violations: [], counts: {} };
    sizes.push({ w: size.w, h: size.h, violations, counts });
  }
  if (checks.includes("reduced_motion")) {
    // once, at the last size: with reduced motion asked for, nothing may keep moving
    await page.emulateMedia({ reducedMotion: "reduce" });
    await page.waitForTimeout(120);
    const moving = await page.evaluate(() =>
      document
        .getAnimations()
        .filter((a) => a.playState === "running" && Number(a.effect?.getComputedTiming().duration) > 0.01)
        .slice(0, 8)
        .map((a) => ({
          check: "reduced_motion",
          selector: a.effect?.target?.tagName?.toLowerCase() ?? "",
          animation: a.animationName ?? a.transitionProperty ?? a.id ?? "",
          duration_ms: Number(a.effect.getComputedTiming().duration),
        })),
    );
    await page.emulateMedia({ reducedMotion: null });
    const last = sizes[sizes.length - 1];
    if (last && moving.length) {
      last.violations.push(...moving);
      last.counts.reduced_motion = moving.length;
    }
  }
  const { sizes: kept, dropped } = trimResult(sizes);
  return {
    checked: sizes.length,
    checks,
    clean: kept.length === 0,
    by_check: summarise(sizes),
    sizes: kept,
    dropped,
  };
}
