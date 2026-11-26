import { afterEach, describe, expect, it } from "vitest";
import {
  addProgram,
  groupOf,
  keysOf,
  loadNests,
  moveDivider,
  moveProgram,
  normalise,
  overview,
  pathOf,
  rects,
  removeProgram,
  saveNests,
  setWeights,
  syncNest,
  validZoom,
  WEIGHT_MIN,
  zoomIn,
  zoomOut,
  type NestNode,
  type NestTree,
} from "./nest";

const leaf = (key: string): NestNode => ({ key });
const AREA = { x: 0, y: 0, w: 1600, h: 900 };
const NO_MIN = { w: 0, h: 0 };

/** a | (b / (c | d)): a on the left half; b above c and d on the right. */
const SAMPLE: NestTree = {
  axis: "x",
  children: [
    leaf("a"),
    {
      axis: "y",
      children: [leaf("b"), { axis: "x", children: [leaf("c"), leaf("d")], weights: [0.5, 0.5] }],
      weights: [0.5, 0.5],
    },
  ],
  weights: [0.5, 0.5],
};

const byKey = (t: NestTree, zoom: number[] = [], min = NO_MIN) =>
  Object.fromEntries(rects(t, AREA, zoom, min).map((p) => [p.key, p]));

afterEach(() => window.localStorage.clear());

describe("normalise", () => {
  it("collapses a split with one child and drops empty ones", () => {
    expect(normalise({ axis: "x", children: [leaf("a")], weights: [1] })).toEqual(leaf("a"));
    expect(normalise({ axis: "y", children: [], weights: [] })).toBeNull();
    expect(
      normalise({
        axis: "x",
        children: [{ axis: "y", children: [], weights: [] }, { axis: "y", children: [leaf("b")], weights: [3] }],
        weights: [1, 1],
      }),
    ).toEqual(leaf("b"));
  });

  it("flattens a split inside a split on the same axis, keeping the shares as drawn", () => {
    const t = normalise({
      axis: "x",
      children: [leaf("a"), { axis: "x", children: [leaf("b"), leaf("c")], weights: [1, 3] }],
      weights: [1, 1],
    });
    expect(t).toEqual({ axis: "x", children: [leaf("a"), leaf("b"), leaf("c")], weights: [0.5, 0.125, 0.375] });
  });

  it("keeps a split on the other axis and makes the weights sum to 1", () => {
    const t = normalise({
      axis: "x",
      children: [leaf("a"), { axis: "y", children: [leaf("b"), leaf("c")], weights: [2, 2] }],
      weights: [3, 1],
    });
    expect(t).toEqual({
      axis: "x",
      children: [leaf("a"), { axis: "y", children: [leaf("b"), leaf("c")], weights: [0.5, 0.5] }],
      weights: [0.75, 0.25],
    });
  });

  it("replaces a weight that is missing, zero or not a number by an equal share", () => {
    const t = normalise({ axis: "y", children: [leaf("a"), leaf("b")], weights: [0, Number.NaN] });
    expect(t).toEqual({ axis: "y", children: [leaf("a"), leaf("b")], weights: [0.5, 0.5] });
  });
});

