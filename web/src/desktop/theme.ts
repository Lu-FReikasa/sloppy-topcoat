// A theme decides how the desktop is drawn and arranged (docs/THEMES.md): how the programs are
// laid out, where the taskbar stands and what it holds, what the title bar of a program has, how
// things move. It is data, never code; what a theme cannot say in this data, it says in CSS.
//
// The desktop itself (which programs are open, minimised, in front; the views; the keys; the
// palette) is the core in `state.ts` and does not depend on a theme. A theme only arranges what the
// core hands it.
import { tile, type Rect } from "./tiling";

/** How the programs that are shown are laid out. `nest`: splits that nest, with a view that zooms
 *  to a part of them (nest.ts; the desktop places these itself, not with `layoutRects`). */
export type LayoutKind = "tile" | "columns" | "rows" | "focus" | "cascade" | "nest";
export const LAYOUT_KINDS: readonly LayoutKind[] = ["tile", "columns", "rows", "focus", "cascade", "nest"];

/** Where the taskbar stands. `hidden`: not drawn; the palette (Ctrl+K) and the keys still work. */
export type BarPosition = "bottom" | "top" | "left" | "right" | "hidden";
export const BAR_POSITIONS: readonly BarPosition[] = ["bottom", "top", "left", "right", "hidden"];

/** What the taskbar holds, in the order a theme lists it. */
export type BarItem = "start" | "programs" | "clock" | "status" | "me";
export const BAR_ITEMS: readonly BarItem[] = ["start", "programs", "clock", "status", "me"];

/** The buttons of a program's title bar, in the order a theme lists them. */
export type ChromeButton = "minimise" | "maximise" | "close";
export const CHROME_BUTTONS: readonly ChromeButton[] = ["minimise", "maximise", "close"];

export type MotionStyle = "none" | "fade" | "slide" | "flicker" | "zoom";
export const MOTION_STYLES: readonly MotionStyle[] = ["none", "fade", "slide", "flicker", "zoom"];

export interface ThemeManifest {
  /** Stable key; stored in the person's preferences. */
  key: string;
  /** What the person sees in the list of themes (a built-in theme is translated by its key). */
  name: string;
  /** A built-in theme whose style sheet this one starts from (`dt-look-<base>`). */
  base?: "terminal" | "leitstand" | "schreibtisch";
  layout: { kind: LayoutKind };
  bar: { position: BarPosition; items: BarItem[] };
  chrome: { buttons: ChromeButton[]; titlebar: "top" | "hidden" };
  /** `style`: a window opens; `close`: it closes or is minimised; `overlay`: the overlay of the
   *  desktop client comes and goes. `ms` is the length of each. */
  motion: { style: MotionStyle; ms: number; close?: MotionStyle; overlay?: MotionStyle };
  /** Extra style sheet of this theme (custom themes; checked by `safeCss`). */
  css?: string;
}

const STANDARD_BAR: BarItem[] = ["start", "programs", "status", "me"];
const STANDARD_CHROME: ThemeManifest["chrome"] = {
  buttons: ["minimise", "maximise", "close"],
  titlebar: "top",
};

/** The built-in style sheets a theme can start from (`base`): `desktop.css`, `leitstand.css` and
 *  `schreibtisch.css`. */
export const BASES: readonly NonNullable<ThemeManifest["base"]>[] = ["terminal", "leitstand", "schreibtisch"];

/** Terminal Nest (#16): the terminal in panes, like a terminal multiplexer. Its own look is a style
 *  sheet like a custom theme's, within the same limits (`cssProblems`): one thin title line per
 *  program, the taskbar as one line at the top, crisp one-pixel borders between the panes. */
