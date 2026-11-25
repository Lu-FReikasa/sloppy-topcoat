// Unit tests of the pure parts of the workshop browser (E6-02): the audit measurements and the
// snapshot run in jsdom with a stubbed layout (jsdom has none), the rest is plain functions.
import { beforeEach, describe, expect, it } from "vitest";
import { auditInPage } from "./audit-page.mjs";
import { CHECKS, DEFAULT_MATRIX, LIMITS, runAudit, summarise, trimResult } from "./audit.mjs";
import { snapshotInPage, treeDiff, treeLines } from "./snapshot-page.mjs";

globalThis.CSS ??= { escape: (s) => s };

// every element is 100x50 unless it says data-box="x,y,w,h"
Element.prototype.getBoundingClientRect = function () {
  const [x, y, w, h] = (this.getAttribute?.("data-box") ?? "0,0,100,50").split(",").map(Number);
  return { x, y, left: x, top: y, width: w, height: h, right: x + w, bottom: y + h, toJSON: () => ({}) };
};

const opts = { minTarget: LIMITS.minTarget, minFont: LIMITS.minFont, minContrast: LIMITS.minContrast, maxPerCheck: 8 };
const audit = (checks, html) => {
  document.body.innerHTML = html;
  return auditInPage({ checks, opts });
};

describe("the matrix and the answer", () => {
  it("covers phone, tablet and desktop in both orientations, within the capability's limits", () => {
    expect(DEFAULT_MATRIX.some((m) => m.w === 320)).toBe(true);
    expect(DEFAULT_MATRIX.some((m) => m.w === 2560)).toBe(true);
    expect(DEFAULT_MATRIX.some((m) => m.w > m.h)).toBe(true);
    expect(DEFAULT_MATRIX.some((m) => m.w < m.h)).toBe(true);
    for (const m of DEFAULT_MATRIX) {
      expect(m.w).toBeGreaterThanOrEqual(200);
      expect(m.h).toBeLessThanOrEqual(4000);
    }
    expect(DEFAULT_MATRIX.length).toBeLessThanOrEqual(40);
  });

  it("offers the nine checks the capability's schema names", () => {
    expect(CHECKS).toEqual([
      "page_scroll", "inner_scroll", "clipped_text", "overlap", "target_size", "font_size",
      "contrast", "focus", "reduced_motion",
    ]);
  });

  it("keeps only sizes with violations and never more than the cap", () => {
    const v = (n) => Array.from({ length: n }, (_, i) => ({ check: "target_size", selector: `b${i}` }));
    const sizes = [
      { w: 320, h: 640, violations: v(5), counts: { target_size: 5 } },
      { w: 768, h: 1024, violations: [], counts: {} },
      { w: 1280, h: 720, violations: v(5), counts: { target_size: 5 } },
    ];
    const out = trimResult(sizes, 7);
    expect(out.sizes.map((s) => s.w)).toEqual([320, 1280]);
    expect(out.sizes.reduce((n, s) => n + s.violations.length, 0)).toBe(7);
    expect(out.dropped).toBe(3);
    expect(summarise(sizes)).toEqual({ target_size: 10 });
  });
});