describe("rects", () => {
  it("lays out the whole tree in the overview", () => {
    const r = byKey(SAMPLE);
    expect(r.a!.rect).toEqual({ x: 0, y: 0, w: 800, h: 900 });
    expect(r.b!.rect).toEqual({ x: 800, y: 0, w: 800, h: 450 });
    expect(r.c!.rect).toEqual({ x: 800, y: 450, w: 400, h: 450 });
    expect(r.d!.rect).toEqual({ x: 1200, y: 450, w: 400, h: 450 });
    expect(Object.values(r).every((p) => p.framed)).toBe(true);
    expect(rects(SAMPLE, AREA).map((p) => p.key)).toEqual(["a", "b", "c", "d"]);
  });

  it("frames a group at each level, the rest lying outside the area where it was", () => {
    // level 1: the right half fills the area
    const one = byKey(SAMPLE, [1]);
    expect(one.b!.rect).toEqual({ x: 0, y: 0, w: 1600, h: 450 });
    expect(one.c!.rect).toEqual({ x: 0, y: 450, w: 800, h: 450 });
    expect(one.a!.rect).toEqual({ x: -1600, y: 0, w: 1600, h: 900 });
    expect([one.a!.framed, one.b!.framed, one.c!.framed]).toEqual([false, true, true]);
    // level 2: c | d fills the area
    const two = byKey(SAMPLE, [1, 1]);
    expect(two.c!.rect).toEqual({ x: 0, y: 0, w: 800, h: 900 });
    expect(two.d!.rect).toEqual({ x: 800, y: 0, w: 800, h: 900 });
    expect(two.b!.rect.y).toBe(-900);
    expect(two.b!.framed).toBe(false);
    // level 3: one program alone
    const three = byKey(SAMPLE, [1, 1, 0]);
    expect(three.c!.rect).toEqual(AREA);
    expect(three.d!.framed).toBe(false);
    expect(three.d!.rect.x).toBe(1600);
  });

  it("honours the area's offset", () => {
    const r = byKey(SAMPLE);
    const moved = Object.fromEntries(rects(SAMPLE, { x: 100, y: 50, w: 1600, h: 900 }, [], NO_MIN).map((p) => [p.key, p]));
    expect(moved.d!.rect).toEqual({ ...r.d!.rect, x: r.d!.rect.x + 100, y: r.d!.rect.y + 50 });
  });

  it("scales a program that is smaller than its minimum, and not one that fits", () => {
    const min = { w: 480, h: 320 };
    const r = byKey(SAMPLE, [], min);
    expect(r.a!.scale).toBe(1);
    // c is 400 x 450: too narrow by 400 / 480
    expect(r.c!.scale).toBeCloseTo(400 / 480);
    // zoomed in, c is wide enough
    expect(byKey(SAMPLE, [1, 1], min).c!.scale).toBe(1);
    // the smaller side decides
    const tiny = rects(leaf("x"), { x: 0, y: 0, w: 240, h: 80 }, [], min)[0]!;
    expect(tiny.scale).toBeCloseTo(80 / 320);
  });

  it("is empty for an empty tree and ignores a zoom that leads nowhere", () => {
    expect(rects(null, AREA)).toEqual([]);
    expect(rects(SAMPLE, AREA, [7, 3], NO_MIN)).toEqual(rects(SAMPLE, AREA, [], NO_MIN));
    expect(validZoom(SAMPLE, [1, 1, 0, 4])).toEqual([1, 1, 0]);
    expect(validZoom(SAMPLE, [1, 9])).toEqual([1]);
  });
});

describe("zoom", () => {
  it("steps into the focused program's groups, out one level, and to the overview", () => {
    let z = overview();
    z = zoomIn(SAMPLE, z, "d");
    expect(z).toEqual([1]);
    z = zoomIn(SAMPLE, z, "d");
    expect(z).toEqual([1, 1]);
    z = zoomIn(SAMPLE, z, "d");
    expect(z).toEqual([1, 1, 1]); // the program itself
    expect(zoomIn(SAMPLE, z, "d")).toEqual([1, 1, 1]); // no deeper
    z = zoomOut(SAMPLE, z);
    expect(z).toEqual([1, 1]);
    expect(zoomOut(SAMPLE, [])).toEqual([]);
    expect(overview()).toEqual([]);
  });

  it("frames the focused program's own group when it lies outside the frame", () => {
    expect(zoomIn(SAMPLE, [1, 1], "a")).toEqual([]);
    expect(zoomIn(SAMPLE, [0], "c")).toEqual([1, 1]);
    expect(groupOf(SAMPLE, "b")).toEqual([1]);
    expect(groupOf(leaf("a"), "a")).toEqual([]);
    expect(zoomIn(SAMPLE, [], "nope")).toEqual([]);
  });
});