const TERMINAL_NEST_CSS = `--radius: 0;
font-family: ui-monospace, "JetBrains Mono", "SFMono-Regular", "DejaVu Sans Mono", Consolas, monospace;
.dt-stage { background: var(--border); }
.dt-program { border: 0; box-shadow: none; outline: 1px solid var(--border); outline-offset: -1px; }
.dt-program.dt-focused { outline-color: hsl(var(--pg-hue) 70% 60%); z-index: 1; }
.dt-titlebar { min-height: 0; height: 1.35rem; padding: 0 0 0 0.6ch; gap: 1ch; font-size: 0.8rem; background: var(--bg); border-bottom: 1px solid var(--border); }
.dt-focused .dt-titlebar { background: hsl(var(--pg-hue) 70% 60%); color: var(--accent-text); }
.dt-focused .dt-title, .dt-focused .dt-code { color: var(--accent-text); }
.dt-title { font-weight: 400; }
.dt-lamp, .dt-titlebar .dt-icon { display: none; }
.dt-titlebar-buttons button { min-width: 2.2ch; height: 1.35rem; padding: 0 0.4ch; border: 0; background: transparent; color: inherit; font-size: 0.8rem; }
.dt-titlebar-buttons button:hover { background: var(--track); }
.dt-taskbar { min-height: 0; height: 1.6rem; padding: 0; gap: 0; border-top: 0; border-bottom: 1px solid var(--border); font-size: 0.8rem; }
.dt-taskbar button { min-height: 0; height: 1.6rem; border: 0; border-radius: 0; padding: 0 1ch; }
.dt-task { width: auto; max-width: 14rem; border-bottom: 0; }
.dt-task.dt-front, .dt-task.dt-front .dt-icon { background: hsl(var(--pg-hue) 70% 60%); color: var(--accent-text); }
.dt-start { background: var(--accent); color: var(--accent-text); }
.dt-program.dt-scaled .dt-titlebar { font-style: italic; }
@media (prefers-reduced-motion: reduce) { .dt-program { animation: none; } }`;

/** The themes that come with the web build: the first three are the style sheets above; the rest
 *  is data like any other theme's. */
export const BUILTIN_THEMES: readonly ThemeManifest[] = [
  {
    key: "terminal",
    name: "Terminal",
    base: "terminal",
    layout: { kind: "tile" },
    bar: { position: "bottom", items: STANDARD_BAR },
    chrome: STANDARD_CHROME,
    motion: { style: "none", ms: 0 },
  },
  {
    key: "leitstand",
    name: "Leitstand",
    base: "leitstand",
    layout: { kind: "tile" },
    bar: {
      position: "bottom",
      items: ["start", "programs", "clock", "status", "me"],
    },
    chrome: STANDARD_CHROME,
    // the control room powers its displays up in its own style sheet (dt-power)
    motion: { style: "none", ms: 0 },
  },
  {
    key: "schreibtisch",
    name: "Schreibtisch",
    base: "schreibtisch",
    layout: { kind: "tile" },
    bar: { position: "bottom", items: STANDARD_BAR },
    chrome: STANDARD_CHROME,
    motion: { style: "fade", ms: 120 },
  },
  {
    key: "terminal-nest",
    name: "Terminal Nest",
    base: "terminal",
    layout: { kind: "nest" },
    bar: { position: "top", items: ["start", "programs", "clock", "status", "me"] },
    chrome: { buttons: ["minimise", "close"], titlebar: "top" },
    motion: { style: "zoom", ms: 140, close: "fade" },
    css: TERMINAL_NEST_CSS,
  },
];

export const DEFAULT_THEME_KEY = "terminal";
const BUILTIN_BY_KEY = new Map(BUILTIN_THEMES.map((t) => [t.key, t]));
const FALLBACK = BUILTIN_BY_KEY.get(DEFAULT_THEME_KEY)!;

/** The parts a theme's bar shows. The name menu is always among them (at the end when the theme
 *  leaves it out): it is the only way to language, look, hue and sign-out. */
export function barItems(theme: Pick<ThemeManifest, "bar">): BarItem[] {
  const items = theme.bar.items;
  return items.includes("me") ? items : [...items, "me"];
}

export function isBuiltin(key: string): boolean {
  return BUILTIN_BY_KEY.has(key);
}

/** The theme to draw with: a custom one of this key, else a built-in one, else the terminal. */
export function resolveTheme(key: string | null | undefined, custom: readonly ThemeManifest[] = []): ThemeManifest {
  return custom.find((t) => t.key === key) ?? BUILTIN_BY_KEY.get(key ?? "") ?? FALLBACK;
}

/** The themes a person can choose from: the built-in ones, then the custom ones. */
export function allThemes(custom: readonly ThemeManifest[] = []): ThemeManifest[] {
  const own = custom.filter((t) => !isBuiltin(t.key));
  return [...BUILTIN_THEMES, ...own];
}

