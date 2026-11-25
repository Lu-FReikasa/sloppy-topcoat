import { useEffect, useState } from "react";
import { reducedMotion } from "../motion";

const COLS = 44;
const ROWS = 7;
const TICK_MS = 700;
const MIDDLE = Math.floor(ROWS / 2);
const MARK = "tc";

/** A cheap deterministic scatter: the same cell and the same moment always give the same answer, so
 *  a frame can be tested and nothing needs a random number. */
function lit(x: number, y: number, t: number): boolean {
  const h =
    (Math.imul(x + 1, 73856093) ^
      Math.imul(y + 1, 19349663) ^
      Math.imul(t + 1, 83492791)) >>>
    0;
  return h % 41 === 0;
}

/** One frame of the idle picture: a sparse field of dots that drifts one cell to the right per
 *  moment, the mark of the system in the middle and a cursor that blinks after it. */
export function frame(t: number): string {
  const rows: string[] = [];
  for (let y = 0; y < ROWS; y += 1) {
    let line = "";
    for (let x = 0; x < COLS; x += 1) {
      const drift = (x - t + COLS * 4) % COLS;
      line += lit(drift, y, 0) ? (lit(drift, y, 1) ? "+" : "·") : " ";
    }
    rows.push(line);
  }
  const text = `> ${MARK}${t % 2 === 0 ? "_" : " "}`;
  const at = Math.floor((COLS - text.length) / 2);
  const mid = rows[MIDDLE] ?? "";
  rows[MIDDLE] = mid.slice(0, at) + text + mid.slice(at + text.length);
  return rows.join("\n");
}


/** What the empty chat window shows while nothing is going on. It is decoration: hidden from
 *  screen readers, and still when the person asked their system for less motion. */
export function IdleArt() {
  const [t, setT] = useState(0);
  useEffect(() => {
    if (reducedMotion()) return;
    const timer = setInterval(() => setT((n) => n + 1), TICK_MS);
    return () => clearInterval(timer);
  }, []);
  return (
    <pre className="idle-art" aria-hidden="true" data-testid="idle-art">
      {frame(t)}
    </pre>
  );
}
