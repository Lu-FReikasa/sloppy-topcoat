// The theme workshop without React (ThemeWorkshop.tsx): the variables of a style sheet for the
// controls, a variable set without a model, and playing a window's animations on demand.
import type { ThemeManifest } from "./theme";

export interface WorkshopTurnResult {
  theme: ThemeManifest;
  applied: boolean;
  reply: string;
  changed: string[];
  notes: string[];
  problems: string[];
  lint: string[];
  model: string;
  calls: number;
  ms: number;
}

/** The colour variables a theme usually sets (the bases read them, docs/THEMES.md). */
export const COLOUR_VARS = ["--bg", "--surface", "--text", "--muted", "--border", "--accent", "--accent-text"] as const;

const HEX = /^#([0-9a-f]{3}|[0-9a-f]{6})$/i;

/** The part of a style sheet before its first block: where the bare declarations stand. */
function head(css: string): [string, string] {
  const at = css.indexOf("{");
  if (at < 0) return [css, ""];
  // the head ends after the last ';' before the first block (the block's selector is not head)
  const cut = css.lastIndexOf(";", at) + 1;
  return [css.slice(0, cut), css.slice(cut)];
}

/** The bare variables of a style sheet, in order: `--name` and its value. */
export function readVars(css: string | undefined): [string, string][] {
  const [top] = head(css ?? "");
  const out: [string, string][] = [];
  for (const m of top.matchAll(/(--[\w-]+)\s*:\s*([^;]*);/g)) out.push([m[1]!, m[2]!.trim()]);
  return out;
}

/** A colour of the colour input: a hex value as #rrggbb, anything else none. */
export function asHex(value: string | undefined): string | null {
  if (!value || !HEX.test(value.trim())) return null;
  const v = value.trim();
  return v.length === 4 ? `#${v[1]}${v[1]}${v[2]}${v[2]}${v[3]}${v[3]}` : v.toLowerCase();
}

/** The style sheet with `name` set to `value` among the bare declarations (added at the top). */
export function setVar(css: string | undefined, name: string, value: string): string {
  const [top, rest] = head(css ?? "");
  const decl = `${name}: ${value};`;
  const re = new RegExp(`(^|[;\\s])${name.replace(/[-]/g, "\\-")}\\s*:[^;]*;`);
  if (re.test(top)) return top.replace(re, (_m, before: string) => `${before}${decl}`) + rest;
  return `${decl}\n${css ?? ""}`.trimEnd();
}

/** Play an animation of a window again: `open` (the window's own animation from the start),
 *  `close` (.dt-closing for the theme's length) or `focus` (.dt-focused off and on). */
export function playOn(el: HTMLElement | null, what: "open" | "close" | "focus", ms: number): void {
  if (!el) return;
  if (what === "open") {
    el.style.animation = "none";
    void el.offsetWidth; // the browser forgets the animation, the next frame starts it anew
    el.style.animation = "";
    return;
  }
  const cls = what === "close" ? "dt-closing" : "dt-focused";
  if (what === "close") {
    el.classList.add(cls);
    window.setTimeout(() => {
      el.classList.remove(cls);
      playOn(el, "open", ms);
    }, Math.max(ms, 50) + 150);
  } else {
    el.classList.remove(cls);
    void el.offsetWidth;
    window.setTimeout(() => el.classList.add(cls), 120);
  }
}