// ---------- layout engines ----------

const clamp = (n: number, lo: number, hi: number) => Math.min(hi, Math.max(lo, n));

/** Where each of `n` shown programs stands, as fractions of the stage. The last one is in front for
 *  `cascade` (the others lie under it); all other engines keep the programs apart. `nest` keeps a
 *  tree of its own (nest.ts); asked here, it tiles. */
export function layoutRects(kind: LayoutKind, n: number): Rect[] {
  if (n <= 0) return [];
  switch (kind) {
    case "columns":
      return Array.from({ length: n }, (_, i) => ({
        x: i / n,
        y: 0,
        w: 1 / n,
        h: 1,
      }));
    case "rows":
      return Array.from({ length: n }, (_, i) => ({
        x: 0,
        y: i / n,
        w: 1,
        h: 1 / n,
      }));
    case "focus": {
      if (n === 1) return [{ x: 0, y: 0, w: 1, h: 1 }];
      const side = n - 1;
      return [
        { x: 0, y: 0, w: 0.64, h: 1 },
        ...Array.from({ length: side }, (_, i) => ({
          x: 0.64,
          y: i / side,
          w: 0.36,
          h: 1 / side,
        })),
      ];
    }
    case "cascade": {
      if (n === 1) return [{ x: 0, y: 0, w: 1, h: 1 }];
      const step = clamp(0.3 / (n - 1), 0.03, 0.08);
      const size = 1 - step * (n - 1);
      return Array.from({ length: n }, (_, i) => ({
        x: i * step,
        y: i * step,
        w: size,
        h: size,
      }));
    }
    case "tile":
    case "nest":
    default:
      return tile(n);
  }
}

// ---------- checking a manifest ----------

const KEY = /^[a-z][a-z0-9-]{0,39}$/;

/** What is wrong with a theme document, or an empty list. A theme the hub stores passes this; the
 *  web build checks it again before it draws one. */
export function themeProblems(value: unknown): string[] {
  const out: string[] = [];
  if (typeof value !== "object" || value === null) return ["the theme must be an object"];
  const m = value as Record<string, unknown>;
  if (typeof m.key !== "string" || !KEY.test(m.key)) out.push("key: lower case letters, digits, '-'");
  if (typeof m.name !== "string" || m.name.trim() === "" || m.name.length > 60) out.push("name: 1 to 60 characters");
  if (m.base !== undefined && !BASES.includes(m.base as NonNullable<ThemeManifest["base"]>))
    out.push(`base: one of ${BASES.join(", ")}`);
  const layout = m.layout as { kind?: unknown } | undefined;
  if (!layout || !LAYOUT_KINDS.includes(layout.kind as LayoutKind))
    out.push(`layout.kind: one of ${LAYOUT_KINDS.join(", ")}`);
  const bar = m.bar as { position?: unknown; items?: unknown } | undefined;
  if (!bar || !BAR_POSITIONS.includes(bar.position as BarPosition))
    out.push(`bar.position: one of ${BAR_POSITIONS.join(", ")}`);
  if (
    !bar ||
    !Array.isArray(bar.items) ||
    bar.items.some((i) => !BAR_ITEMS.includes(i as BarItem)) ||
    new Set(bar.items).size !== bar.items.length
  )
    out.push(`bar.items: a list without repeats of ${BAR_ITEMS.join(", ")}`);
  const chrome = m.chrome as { buttons?: unknown; titlebar?: unknown } | undefined;
  if (
    !chrome ||
    !Array.isArray(chrome.buttons) ||
    chrome.buttons.some((b) => !CHROME_BUTTONS.includes(b as ChromeButton)) ||
    new Set(chrome.buttons).size !== chrome.buttons.length
  )
    out.push(`chrome.buttons: a list without repeats of ${CHROME_BUTTONS.join(", ")}`);
  if (!chrome || (chrome.titlebar !== "top" && chrome.titlebar !== "hidden"))
    out.push("chrome.titlebar: top or hidden");
  const motion = m.motion as { style?: unknown; ms?: unknown; close?: unknown; overlay?: unknown } | undefined;
  if (!motion || !MOTION_STYLES.includes(motion.style as MotionStyle))
    out.push(`motion.style: one of ${MOTION_STYLES.join(", ")}`);
  for (const part of ["close", "overlay"] as const) {
    if (motion?.[part] !== undefined && !MOTION_STYLES.includes(motion[part] as MotionStyle))
      out.push(`motion.${part}: one of ${MOTION_STYLES.join(", ")}`);
  }
  if (!motion || typeof motion.ms !== "number" || motion.ms < 0 || motion.ms > 2000) out.push("motion.ms: 0 to 2000");
  if (m.css !== undefined) {
    if (typeof m.css !== "string") out.push("css: text");
    else out.push(...cssProblems(m.css));
  }
  return out;
}

