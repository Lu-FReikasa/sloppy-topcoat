// The text of each look against the background it really stands on, at least 4.5:1 (WCAG AA, #75):
// status badges (text on their fill), the application tags, the closed programs on the control
// room's taskbar, and the links and names on the desk's paper. The colours are read from the
// shipped style sheets, so a change of a token is measured here.
import { readFileSync } from "node:fs";
import { describe, expect, it } from "vitest";
import { resolveTheme } from "./theme";

const MIN = 4.5;

interface Rule {
  selector: string;
  /** The at-rule the rule sits in ("" at the top level). */
  within: string;
  decls: Record<string, string>;
}

// Block comments, which hold braces and colons we must not read: matches "/* a { b } */", not "a { b }".
const COMMENT = /\/\*[\s\S]*?\*\//g;
/** One brace of a block: matches "{" and "}", nothing else. */
const BRACE = /[{}]/g;
/** Runs of white space, folded to one space so selectors compare: matches "\n  ", not "a". */
const SPACE = /\s+/g;

/** The rules of a style sheet, with the at-rule each sits in. Enough for our own sheets. */
function parse(file: string): Rule[] {
  return parseCss(readFileSync(`src/${file}`, "utf8"));
}

/** The rules of style sheet text (a file, or the `css` of a built-in theme). */
function parseCss(text: string): Rule[] {
  const css = text.replace(COMMENT, "");
  const fold = (text: string) => text.trim().replace(SPACE, " ");
  const declarations = (body: string): Record<string, string> =>
    Object.fromEntries(
      body
        .split(";")
        .map((d) => [d.slice(0, d.indexOf(":")), d.slice(d.indexOf(":") + 1)] as const)
        .filter(([name]) => name.trim() !== "")
        .map(([name, value]) => [name.trim(), fold(value)]),
    );
  const walk = [...css.matchAll(BRACE)].reduce(
    (acc, m) => {
      const at = m.index;
      const text = css.slice(acc.start, at);
      if (m[0] === "{") return { ...acc, stack: [...acc.stack, fold(text)], start: at + 1 };
      const prelude = acc.stack.at(-1) ?? "";
      const stack = acc.stack.slice(0, -1);
      const within = stack.filter((s) => s.startsWith("@")).join(" ");
      const rules = prelude.startsWith("@")
        ? acc.rules
        : [...acc.rules, { selector: prelude, within, decls: declarations(text) }];
      return { rules, stack, start: at + 1 };
    },
    { rules: [] as Rule[], stack: [] as string[], start: 0 },
  );
  return walk.rules;
}

const SHEETS = Object.fromEntries(
  ["styles.css", "inbox/inbox.css", "chat-layout.css", "desktop/desktop.css", "desktop/leitstand.css", "desktop/schreibtisch.css"].map(
    (f) => [f, parse(f)],
  ),
);

/** The declarations of the top-level rules of `file` with exactly this selector, later ones winning. */
function decls(file: string, selector: string): Record<string, string> {
  const found = SHEETS[file]!.filter((r) => r.selector === selector && r.within === "");
  expect(found.length, `${selector} in ${file}`).toBeGreaterThan(0);
  return Object.assign({}, ...found.map((r) => r.decls));
}

type RGB = [number, number, number];
type Vars = Record<string, string>;

const CHANNEL_MAX = 255;
const PERCENT = 100;
/** How deep a variable may refer to another before we give up (a cycle would loop). */
const MAX_VAR_DEPTH = 10;
/** A `var()` with an optional plain fallback: matches "var(--ok)" and "var(--pg-hue, 140)", not "--ok". */
const VAR = /var\((--[\w-]+)(?:,\s*([^()]+))?\)/g;
/** The only calc() the looks use, a percentage times a factor: matches "calc(70% * 0.3)", not "calc(1px + 2px)". */
const CALC = /calc\(\s*([\d.]+)%\s*\*\s*([\d.]+)\s*\)/g;
/** A hex colour of three or six digits: matches "#fff" and "#1b2230", not "#ffff". */
const HEX = /^#([0-9a-f]{3}|[0-9a-f]{6})$/i;
/** hsl() with space or comma syntax, no alpha: matches "hsl(200 60% 24%)", not "hsl(200 60% 24% / 0.5)". */
const HSL = /^hsl\(\s*([\d.]+)[\s,]+([\d.]+)%[\s,]+([\d.]+)%\s*\)$/;