describe("changing the tree", () => {
  it("adds a program by splitting the focused one along its longer side", () => {
    let t: NestTree = null;
    t = addProgram(t, "a");
    expect(t).toEqual(leaf("a"));
    t = addProgram(t, "b", { focus: "a", aspect: 16 / 9 });
    // a wide screen: side by side
    expect(t).toEqual({ axis: "x", children: [leaf("a"), leaf("b")], weights: [0.5, 0.5] });
    t = addProgram(t, "c", { focus: "b", aspect: 16 / 9 });
    // b is taller than wide on the screen: above each other
    expect(t).toEqual({
      axis: "x",
      children: [leaf("a"), { axis: "y", children: [leaf("b"), leaf("c")], weights: [0.5, 0.5] }],
      weights: [0.5, 0.5],
    });
    // a tall screen splits the first program above each other
    expect(addProgram(leaf("a"), "b", { aspect: 9 / 16 })).toEqual({
      axis: "y",
      children: [leaf("a"), leaf("b")],
      weights: [0.5, 0.5],
    });
  });

  it("flattens when the new split runs the same way as its parent, and keeps a program only once", () => {
    const two: NestTree = { axis: "y", children: [leaf("a"), leaf("b")], weights: [0.5, 0.5] };
    // b is 0.2 x 0.5 on a narrow area: taller than wide, so it splits on y like its parent
    const t = addProgram(two, "c", { focus: "b", aspect: 0.2 });
    expect(t).toEqual({ axis: "y", children: [leaf("a"), leaf("b"), leaf("c")], weights: [0.5, 0.25, 0.25] });
    expect(addProgram(t, "a", { focus: "c" })).toBe(t);
  });

  it("splits the last program when the focused one is not in the tree", () => {
    const t = addProgram(SAMPLE, "e", { focus: "zz", aspect: 16 / 9 });
    // d (400 x 450) is split above each other
    expect(pathOf(t, "e")).toEqual([1, 1, 1, 1]);
    expect(pathOf(t, "d")).toEqual([1, 1, 1, 0]);
  });

  it("looks at the focused program as it is seen at the zoom", () => {
    // d is 400 x 450 in the overview (taller), 1600 x 900 when it is framed alone (wider)
    const seen = addProgram(SAMPLE, "e", { focus: "d", aspect: 16 / 9, zoom: [1, 1, 1] });
    expect(pathOf(seen, "e")).toEqual([1, 1, 2]); // flattened into c | d | e
    const overviewed = addProgram(SAMPLE, "e", { focus: "d", aspect: 16 / 9 });
    expect(pathOf(overviewed, "e")).toEqual([1, 1, 1, 1]); // d above e
  });

  it("removes a program and collapses what is left", () => {
    let t = removeProgram(SAMPLE, "c");
    expect(t).toEqual({
      axis: "x",
      children: [leaf("a"), { axis: "y", children: [leaf("b"), leaf("d")], weights: [0.5, 0.5] }],
      weights: [0.5, 0.5],
    });
    t = removeProgram(t, "b");
    expect(t).toEqual({ axis: "x", children: [leaf("a"), leaf("d")], weights: [0.5, 0.5] });
    t = removeProgram(removeProgram(t, "a"), "d");
    expect(t).toBeNull();
    expect(removeProgram(SAMPLE, "nope")).toBe(SAMPLE);
  });

  it("moves a program next to another", () => {
    // a goes to the right of d: into d's split (same axis)
    const right = moveProgram(SAMPLE, "a", "d", "right");
    expect(right).toEqual({
      axis: "y",
      children: [leaf("b"), { axis: "x", children: [leaf("c"), leaf("d"), leaf("a")], weights: [0.5, 0.25, 0.25] }],
      weights: [0.5, 0.5],
    });
    // b goes above a: a's place is split; without b, c | d joins the outer split (same axis)
    const top = moveProgram(SAMPLE, "b", "a", "top");
    expect(top).toEqual({
      axis: "x",
      children: [{ axis: "y", children: [leaf("b"), leaf("a")], weights: [0.5, 0.5] }, leaf("c"), leaf("d")],
      weights: [0.5, 0.25, 0.25],
    });
    expect(keysOf(moveProgram(SAMPLE, "d", "c", "left"))).toEqual(["a", "b", "d", "c"]);
    expect(moveProgram(SAMPLE, "a", "a", "left")).toBe(SAMPLE);
    expect(moveProgram(SAMPLE, "a", "zz", "left")).toBe(SAMPLE);
  });

  it("changes weights, keeping every share at the minimum or more", () => {
    const t = setWeights(SAMPLE, [], [3, 1]);
    expect(byKey(t).a!.rect.w).toBeCloseTo(1200);
    const tiny = setWeights(SAMPLE, [1, 1], [1, 0.001]);
    const w = (tiny as { children: { children: { weights: number[] }[] }[] }).children[1]!.children[1]!.weights;
    expect(w[1]).toBeCloseTo(WEIGHT_MIN);
    expect(w[0]! + w[1]!).toBeCloseTo(1);
    // wrong length, a leaf or bad numbers: unchanged
    expect(setWeights(SAMPLE, [], [1])).toBe(SAMPLE);
    expect(setWeights(SAMPLE, [0], [1, 1])).toBe(SAMPLE);
    expect(setWeights(SAMPLE, [], [1, -1])).toBe(SAMPLE);
  });

  it("moves a divider, changing only the two children beside it", () => {
    const three: NestTree = { axis: "x", children: [leaf("a"), leaf("b"), leaf("c")], weights: [0.25, 0.25, 0.5] };
    const t = moveDivider(three, [], 0, 0.4) as { weights: number[] };
    expect(t.weights[0]).toBeCloseTo(0.4);
    expect(t.weights[1]).toBeCloseTo(0.1);
    expect(t.weights[2]).toBeCloseTo(0.5);
    const far = moveDivider(three, [], 0, 0.99) as { weights: number[] };
    expect(far.weights[1]).toBeCloseTo(WEIGHT_MIN);
    expect(moveDivider(three, [], 2, 0.5)).toBe(three);
  });

  it("syncs the tree with the open programs", () => {
    const t = syncNest(SAMPLE, ["a", "d", "e"], { focus: "d", aspect: 16 / 9 });
    expect(keysOf(t).sort()).toEqual(["a", "d", "e"]);
    expect(syncNest(null, ["x", "y"])).toEqual({ axis: "x", children: [leaf("x"), leaf("y")], weights: [0.5, 0.5] });
    expect(syncNest(SAMPLE, [])).toBeNull();
  });
});

