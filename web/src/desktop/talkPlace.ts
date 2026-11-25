// Where Clipper's talk stands at a window: inside it, below its title bar (2026-11-20, the
// maintainer: with tiled windows a talk over the window's edge lay on its neighbour). At the perch
// he stands in the window's lower right corner with his bubble and field; folded out, the talk is a
// column in the right part of the window's frame. The window gives the column room (`--talk-share`, desktop.css), so window
// and column are one rigid block that moves and sizes together.

/** The part of the window the folded-out talk takes. */
export const TALK_SHARE = 0.38;
/** The window's title bar (`.dt-titlebar`, min-height 1.9rem). */
const TITLE = "1.9rem";

const pct = (n: number) => `${n * 100}%`;

export function talkPlace(
  b: { x: number; y: number; w: number; h: number },
  column: boolean,
): Record<string, string> {
  if (!column) {
    // the whole window below the title bar; the box lets clicks through (clipper.css)
    return { left: pct(b.x), top: `calc(${pct(b.y)} + ${TITLE})`, width: pct(b.w), height: `calc(${pct(b.h)} - ${TITLE})` };
  }
  return {
    left: pct(b.x + b.w * (1 - TALK_SHARE)),
    top: `calc(${pct(b.y)} + ${TITLE})`,
    width: pct(b.w * TALK_SHARE),
    height: `calc(${pct(b.h)} - ${TITLE})`,
  };
}