/** hsl to rgb, the CSS Color 4 formula. */
function fromHsl(h: number, s: number, l: number): RGB {
  const HUE_SECTOR = 30;
  const SECTORS = 12;
  const k = (n: number) => (n + h / HUE_SECTOR) % SECTORS;
  const a = s * Math.min(l, 1 - l);
  const f = (n: number) => l - a * Math.max(-1, Math.min(k(n) - 3, Math.min(9 - k(n), 1)));
  const RED = 0;
  const GREEN = 8;
  const BLUE = 4;
  return [f(RED), f(GREEN), f(BLUE)].map((x) => Math.round(x * CHANNEL_MAX)) as RGB;
}

/** A colour value with its variables filled in: hex, or hsl() (with calc() of a percentage). */
function colour(value: string, vars: Vars): RGB {
  const fill = (text: string, depth: number): string => {
    if (!text.includes("var(")) return text;
    if (depth >= MAX_VAR_DEPTH) throw new Error(`variables nest too deep in ${value}`);
    const once = text.replace(VAR, (_, name: string, fallback?: string) => {
      const got = vars[name] ?? fallback;
      if (got === undefined) throw new Error(`${name} is not defined (in ${value})`);
      return got;
    });
    return fill(once, depth + 1);
  };
  const v = fill(value, 0).replace(CALC, (_, a: string, b: string) => `${Number(a) * Number(b)}%`);
  const hex = HEX.exec(v);
  if (hex) {
    const digits = hex[1]!;
    const six = digits.length === 3 ? [...digits].map((c) => c + c).join("") : digits;
    return [0, 2, 4].map((i) => parseInt(six.slice(i, i + 2), 16)) as RGB;
  }
  const hsl = HSL.exec(v);
  if (hsl) return fromHsl(Number(hsl[1]), Number(hsl[2]) / PERCENT, Number(hsl[3]) / PERCENT);
  throw new Error(`cannot read the colour ${value} (${v})`);
}

/** Relative luminance and contrast ratio as WCAG 2 defines them. */
const WCAG = { knee: 0.03928, slope: 12.92, offset: 0.055, scale: 1.055, gamma: 2.4, flare: 0.05 };
const WEIGHTS: RGB = [0.2126, 0.7152, 0.0722];

function luminance(rgb: RGB): number {
  const linear = (c: number) => {
    const x = c / CHANNEL_MAX;
    return x <= WCAG.knee ? x / WCAG.slope : ((x + WCAG.offset) / WCAG.scale) ** WCAG.gamma;
  };
  return rgb.reduce((sum, c, i) => sum + WEIGHTS[i]! * linear(c), 0);
}

function ratio(a: RGB, b: RGB): number {
  const [x, y] = [luminance(a), luminance(b)].sort((p, q) => q - p) as [number, number];
  return (x + WCAG.flare) / (y + WCAG.flare);
}

/** The custom properties in force inside a look, as the cascade gives them (later wins). */
function looksVars(look: "terminal" | "leitstand" | "schreibtisch", hue: number): Vars {
  return {
    "--ws-hue": String(hue),
    ...decls("styles.css", ":root"),
    ...decls("inbox/inbox.css", ":root"),
    ...decls("desktop/desktop.css", ".dt-root"),
    ...(look === "terminal" ? {} : decls(`desktop/${look}.css`, `.dt-look-${look}`)),
  };
}

