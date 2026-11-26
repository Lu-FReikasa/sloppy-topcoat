// The web build keeps its own copy of the lists a theme is made of and of the style sheet rules; the
// hub keeps the authoritative ones (proto/ui/theme.schema.json, hub/.../ui/themes.py). These tests
// read the shared files, so the two sides cannot drift apart.
import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { describe, expect, it } from "vitest";
import {
  BAR_ITEMS,
  BASES,
  BAR_POSITIONS,
  BUILTIN_THEMES,
  CHROME_BUTTONS,
  DT_CLASSES,
  LAYOUT_KINDS,
  MOTION_STYLES,
  cssProblems,
  themeProblems,
} from "./theme";

const read = (name: string) => JSON.parse(readFileSync(resolve(process.cwd(), "../proto/ui", name), "utf8"));

interface Schema {
  properties: {
    base: { enum: string[] };
    layout: { properties: { kind: { enum: string[] } } };
    bar: { properties: { position: { enum: string[] }; items: { items: { enum: string[] } } } };
    chrome: { properties: { buttons: { items: { enum: string[] } }; titlebar: { enum: string[] } } };
    motion: { properties: { style: { enum: string[] }; ms: { maximum: number } } };
    css: { maxLength: number };
  };
}

describe("the theme lists of the web build and of the hub", () => {
  const schema = read("theme.schema.json") as Schema;

  it("are the same", () => {
    const p = schema.properties;
    expect([...LAYOUT_KINDS]).toEqual(p.layout.properties.kind.enum);
    expect([...BAR_POSITIONS]).toEqual(p.bar.properties.position.enum);
    expect([...BAR_ITEMS]).toEqual(p.bar.properties.items.items.enum);
    expect([...CHROME_BUTTONS]).toEqual(p.chrome.properties.buttons.items.enum);
    expect([...MOTION_STYLES]).toEqual(p.motion.properties.style.enum);
    expect([...BASES]).toEqual(p.base.enum);
    expect(p.chrome.properties.titlebar.enum).toEqual(["top", "hidden"]);
    expect(p.motion.properties.ms.maximum).toBe(2000);
    expect(p.css.maxLength).toBe(40000);
  });

  it("let the built-in themes pass the check of the hub's schema file as well", () => {
    for (const t of BUILTIN_THEMES) expect(themeProblems(t)).toEqual([]);
  });
});

describe("the style sheet rules of the web build", () => {
  const cases = read("theme-css-cases.json").cases as { css: string; ok: boolean }[];

  it.each(cases.map((c, i) => [i, c] as const))("agree with the shared case %i", (_i, c) => {
    expect(cssProblems(c.css).length === 0, c.css).toBe(c.ok);
  });
});

describe("the built-in themes", () => {
  it("are the keys the hub keeps from custom themes (BUILTIN in ui/themes.py)", () => {
    const py = readFileSync(resolve(process.cwd(), "../hub/src/topcoat_hub/ui/themes.py"), "utf8");
    const keys = /^BUILTIN = \(([^)]*)\)/m.exec(py)?.[1] ?? "";
    expect([...keys.matchAll(/"([^"]+)"/g)].map((m) => m[1])).toEqual(BUILTIN_THEMES.map((t) => t.key));
  });
});

describe("the classes a theme can reach", () => {
  it("are the ones the hub checks a theme against (proto/ui/theme-surface.json)", () => {
    expect([...DT_CLASSES]).toEqual(read("theme-surface.json").classes);
  });
});
