import { afterEach, describe, expect, it } from "vitest";
import { dwindle, loadTiles, ratioAt, saveTiles, setRatio, swap, tile, tileOf, untile, type Tiles } from "./dwindle";

const SCREEN = { x: 0, y: 0, w: 1, h: 1 };

afterEach(() => window.localStorage.clear());

describe("dwindle", () => {
  it("gives one window the screen, and each next one a part of what is left", () => {
    expect(dwindle(1, SCREEN, [], 16 / 9).rects).toEqual([SCREEN]);
    const two = dwindle(2, SCREEN, [], 16 / 9);
    // a wide screen splits side by side
    expect(two.rects).toEqual([
      { x: 0, y: 0, w: 0.5, h: 1 },
      { x: 0.5, y: 0, w: 0.5, h: 1 },
    ]);
    const three = dwindle(3, SCREEN, [], 16 / 9);
    // the right half (taller than wide on the screen) splits above each other
    expect(three.vertical).toEqual([true, false]);
    expect(three.rects[1]).toEqual({ x: 0.5, y: 0, w: 0.5, h: 0.5 });
    expect(three.rects[2]).toEqual({ x: 0.5, y: 0.5, w: 0.5, h: 0.5 });
  });

  it("follows a dragged divider, within limits", () => {
    const l = dwindle(2, SCREEN, [0.7], 16 / 9);
    expect(l.rects[0]!.w).toBeCloseTo(0.7);
    expect(ratioAt(l.areas[0]!, true, 0.95, 0.5)).toBe(0.85);
    expect(dwindle(2, SCREEN, [0.01], 16 / 9).rects[0]!.w).toBeCloseTo(0.15);
  });
});

describe("tile groups", () => {
  it("join, leave, swap and keep their ratios in step", () => {
    let t: Tiles = {};
    t = tile(t, "a", 0);
    t = tile(t, "b", 0);
    t = tile(t, "c", 1);
    expect(tileOf(t, "b")).toEqual({ screen: 0, index: 1 });
    t = setRatio(t, 0, 0, 0.6);
    t = tile(t, "d", 0, 1); // between a and b
    expect(t[0]).toEqual({ keys: ["a", "d", "b"], ratios: [0.6, 0.5, 0.5] });
    t = swap(t, "a", "c"); // across screens
    expect(t[0]!.keys[0]).toBe("c");
    expect(t[1]!.keys).toEqual(["a"]);
    t = untile(t, "a");
    expect(t[1]).toBeUndefined();
    t = tile(t, "c", 1); // moves from screen 0 to 1
    expect(t[0]!.keys).toEqual(["d", "b"]);
    expect(t[0]!.ratios).toHaveLength(2);
  });

  it("are kept in the web view and cleaned when read", () => {
    saveTiles({ 0: { keys: ["a"], ratios: [0.5] } });
    expect(loadTiles()).toEqual({ 0: { keys: ["a"], ratios: [0.5] } });
    window.localStorage.setItem("topcoat.overlay.tiles", JSON.stringify({ 0: { keys: [1] }, x: {} }));
    expect(loadTiles()).toEqual({});
  });
});