/** The person's hue moves the surfaces of the terminal and the desk: every 15 degrees is measured. */
const HUE_STEP = 15;
const FULL_CIRCLE = 360;
const HUES = Array.from({ length: FULL_CIRCLE / HUE_STEP }, (_, i) => i * HUE_STEP);
/** Every hue a program can get (`programHue` hashes unknown keys onto the whole circle). */
const ALL_HUES = Array.from({ length: FULL_CIRCLE }, (_, h) => h);
/** The default hue (`Desktop.tsx`), also used for the parts that do not depend on it. */
const ANY_HUE = 140;
const MIN_TAG_PX = 12;
const APPS = ["chat", "research", "dev", "admin", "project"];
/** A badge's fill and text, as `styles.css` pairs them. */
interface Badge {
  name: string;
  fill: string;
  text: string;
}
/** The fills of the status badges (ok, warn, off): light in the dark looks, dark inks on paper. */
const SIGNAL_FILLS = ["var(--ok)", "var(--warn)", "var(--off)"];
/** The badges on the accent fill take the accent's own text colour, like a primary button. The
 *  terminal's accent follows the person's hue, and around blue (220-260) it stays under 4.5:1 with
 *  any text colour: a matter of the accent, so these are measured at the default hue only. */
const ACCENT_FILL = "var(--accent)";
/** Every badge rule of `styles.css` on one of these fills, found by its background so a new badge is
 *  measured without being listed here; its text is its own colour or the one `.badge` gives. */
function badges(fills: string[]): Badge[] {
  const base = decls("styles.css", ".badge").color!;
  return SHEETS["styles.css"]!
    .filter((r) => r.within === "" && r.selector.includes("badge") && fills.includes(r.decls.background ?? ""))
    .map((r) => {
      const d = decls("styles.css", r.selector);
      return { name: r.selector, fill: d.background!, text: d.color ?? base };
    });
}
const SIGNAL_BADGES = badges(SIGNAL_FILLS);
const BADGES = [...SIGNAL_BADGES, ...badges([ACCENT_FILL])];
/** The unread count on the navigation, urgent: the count's text on the red of `.ws-badge-urgent`. */
function urgentCount(): Badge {
  return {
    name: ".nav-badge.ws-badge-urgent",
    fill: decls("styles.css", ".ws-badge-urgent").background!,
    text: decls("styles.css", ".nav-badge").color!,
  };
}

function worst(pairs: { what: string; fg: RGB; bg: RGB }[]) {
  return pairs
    .map((p) => ({ what: p.what, ratio: Math.round(ratio(p.fg, p.bg) * PERCENT) / PERCENT }))
    .filter((p) => p.ratio < MIN);
}

describe.each(["terminal", "leitstand", "schreibtisch"] as const)("the %s look on its dark ground", (look) => {
  it("puts readable text on every status badge", () => {
    const pairs = HUES.flatMap((hue) => {
      const vars = looksVars(look, hue);
      return [...(hue === ANY_HUE ? BADGES : SIGNAL_BADGES), urgentCount()].map((b) => ({
        what: `${b.name} hue ${hue}`,
        fg: colour(b.text, vars),
        bg: colour(b.fill, vars),
      }));
    });
    expect(worst(pairs)).toEqual([]);
  });

  it("puts readable text on a task's filled application badge", () => {
    expect(worst(writeBadges(looksVars(look, ANY_HUE), look))).toEqual([]);
  });

  it("shows every application tag readable on the surface and the ground", () => {
    const pairs = HUES.flatMap((hue) => {
      const vars = looksVars(look, hue);
      return APPS.flatMap((app) =>
        ["--surface", "--bg"].map((ground) => ({
          what: `${app} on ${ground} hue ${hue}`,
          fg: colour(`var(--app-${app})`, vars),
          bg: colour(`var(${ground})`, vars),
        })),
      );
    });
    expect(worst(pairs)).toEqual([]);
  });
});

