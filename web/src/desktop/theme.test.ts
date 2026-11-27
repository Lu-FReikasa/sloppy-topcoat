import { describe, expect, it } from "vitest";
import {
  BUILTIN_THEMES,
  LAYOUT_KINDS,
  allThemes,
  cssProblems,
  layoutRects,
  leaveMs,
  resolveTheme,
  rootSelectors,
  safeCss,
  cssWarnings,
  DT_CLASSES,
  splitKeyframes,
  themeProblems,
  type ThemeManifest,
} from "./theme";

const CUSTOM: ThemeManifest = {
  key: "wall",
  name: "Wall",
  base: "terminal",
  layout: { kind: "columns" },
  bar: { position: "left", items: ["programs", "status"] },
  chrome: { buttons: ["close"], titlebar: "top" },
  motion: { style: "slide", ms: 200 },
};

describe("the layout engines", () => {
  it.each(LAYOUT_KINDS)("%s puts every program inside the stage, for one to six programs", (kind) => {
    for (let n = 1; n <= 6; n += 1) {
      const rects = layoutRects(kind, n);
      expect(rects).toHaveLength(n);
      for (const r of rects) {
        expect(r.x).toBeGreaterThanOrEqual(0);
        expect(r.y).toBeGreaterThanOrEqual(0);
        expect(r.x + r.w).toBeLessThanOrEqual(1.0001);
        expect(r.y + r.h).toBeLessThanOrEqual(1.0001);
        expect(r.w).toBeGreaterThan(0.1);
        expect(r.h).toBeGreaterThan(0.1);
      }
    }
    expect(layoutRects(kind, 0)).toEqual([]);
  });

  it("keeps programs apart in every engine but the cascade, which overlaps them", () => {
    const apart = (kind: Parameters<typeof layoutRects>[0], n: number) => {
      const r = layoutRects(kind, n);
      for (let i = 0; i < r.length; i += 1)
        for (let j = i + 1; j < r.length; j += 1) {
          const a = r[i]!;
          const b = r[j]!;
          const overlap =
            a.x < b.x + b.w - 1e-9 && b.x < a.x + a.w - 1e-9 && a.y < b.y + b.h - 1e-9 && b.y < a.y + a.h - 1e-9;
          if (overlap) return false;
        }
      return true;
    };
    for (const kind of ["tile", "columns", "rows", "focus"] as const) expect(apart(kind, 4), kind).toBe(true);
    expect(apart("cascade", 3)).toBe(false);
  });

  it("gives the first program the large place in 'focus'", () => {
    const [main, ...side] = layoutRects("focus", 4);
    expect(main!.w).toBeGreaterThan(0.6);
    expect(side).toHaveLength(3);
    expect(side.every((r) => r.x >= main!.w - 1e-9)).toBe(true);
  });
});

describe("themes", () => {
  it("come with four built-in ones that pass their own check", () => {
    expect(BUILTIN_THEMES.map((t) => t.key)).toEqual(["terminal", "leitstand", "schreibtisch", "terminal-nest"]);
    for (const t of BUILTIN_THEMES) expect(themeProblems(t), t.key).toEqual([]);
  });

  it("include Terminal Nest: the nest layout on the terminal, its style sheet within a theme's limits", () => {
    const nest = resolveTheme("terminal-nest");
    expect(nest).toMatchObject({ base: "terminal", layout: { kind: "nest" }, bar: { position: "top" } });
    expect(cssProblems(nest.css ?? "")).toEqual([]);
    // every rule reaches something the desktop draws, and no line swallows the next
    expect(cssWarnings(nest.css ?? "")).toEqual({ classes: [], lines: [] });
    // scoped to its own root like a custom theme's
    expect(safeCss(nest)).toContain(".dt-theme-terminal-nest:is(.dt-root, #dt-theme) {");
    // a built-in theme is not a base: a custom theme cannot start from it
    expect(themeProblems({ ...nest, key: "mine", base: "terminal-nest" })).toEqual(["base: one of terminal, leitstand, schreibtisch"]);
  });

  it("resolve to a custom theme, a built-in one, or the terminal", () => {
    expect(resolveTheme("wall", [CUSTOM])).toBe(CUSTOM);
    expect(resolveTheme("leitstand", [CUSTOM]).key).toBe("leitstand");
    expect(resolveTheme("gone", [CUSTOM]).key).toBe("terminal");
    expect(resolveTheme(null).key).toBe("terminal");
    // a custom theme cannot take the key of a built-in one over in the list
    expect(allThemes([{ ...CUSTOM, key: "terminal" }, CUSTOM]).map((t) => t.key)).toEqual([
      "terminal",
      "leitstand",
      "schreibtisch",
      "terminal-nest",
      "wall",
    ]);
  });

  it("name what is wrong with a document", () => {
    expect(themeProblems(CUSTOM)).toEqual([]);
    expect(themeProblems(null)).toHaveLength(1);
    const bad = {
      ...CUSTOM,
      key: "Bad Key",
      layout: { kind: "spiral" },
      bar: { position: "middle", items: ["clock", "clock", "nothing"] },
      chrome: { buttons: ["close", "close"], titlebar: "side" },
      motion: { style: "spin", ms: 5000 },
      base: "elsewhere",
    };
    const problems = themeProblems(bad).join("\n");
    for (const part of [
      "key:",
      "base:",
      "layout.kind",
      "bar.position",
      "bar.items",
      "chrome.buttons",
      "chrome.titlebar",
      "motion.style",
      "motion.ms",
    ])
      expect(problems, part).toContain(part);
  });
});