describe("the measurements", () => {
  it("finds click targets under 40 px and no others", () => {
    const { violations } = audit(["target_size"], `<button data-box="0,0,20,20">a</button><button data-box="0,60,44,44">b</button>`);
    expect(violations).toHaveLength(1);
    expect(violations[0]).toMatchObject({ check: "target_size", width: 20, height: 20, min: 40 });
    expect(violations[0].selector).toContain("button");
  });

  it("measures a checkbox or a radio by its label when the label is larger", () => {
    const { violations } = audit(
      ["target_size"],
      `<label data-box="0,0,120,44"><input type="checkbox" data-box="4,14,16,16"> big label</label>` +
        `<label data-box="0,60,30,20"><input type="radio" id="r" data-box="4,62,16,16"> small</label>` +
        `<input type="checkbox" data-box="0,100,16,16" aria-label="no label">`,
    );
    expect(violations.map((v) => [v.width, v.height])).toEqual([[30, 20], [16, 16]]);
  });

  it("passes a checkbox that is large enough itself, even beside a wide, low label", () => {
    const { violations } = audit(
      ["target_size"],
      `<input type="checkbox" id="c" data-box="0,0,44,44"><label for="c" data-box="50,12,200,20">a long label</label>`,
    );
    expect(violations).toEqual([]);
  });

  it("lets a link inside a sentence keep the line's height, but not a link on its own", () => {
    const { violations } = audit(
      ["target_size"],
      `<p>See the <a href="#a" data-box="0,0,60,18">models page</a> for more.</p>` +
        `<p><a href="#b" data-box="0,40,60,18">alone</a></p>`,
    );
    expect(violations.map((v) => [v.width, v.height])).toEqual([[60, 18]]);
    expect(violations[0].selector).toContain("a");
  });

  it("leaves out what sits in a closed fold, but not its summary", () => {
    const { violations } = audit(
      ["target_size"],
      `<details><summary data-box="0,0,60,20">Options</summary><button data-box="0,30,20,20">hidden</button></details>` +
        `<details open><summary data-box="0,60,60,44">Open</summary><button data-box="0,110,20,20">shown</button></details>`,
    );
    expect(violations.map((v) => [v.width, v.height])).toEqual([[60, 20], [20, 20]]);
  });

  it("finds interactive elements that overlap, but not one inside another", () => {
    const { violations } = audit(
      ["overlap"],
      `<button data-box="0,0,100,50">a</button><button data-box="50,10,100,50">b</button>
       <a href="#" data-box="0,200,100,50">c<button data-box="10,210,20,20">d</button></a>`,
    );
    expect(violations).toHaveLength(1);
    expect(violations[0]).toMatchObject({ check: "overlap", overlap_width: 50, overlap_height: 40 });
  });

  it("measures the contrast of text against the background it sits on", () => {
    const { violations } = audit(
      ["contrast"],
      `<div style="background-color: rgb(255, 255, 255)"><p style="color: rgb(153, 153, 153)">pale</p><p style="color: rgb(0, 0, 0)">black</p></div>`,
    );
    expect(violations).toHaveLength(1);
    expect(violations[0].ratio).toBeCloseTo(2.85, 1);
    expect(violations[0].need).toBe(4.5);
  });

  it("asks less of large text", () => {
    const { violations } = audit(
      ["contrast"],
      `<div style="background-color: rgb(255, 255, 255)"><p style="color: rgb(119, 119, 119); font-size: 30px">big</p></div>`,
    );
    expect(violations).toHaveLength(0); // 4.48:1 passes at 3:1
  });

  it("flags text under the minimum size", () => {
    const { violations } = audit(["font_size"], `<p style="font-size: 10px">tiny</p><p style="font-size: 16px">fine</p>`);
    expect(violations).toHaveLength(1);
    expect(violations[0]).toMatchObject({ check: "font_size", font_size: 10, min: 12 });
  });

  it("flags a page that scrolls sideways", () => {
    Object.defineProperty(document.documentElement, "scrollWidth", { value: 500, configurable: true });
    Object.defineProperty(window, "innerWidth", { value: 400, configurable: true });
    const { violations } = audit(["page_scroll"], "<p>x</p>");
    expect(violations).toEqual([expect.objectContaining({ check: "page_scroll", overflow_x: 100, viewport: 400 })]);
    delete document.documentElement.scrollWidth;
  });

  it("flags a control without a visible focus indication", () => {
    const { violations } = audit(
      ["focus"],
      `<button style="outline-style: none" data-box="0,0,50,50">no ring</button>
       <button style="outline-style: solid; outline-width: 2px" data-box="0,100,50,50">ring</button>`,
    );
    expect(violations.filter((v) => v.problem === "no visible focus indication")).toHaveLength(1);
  });

  it("flags a positive tabindex", () => {
    const { violations } = audit(["focus"], `<button tabindex="3" style="outline-style: solid; outline-width: 2px">x</button>`);
    expect(violations.map((v) => v.problem)).toContain("positive tabindex changes the focus order");
  });

  it("caps the violations it reports per check but counts them all", () => {
    const html = Array.from({ length: 12 }, () => `<button data-box="0,0,10,10">x</button>`).join("");
    const { violations, counts } = audit(["target_size"], html);
    expect(violations).toHaveLength(8);
    expect(counts.target_size).toBe(12);
  });

  it("reports nothing for a page that is fine", () => {
    const { violations } = audit(
      ["target_size", "font_size", "contrast", "overlap"],
      `<div style="background-color: rgb(255, 255, 255)"><button data-box="0,0,60,44" style="color: rgb(0, 0, 0)">ok</button></div>`,
    );
    expect(violations).toEqual([]);
  });
});

