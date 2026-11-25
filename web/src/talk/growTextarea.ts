import { type RefObject, useCallback, useEffect, useLayoutEffect } from "react";

/** The message box shows at least this many lines, ... */
export const MIN_LINES = 3;
/** ... and grows with the text up to this many; beyond that it scrolls inside. */
export const MAX_LINES = 10;

/** used when the style gives no font size (never in a browser) */
const FALLBACK_FONT_PX = 16;
/** browsers draw `line-height: normal` at about this many times the font size */
const NORMAL_LINE_HEIGHT = 1.2;
/** a rounding pixel of scrollHeight must not count as a line of its own */
const ROUNDING_PX = 1;

export interface BoxMetrics {
  /** scrollHeight of the box measured with `height: auto`: text plus vertical padding */
  scrollHeight: number;
  lineHeight: number;
  /** top plus bottom padding */
  padding: number;
  /** top plus bottom border (the box is border-box) */
  border: number;
  /** the style's max-height in px (a low window allows fewer lines); Infinity when there is none */
  maxHeight?: number;
}

/**
 * The height of a growing text box: as many lines as the text needs, at least `min`, at most `max`;
 * `scrolls` says whether the text is longer than that. Pure.
 */
export function growHeight(
  m: BoxMetrics,
  min = MIN_LINES,
  max = MAX_LINES,
): { height: number; lines: number; scrolls: boolean } {
  const line = m.lineHeight > 0 ? m.lineHeight : FALLBACK_FONT_PX * NORMAL_LINE_HEIGHT;
  const needed = Math.max(1, Math.ceil((m.scrollHeight - m.padding - ROUNDING_PX) / line));
  const fits = Math.floor(((m.maxHeight ?? Infinity) - m.padding - m.border + ROUNDING_PX) / line);
  const top = Math.max(1, Math.min(max, fits));
  const lines = Math.min(top, Math.max(min, needed));
  return { height: lines * line + m.padding + m.border, lines, scrolls: needed > top };
}

function metricsOf(el: HTMLTextAreaElement): BoxMetrics {
  const s = getComputedStyle(el);
  const px = (v: string): number => parseFloat(v) || 0;
  const fontSize = px(s.fontSize) || FALLBACK_FONT_PX;
  return {
    scrollHeight: el.scrollHeight,
    lineHeight: px(s.lineHeight) || fontSize * NORMAL_LINE_HEIGHT,
    padding: px(s.paddingTop) + px(s.paddingBottom),
    border: px(s.borderTopWidth) + px(s.borderBottomWidth),
    maxHeight: px(s.maxHeight) || Infinity, // "none" -> Infinity
  };
}

/**
 * Sizes the text box behind `ref` to its text (3 to 10 lines) whenever `value` changes and when the
 * box gets narrower or wider (the text wraps differently then).
 */
export function useGrowingTextarea(
  ref: RefObject<HTMLTextAreaElement | null>,
  value: string,
): void {
  const fit = useCallback((): void => {
    const el = ref.current;
    if (!el) return;
    el.style.height = "auto"; // so that scrollHeight is the text, not the last height
    const { height, scrolls } = growHeight(metricsOf(el));
    el.style.height = `${height}px`;
    el.style.overflowY = scrolls ? "auto" : "hidden";
  }, [ref]);

  useLayoutEffect(fit, [fit, value]);

  useEffect(() => {
    const el = ref.current;
    if (!el || typeof ResizeObserver === "undefined") return undefined;
    let width = el.clientWidth;
    // only a change of width re-wraps the text; the height is ours
    const observer = new ResizeObserver(() => {
      if (el.clientWidth === width) return;
      width = el.clientWidth;
      fit();
    });
    observer.observe(el);
    return () => observer.disconnect();
  }, [ref, fit]);
}