/** A task that may propose changes wears its application's colour as a fill (`AppBadge`). */
function writeBadges(vars: Vars, what: string) {
  const d = decls("styles.css", ".app-badge.is-write");
  return APPS.map((app) => ({
    what: `${app} write badge ${what}`,
    fg: colour(d.color!, vars),
    bg: colour(d.background!, { ...vars, "--app-color": `var(--app-${app})` }),
  }));
}

describe("the status badges", () => {
  it("are found by their fill, the unread count among them", () => {
    expect(BADGES.map((b) => b.name)).toEqual(
      expect.arrayContaining([".badge-available", ".badge-degraded", ".badge-offline", ".badge-busy_gaming", ".nav-badge"]),
    );
  });

  it("are not faded by an offline machine's row", () => {
    // opacity fades the "Offline" badge's fill and text together: 3.3:1 at 0.75 in the dark looks
    expect(decls("styles.css", ".state-offline").opacity).toBeUndefined();
  });
});

describe("the application tags", () => {
  it("are at least 12 px and take the button's text in the chosen filter", () => {
    const size = decls("inbox/inbox.css", ".app-tag")["font-size"]!;
    const root = parseFloat(decls("styles.css", ":root")["font-size"]!);
    expect(parseFloat(size) * (size.endsWith("rem") ? root : 1)).toBeGreaterThanOrEqual(MIN_TAG_PX);
    expect(decls("inbox/inbox.css", ".inbox-tabs button.primary .app-tag")["--app-color"]).toBe("currentColor");
  });
});

describe("the control room's taskbar", () => {
  it("keeps the legend of a closed program readable on its dark key", () => {
    const vars = looksVars("leitstand", ANY_HUE);
    const key = decls("desktop/leitstand.css", ".dt-look-leitstand .dt-task");
    const minimised = decls("desktop/leitstand.css", ".dt-look-leitstand .dt-task.dt-min");
    const bg = colour(key.background!, vars);
    expect(ratio(colour(key.color!, vars), bg)).toBeGreaterThanOrEqual(MIN);
    expect(ratio(colour(minimised.color!, vars), bg)).toBeGreaterThanOrEqual(MIN);
  });
});

/** The rules that give a slip its paper: `.dt-look-schreibtisch .dt-program[data-paper="legal"]`. */
const PAPER_RULE = '.dt-look-schreibtisch .dt-program[data-paper="';
/** The opaque colours in a paper's background: matches "#efe398", not "rgba(60, 100, 160, 0.2)". */
const PAPER_HEX = /#[0-9a-f]{6}\b/gi;