describe("the snapshot", () => {
  beforeEach(() => {
    window.__tcRefs = undefined;
    window.__tcOf = undefined;
    window.__tcN = undefined;
  });
  const page = `<main><h1>Title</h1><nav aria-label="Main"><a href="/a">Home</a></nav>
    <label for="q">Search</label><input id="q" type="text" value="hello">
    <button disabled>Send</button><div hidden><button>Hidden</button></div>
    <input type="password" value="secret"><ul><li>One</li><li>Two</li></ul></main>`;

  it("lists what a user can perceive and operate, with refs, names and states", () => {
    document.body.innerHTML = page;
    const snap = snapshotInPage({ maxNodes: 100 });
    const byName = Object.fromEntries(snap.nodes.map((n) => [`${n.role}:${n.name}`, n]));
    expect(byName["heading:Title"].state).toEqual({ level: 1 });
    expect(byName["navigation:Main"]).toBeDefined();
    expect(byName["link:Home"].ref).toMatch(/^e\d+$/);
    expect(byName["textbox:Search"].state).toEqual({ value: "hello" });
    expect(byName["button:Send"].state).toEqual({ disabled: true });
    expect(snap.nodes.some((n) => n.name === "Hidden")).toBe(false);
    expect(JSON.stringify(snap)).not.toContain("secret"); // a password's value is never in the tree
  });

  it("keeps a ref for an element and finds the subtree of a ref", () => {
    document.body.innerHTML = page;
    const first = snapshotInPage({ maxNodes: 100 });
    const nav = first.nodes.find((n) => n.role === "navigation");
    const again = snapshotInPage({ maxNodes: 100 });
    expect(again.nodes.find((n) => n.role === "navigation").ref).toBe(nav.ref);
    const sub = snapshotInPage({ scope: nav.ref, maxNodes: 10 });
    expect(sub.nodes.map((n) => n.role)).toEqual(["navigation", "link"]);
  });

  it("says how many nodes it left out, and adds boxes when asked", () => {
    document.body.innerHTML = page;
    const snap = snapshotInPage({ maxNodes: 3, layout: true });
    expect(snap.shown).toBe(3);
    expect(snap.omitted).toBeGreaterThan(0);
    expect(snap.nodes[0].box).toEqual([0, 0, 100, 50]);
  });

  it("refuses a ref that no element has", () => {
    document.body.innerHTML = page;
    expect(() => snapshotInPage({ scope: "e999", maxNodes: 10 })).toThrow("no element has the ref e999");
  });

  it("compares two trees by what they say", () => {
    const a = treeLines([{ role: "button", name: "Save" }, { role: "text", name: "Old" }]);
    const b = treeLines([{ role: "button", name: "Save" }, { role: "dialog", name: "Confirm", state: { expanded: true } }]);
    const diff = treeDiff(a, b);
    expect(diff.added).toEqual(['dialog|Confirm|{"expanded":true}']);
    expect(diff.removed).toEqual(["text|Old"]);
    expect(diff.more).toBe(0);
  });
});

describe("the check names", () => {
  it("refuses a name it does not know instead of dropping it", async () => {
    await expect(runAudit({}, { checks: ["page_scroll", "targets"] }, async () => undefined)).rejects.toThrow(
      /unknown check: targets/,
    );
  });
});
