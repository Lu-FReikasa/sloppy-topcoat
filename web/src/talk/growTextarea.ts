import { type RefObject, useEffect, useLayoutEffect } from "react";

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
/** the box takes at most this share of the height of its room (the chat window), so the messages
    stay visible in a small window */
export const ROOM_SHARE = 0.5;

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

function metricsOf(el: HTMLTextAreaElement, room: HTMLElement | null): BoxMetrics {
  const s = getComputedStyle(el);
  const px = (v: string): number => parseFloat(v) || 0;
  const fontSize = px(s.fontSize) || FALLBACK_FONT_PX;
  const styleMax = px(s.maxHeight) || Infinity; // "none" -> Infinity
  const roomMax = room && room.clientHeight > 0 ? room.clientHeight * ROOM_SHARE : Infinity;
  return {
    scrollHeight: el.scrollHeight,
    lineHeight: px(s.lineHeight) || fontSize * NORMAL_LINE_HEIGHT,
    padding: px(s.paddingTop) + px(s.paddingBottom),
    border: px(s.borderTopWidth) + px(s.borderBottomWidth),
    maxHeight: Math.min(styleMax, roomMax),
  };
}

/** Sets the height of `el` to its text, within the limits of growHeight and ROOM_SHARE of `room`. */
function fitBox(el: HTMLTextAreaElement, room: HTMLElement | null): void {
  el.style.height = "auto"; // so that scrollHeight is the text, not the last height
  const { height, scrolls } = growHeight(metricsOf(el, room));
  el.style.height = `${height}px`;
  el.style.overflowY = scrolls ? "auto" : "hidden";
}

/**
 * Sizes the text box behind `ref` to its text (3 to 10 lines) whenever `value` changes, when the box
 * gets narrower or wider (the text wraps differently then) and when its `room` (the element whose
 * height is fixed from outside, e.g. the chat window) changes size; the box takes at most
 * ROOM_SHARE of the room's height.
 */
export function useGrowingTextarea(
  ref: RefObject<HTMLTextAreaElement | null>,
  value: string,
  room?: RefObject<HTMLElement | null>,
): void {
  useLayoutEffect(() => {
    if (ref.current) fitBox(ref.current, room?.current ?? null);
  }, [ref, room, value]);

  useEffect(() => {
    const el = ref.current;
    if (!el || typeof ResizeObserver === "undefined") return undefined;
    const box = room?.current ?? null;
    let width = el.clientWidth;
    let roomHeight = box?.clientHeight ?? 0;
    // only the box's width re-wraps the text (its height is ours); the room's height sets the cap
    const observer = new ResizeObserver(() => {
      const nowRoom = box?.clientHeight ?? 0;
      if (el.clientWidth === width && nowRoom === roomHeight) return;
      width = el.clientWidth;
      roomHeight = nowRoom;
      fitBox(el, box);
    });
    observer.observe(el);
    if (box) observer.observe(box);
    return () => observer.disconnect();
  }, [ref, room]);
}