const MAX_CSS = 40_000;

/** Why a style sheet may not be used. A theme's CSS can restyle everything it can reach, but it
 *  cannot load anything, run anything or hide from the check: no `@import`, no `url(` to anywhere
 *  but an inline `data:` image, no `expression(`, `javascript:`, `behavior`, `-moz-binding`. */
export function cssProblems(css: string): string[] {
  const out: string[] = [];
  if (css.length > MAX_CSS) out.push(`css: at most ${MAX_CSS} characters`);
  const flat = css.replace(/\/\*[\s\S]*?\*\//g, "").replace(/\\/g, "");
  if (/@import/i.test(flat)) out.push("css: @import is not allowed");
  if (/@charset|@namespace/i.test(flat)) out.push("css: @charset and @namespace are not allowed");
  for (const m of flat.matchAll(/url\(\s*(['"]?)([^'")]*)\1\s*\)/gi)) {
    if (!/^data:image\/(png|jpeg|gif|webp|svg\+xml)[;,]/i.test((m[2] ?? "").trim())) {
      out.push("css: url() may only hold an inline data:image");
      break;
    }
  }
  if (/expression\s*\(|javascript:|behavior\s*:|-moz-binding|<\/?\s*style|<\s*script/i.test(flat))
    out.push("css: script-like constructs are not allowed");
  return out;
}

/** Takes the `@keyframes` blocks out of a style sheet (they cannot be nested in a rule): returns
 *  them and what is left. */
export function splitKeyframes(css: string): {
  keyframes: string;
  rest: string;
} {
  let keyframes = "";
  let rest = "";
  let i = 0;
  const start = /@(-webkit-)?keyframes\b/gi;
  for (;;) {
    start.lastIndex = i;
    const m = start.exec(css);
    if (!m) break;
    const open = css.indexOf("{", m.index);
    if (open < 0) break;
    let depth = 0;
    let j = open;
    for (; j < css.length; j += 1) {
      if (css[j] === "{") depth += 1;
      else if (css[j] === "}") {
        depth -= 1;
        if (depth === 0) break;
      }
    }
    rest += css.slice(i, m.index);
    keyframes += css.slice(m.index, j + 1) + "\n";
    i = j + 1;
  }
  return { keyframes, rest: rest + css.slice(i) };
}

/** How long a window (`close`) or the overlay (`overlay`) waits for its leaving animation before it
 *  goes: the theme's length when the theme animates it (a preset, or its own rule for
 *  `.dt-closing` / `.dt-leaving`), nothing when it does not or the person asked their system for
 *  less motion. */
export function leaveMs(theme: ThemeManifest, part: "close" | "overlay", reduced: boolean): number {
  if (reduced) return 0;
  const preset = theme.motion[part] ?? "none";
  const own = (theme.css ?? "").includes(part === "close" ? "dt-closing" : "dt-leaving");
  return preset !== "none" || own ? Math.min(Math.max(theme.motion.ms, 0), 2000) : 0;
}

/** Classes that stand on the root element itself (where the bar is, the base look, solo, overlay). */
const ROOT_MODIFIER = /(^|[{};,]\s*)\.(dt-(?:bar-[a-z]+|look-[a-z0-9-]+|solo|overlay|arriving|leaving))(?![\w-])/gm;

/** A theme's selectors as they are meant inside the nesting under its root. The root element
 *  carries `.dt-root`, `.dt-theme-<key>` and its modifiers itself, so nested they would ask for a
 *  descendant of the root and match nothing; written by a model or a person they mean the root
 *  (live 2026-11-15: the variables stood in `.dt-theme-<key> { … }` and the desktop's background in
 *  `.dt-root { … }`, none of it was drawn). `:root` means the root too. */
export function rootSelectors(css: string, key: string): string {
  const own = new RegExp(`\\.dt-theme-${key.replace(/[^a-z0-9-]/g, "")}(?![\\w-])`, "g");
  return css
    .replace(/:root\b/g, "&")
    .replace(own, "&")
    .replace(/\.dt-root(?![\w-])/g, "&")
    .replace(ROOT_MODIFIER, "$1&.$2");
}

/** The CSS of a theme as it is put on the page, or "" when it is not safe. Every rule is limited to
 *  the root of the theme by CSS nesting, so a theme cannot restyle the login page or anything
 *  outside the desktop; its declarations apply to that root itself (the place for colours and sizes
 *  as variables). `@keyframes` stand outside, as they must.
 *
 *  The root selector weighs as much as an id (`:is(.dt-root, #dt-theme)`): a theme rule then wins
 *  over the rule of its base look for the same part, whatever selector the base used (the desk's
 *  `.dt-look-schreibtisch .dt-focused .dt-titlebar` beat a theme's `.dt-titlebar` before, and the
 *  theme could not know). The order among the theme's own rules stays as written. */
export function safeCss(theme: ThemeManifest): string {
  if (!theme.css || cssProblems(theme.css).length > 0) return "";
  const { keyframes, rest } = splitKeyframes(theme.css);
  return `${keyframes}.dt-theme-${theme.key}:is(.dt-root, #dt-theme) {\n${rootSelectors(rest, theme.key)}\n}`;
}

/** The `dt-` classes the desktop really draws (the markup of ProgramWindow, Taskbar, Desktop and
 *  CommandPalette). A rule for any other `.dt-` class matches nothing. */
export const DT_CLASSES: readonly string[] = [
  "dt-root", "dt-solo", "dt-overlay", "dt-arriving", "dt-leaving", "dt-meter", "dt-moving", "dt-snap-preview", "dt-tile", "dt-settings", "dt-overlay-hint", "dt-screen", "dt-pin", "dt-grip",
  "dt-grip-n", "dt-grip-s", "dt-grip-e", "dt-grip-w", "dt-grip-ne", "dt-grip-nw", "dt-grip-se", "dt-grip-sw", "dt-stage", "dt-view", "dt-banner", "dt-empty", "dt-note",
  "dt-program", "dt-focused", "dt-maxi", "dt-closing", "dt-scaled", "dt-unframed", "dt-titlebar", "dt-lamp", "dt-code", "dt-title", "dt-icon",
  "dt-titlebar-buttons", "dt-minimise", "dt-maximise", "dt-close", "dt-program-body", "dt-nav",
  "dt-taskbar", "dt-start", "dt-tasks", "dt-task", "dt-open", "dt-min", "dt-front", "dt-label",
  "dt-signal", "dt-signal-red", "dt-signal-amber", "dt-signal-green",
  "dt-clock", "dt-clock-zone", "dt-me", "dt-me-menu",
  "dt-palette", "dt-palette-backdrop", "dt-palette-list", "dt-pal-current",
];

/** What is probably wrong in a style sheet that is still valid: rules for classes the desktop does
 *  not have, and declarations a line break ended without `;` (the next line is then swallowed). Not
 *  a refusal, a hint for the person who reads the card. */
export function cssWarnings(css: string): { classes: string[]; lines: number[] } {
  const flat = css.replace(/\/\*[\s\S]*?\*\//g, "");
  const known = new Set(DT_CLASSES);
  const classes = [
    ...new Set(
      [...flat.matchAll(/\.(dt-[a-z0-9-]+)/gi)]
        .map((m) => m[1]!.toLowerCase())
        .filter((c) => !known.has(c) && !/^dt-(theme|look|bar)-/.test(c)),
    ),
  ].map((c) => `.${c}`);
  const lines: number[] = [];
  flat.split("\n").forEach((raw, i) => {
    const line = raw.trim();
    if (/^[a-z-][\w-]*\s*:[^{}]*$/i.test(line) && !/[;,{}]$/.test(line)) lines.push(i + 1);
  });
  return { classes, lines };
}