describe("the desk's paper", () => {
  const sheet = "desktop/schreibtisch.css";
  const slip = decls(sheet, ".dt-look-schreibtisch .dt-program");
  /** Every paper a slip can be: the default one and each `data-paper` (both stripes of green-bar). */
  const papers: [string, RGB][] = [
    ["default", colour(slip["--paper"]!, {})],
    ...SHEETS[sheet]!.filter((r) => r.selector.startsWith(PAPER_RULE) && r.decls.background).flatMap((r) =>
      [...r.decls.background!.matchAll(PAPER_HEX)].map(
        (m) => [`${r.selector.slice(PAPER_RULE.length).split('"')[0]} ${m[0]}`, colour(m[0], {})] as [string, RGB],
      ),
    ),
  ];
  const vars = { ...looksVars("schreibtisch", ANY_HUE), ...slip };

  it("knows every paper", () => {
    expect(papers.map(([name]) => name.split(" ")[0])).toEqual(
      expect.arrayContaining(["default", "legal", "plain", "card", "ledger", "carbon", "green"]),
    );
  });

  it("puts readable text on every status badge", () => {
    const pairs = [...BADGES, urgentCount()].map((b) => ({ what: b.name, fg: colour(b.text, vars), bg: colour(b.fill, vars) }));
    expect(worst(pairs)).toEqual([]);
  });

  it("writes the application tags in inks readable on every paper", () => {
    const pairs = papers.flatMap(([name, paper]) =>
      APPS.map((app) => ({ what: `${app} on ${name}`, fg: colour(`var(--app-${app})`, vars), bg: paper })),
    );
    expect(worst(pairs)).toEqual([]);
  });

  it("puts readable text on a task's filled application badge", () => {
    expect(worst(writeBadges(vars, "on paper"))).toEqual([]);
  });

  it("writes the link in a chat window's bar in the look's ink, readable on every paper", () => {
    const link = colour(decls(sheet, ".dt-look-schreibtisch .dt-program .chat-window[data-app] .talk-head-actions .linklike").color!, vars);
    const pairs = papers.map(([name, paper]) => ({ what: `link on ${name}`, fg: link, bg: paper }));
    expect(worst(pairs)).toEqual([]);
  });

  it("writes the grey lettering (rail labels, table headings) readable on every paper", () => {
    const muted = colour("var(--muted)", vars);
    expect(worst(papers.map(([name, paper]) => ({ what: `muted on ${name}`, fg: muted, bg: paper })))).toEqual([]);
  });

  it("writes an application's name in its hue as an ink readable on every paper, whatever the hue", () => {
    const name = decls(sheet, ".dt-look-schreibtisch .dt-program .open-chats-app, .dt-look-schreibtisch .dt-program .chat-origin-app");
    const pairs = ALL_HUES.flatMap((hue) =>
      papers.map(([paper, bg]) => ({
        what: `hue ${hue} on ${paper}`,
        fg: colour(name.color!, { ...vars, "--pg-hue": String(hue) }),
        bg,
      })),
    );
    expect(worst(pairs)).toEqual([]);
  });
});

describe("Terminal Nest", () => {
  const rules = parseCss(resolveTheme("terminal-nest").css ?? "");
  const rule = (selector: string) => {
    const found = rules.filter((r) => r.selector === selector && r.within === "");
    expect(found.length, selector).toBeGreaterThan(0);
    return Object.assign({}, ...found.map((r) => r.decls)) as Record<string, string>;
  };

  it("puts readable text on the filled title line and taskbar entry of the program in front, whatever the hues", () => {
    // the fill is the program's hue (any hue: programHue hashes unknown keys), the text follows the person's hue
    const pairs = [".dt-focused .dt-titlebar", ".dt-task.dt-front, .dt-task.dt-front .dt-icon"].flatMap((selector) => {
      const d = rule(selector);
      return HUES.flatMap((hue) => {
        const vars = looksVars("terminal", hue);
        return ALL_HUES.map((pg) => ({
          what: `${selector} program hue ${pg} hue ${hue}`,
          fg: colour(d.color!, vars),
          bg: colour(d.background!, { ...vars, "--pg-hue": String(pg) }),
        }));
      });
    });
    expect(worst(pairs)).toEqual([]);
  });
});

/** The system's palette outside the looks: the light `:root` and its dark twin under the media query. */
const DARK_SCHEME = "@media (prefers-color-scheme: dark)";

describe("the system palette outside the looks", () => {
  const light = decls("styles.css", ":root");
  const darkRules = SHEETS["styles.css"]!.filter((r) => r.selector === ":root" && r.within === DARK_SCHEME);
  const dark: Vars = { ...light, ...Object.assign({}, ...darkRules.map((r) => r.decls)) };

  it("writes error text readable on the dark ground and surface (#69)", () => {
    const pairs = ["--bg", "--surface"].map((ground) => ({
      what: `--bad on ${ground}`,
      fg: colour("var(--bad)", dark),
      bg: colour(`var(${ground})`, dark),
    }));
    expect(darkRules.length).toBeGreaterThan(0);
    expect(worst(pairs)).toEqual([]);
  });

  it("keeps the light theme's error red", () => {
    expect(light["--bad"]).toBe("#c53030");
  });
});