describe("keeping the nests", () => {
  it("round-trips per screen", () => {
    const nests = { 0: { tree: SAMPLE, zoom: [1, 1] }, 2: { tree: leaf("e"), zoom: [] } };
    saveNests(nests);
    expect(loadNests(() => true)).toEqual(nests);
  });

  it("drops programs that are not open, repeats and broken nodes, and trims the zoom", () => {
    saveNests({ 0: { tree: SAMPLE, zoom: [1, 1, 0] } });
    const open = new Set(["a", "b", "d"]);
    const got = loadNests((k) => open.has(k));
    expect(got[0]!.tree).toEqual({
      axis: "x",
      children: [leaf("a"), { axis: "y", children: [leaf("b"), leaf("d")], weights: [0.5, 0.5] }],
      weights: [0.5, 0.5],
    });
    // [1, 1] now leads to d, the third step leads nowhere
    expect(got[0]!.zoom).toEqual([1, 1]);

    window.localStorage.setItem(
      "topcoat.nest",
      JSON.stringify({
        0: { tree: { axis: "z", children: [] } },
        1: { tree: { axis: "x", children: [{ key: "a" }, { key: "a" }, 5, { key: 3 }], weights: ["x"] }, zoom: "no" },
        x: { tree: { key: "a" } },
      }),
    );
    expect(loadNests(() => true)).toEqual({ 1: { tree: leaf("a"), zoom: [] } });
    window.localStorage.setItem("topcoat.nest", "not json");
    expect(loadNests(() => true)).toEqual({});
  });
});