describe("the style sheet of a theme", () => {
  it("may restyle with variables, rules, media queries and keyframes", () => {
    const css =
      "--accent: hotpink;\n.dt-program { border-radius: 8px; }\n@media (max-width: 600px) { .dt-task { padding: 0; } }\n@keyframes wob { from { opacity: 0 } to { opacity: 1 } }";
    expect(cssProblems(css)).toEqual([]);
  });

  it.each([
    ["@import url(https://evil.example/x.css);", "@import"],
    [".a { background: url(https://evil.example/x.png); }", "url()"],
    [".a { background: url('//evil.example/x.png'); }", "url()"],
    [".a { width: expression(alert(1)); }", "script-like"],
    [".a { background: url(javascript:alert(1)); }", "url()"],
    [".a { -moz-binding: url(x); }", "script-like"],
    ["@im\\port 'x';", "@import"],
    ["/* hidden */ @import 'x';", "@import"],
  ])("refuses %s", (css, why) => {
    expect(cssProblems(css).join(" ")).toContain(why);
  });

  it("allows an inline image and refuses a long sheet", () => {
    expect(cssProblems(".a { background: url(data:image/png;base64,AAAA); }")).toEqual([]);
    expect(cssProblems("a{}".repeat(20000)).join(" ")).toContain("at most");
  });

  it("is limited to the root of the theme, with the keyframes outside", () => {
    const css = ".dt-task { color: red; }\n@keyframes wob { from { opacity: 0 } to { opacity: 1 } }\n--accent: red;";
    const out = safeCss({ ...CUSTOM, css });
    expect(out.startsWith("@keyframes wob")).toBe(true);
    expect(out).toContain(".dt-theme-wall:is(.dt-root, #dt-theme) {");
    expect(out).toContain(".dt-task { color: red; }");
    expect(out.indexOf("@keyframes")).toBeLessThan(out.indexOf(".dt-theme-wall:is("));
    expect(splitKeyframes(css).rest).not.toContain("keyframes");
  });

  it("does not let moving the keyframes out join two halves of a name", () => {
    const css = ".a { background: ur@keyframes k { from { opacity: 0 } }l(https://evil.example/x.png); }";
    expect(cssProblems(css)).toEqual([]);
    const { rest } = splitKeyframes(css);
    expect(cssProblems(rest)).toEqual([]);
    expect(safeCss({ ...CUSTOM, css })).not.toMatch(/url\(/i);
  });

  it("lets :root mean the root of the theme (the variables a model writes there apply)", () => {
    const out = safeCss({ ...CUSTOM, css: ":root { --bg: #000; } .dt-task { color: red; }" });
    expect(out).toContain("& { --bg: #000; }");
    expect(out).not.toContain(":root");
  });

  it("lets the root's own selectors mean the root (live 2026-11-15: none of them was drawn)", () => {
    const css = [
      ".dt-theme-wall { --tb: blue; }",
      ".dt-root { background: green; }",
      ".dt-root.dt-bar-top .dt-taskbar { color: red; }",
      ".dt-bar-left .dt-taskbar, .dt-solo .dt-stage { color: red; }",
      ".dt-theme-wall-x .dt-task { color: red; }",
    ].join("\n");
    const out = rootSelectors(css, "wall");
    expect(out).toContain("& { --tb: blue; }");
    expect(out).toContain("& { background: green; }");
    expect(out).toContain("&.dt-bar-top .dt-taskbar");
    expect(out).toContain("&.dt-bar-left .dt-taskbar, &.dt-solo .dt-stage");
    // another theme's key is not this root
    expect(out).toContain(".dt-theme-wall-x .dt-task");
  });

  it("is dropped as a whole when it is not safe", () => {
    expect(safeCss({ ...CUSTOM, css: "@import 'x';" })).toBe("");
    expect(safeCss(CUSTOM)).toBe("");
  });
});

describe("hints for a style sheet that is valid but probably not what was meant", () => {
  it("names classes the desktop does not have", () => {
    const w = cssWarnings(".dt-window { color: red; } .dt-task:hover { color: red; } .dt-button {}");
    expect(w.classes).toEqual([".dt-window", ".dt-button"]);
    // the root's own classes and the window buttons are drawn
    expect(cssWarnings(".dt-theme-x { } .dt-bar-top .dt-close, .dt-minimise, .dt-maximise {}").classes).toEqual([]);
  });

  it("finds a declaration that a line break ended without a semicolon", () => {
    const css = "--bg: #000;\n--surface: #111\n--text: #fff\n.dt-task {\ncolor: red;\n}";
    expect(cssWarnings(css).lines).toEqual([2, 3]);
    expect(cssWarnings("--bg: #000; .dt-task { color: red; }")).toEqual({ classes: [], lines: [] });
  });

  it("knows every class the desktop style sheets use", async () => {
    const { readFileSync } = await import("node:fs");
    const css = readFileSync("src/desktop/desktop.css", "utf8");
    const used = new Set(
      [...css.matchAll(/\.(dt-[a-z0-9-]+)/g)]
        .map((m) => m[1]!)
        .filter((c) => !/^dt-(look|theme|bar)-|^dt-ms$/.test(c)),
    );
    expect([...used].filter((c) => !DT_CLASSES.includes(c))).toEqual([]);
  });
});

describe("barItems", () => {
  it("keeps the name menu in every theme: a bar without it gets it at the end", async () => {
    const { barItems } = await import("./theme");
    expect(barItems({ bar: { position: "bottom", items: ["start", "programs", "clock"] } })).toEqual([
      "start",
      "programs",
      "clock",
      "me",
    ]);
    expect(barItems({ bar: { position: "top", items: ["me", "programs"] } })).toEqual(["me", "programs"]);
  });
});

describe("leaveMs", () => {
  const base = { ...CUSTOM, motion: { style: "fade" as const, ms: 240 } };

  it("waits the theme's length when it animates the leaving, by preset or by its own rule", () => {
    expect(leaveMs(base, "close", false)).toBe(0);
    expect(leaveMs({ ...base, motion: { ...base.motion, close: "zoom" } }, "close", false)).toBe(240);
    expect(leaveMs({ ...base, css: ".dt-program.dt-closing { animation: x 1s; }" }, "close", false)).toBe(240);
    expect(leaveMs({ ...base, motion: { ...base.motion, overlay: "slide" } }, "overlay", false)).toBe(240);
    expect(leaveMs({ ...base, css: ".dt-leaving { opacity: 0; }" }, "overlay", false)).toBe(240);
  });

  it("waits for nothing when the person asked for less motion", () => {
    expect(leaveMs({ ...base, motion: { ...base.motion, close: "zoom" } }, "close", true)).toBe(0);
  });

  it("takes the new fields in the check and refuses an unknown one", () => {
    expect(themeProblems({ ...base, motion: { ...base.motion, close: "zoom", overlay: "fade" } })).toEqual([]);
    expect(themeProblems({ ...base, motion: { ...base.motion, close: "spin" as never } }).join()).toContain("motion.close");
  });
});
