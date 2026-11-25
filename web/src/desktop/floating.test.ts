import { afterEach, describe, expect, it } from "vitest";
import { barInset, firstBox, loadBoxes, MIN, moveBox, onScreens, pinHref, saveBoxes, screenOf, screensOf, sizeBox, snapLines, snapMove, snapSize, WHOLE, zoneAt } from "./floating";

const b = { x: 0.2, y: 0.2, w: 0.4, h: 0.4 };

describe("free windows of the overlay", () => {
  afterEach(() => window.localStorage.clear());

  it("moves a box and keeps it on the stage", () => {
    expect(moveBox(b, 0.1, -0.1)).toEqual({ x: 0.30000000000000004, y: 0.1, w: 0.4, h: 0.4 });
    expect(moveBox(b, 5, 5)).toEqual({ x: 0.6, y: 0.6, w: 0.4, h: 0.4 });
    expect(moveBox(b, -5, -5)).toEqual({ x: 0, y: 0, w: 0.4, h: 0.4 });
  });

  it("sizes at an edge while the opposite edge stays", () => {
    const right = sizeBox(b, "e", 0.1, 0);
    expect(right.x).toBe(0.2);
    expect(right.w).toBeCloseTo(0.5);
    const left = sizeBox(b, "w", -0.1, 0);
    expect(left.x).toBeCloseTo(0.1);
    expect(left.x + left.w).toBeCloseTo(0.6);
    const top = sizeBox(b, "nw", 0, 0.5); // pushed past the bottom: stops at the smallest height
    expect(top.h).toBeCloseTo(MIN.h);
    expect(top.y + top.h).toBeCloseTo(0.6);
  });

  it("never sizes a box out of the stage or below the smallest size", () => {
    const big = sizeBox(b, "se", 5, 5);
    expect(big.x + big.w).toBeCloseTo(1);
    expect(big.y + big.h).toBeCloseTo(1);
    expect(sizeBox(b, "se", -5, -5)).toMatchObject({ w: MIN.w, h: MIN.h });
  });

  it("cascades new boxes and keeps only real boxes from storage", () => {
    expect(firstBox(1).x).toBeGreaterThan(firstBox(0).x);
    saveBoxes({ dev: b });
    window.localStorage.setItem(
      "topcoat.overlay.boxes",
      JSON.stringify({ dev: b, bad: { x: "1" }, huge: { x: 0.9, y: 0.9, w: 0.5, h: 0.5 } }),
    );
    const got = loadBoxes();
    expect(got.dev).toEqual(b);
    expect(got.bad).toBeUndefined();
    expect((got.huge?.x ?? 9) + (got.huge?.w ?? 9)).toBeLessThanOrEqual(1);
    window.localStorage.setItem("topcoat.overlay.boxes", "{not json");
    expect(loadBoxes()).toEqual({});
  });

  it("writes the pin into the solo address before its hash", () => {
    expect(pinHref("?solo=dev#/w/dev", { left: 10.4, top: 20, width: 600, height: 400.6 })).toBe(
      "?solo=dev&pin=10,20,600,401#/w/dev",
    );
  });

  // two screens side by side, the right one smaller and lower (a dead corner top right)
  const left = { x: 0, y: 0, w: 0.6, h: 1 };
  const right = { x: 0.6, y: 0.3, w: 0.4, h: 0.7 };

  it("reads the screens the client writes, primary first, and falls back to one", () => {
    expect(screensOf("?overlay=1&screens=0,0,0.6,1;0.6,0.3,0.4,0.7")).toEqual([left, right]);
    expect(screensOf("?overlay=1")).toEqual([WHOLE]);
    for (const bad of ["0,0,1", "a,0,1,1", "0,0,0,1", "0.5,0,0.6,1", "-1,0,1,1"]) {
      expect(screensOf(`?screens=${bad}`)).toEqual([WHOLE]);
    }
  });

  it("knows the screen a window stands on by its title bar", () => {
    expect(screenOf({ x: 0.5, y: 0.5, w: 0.3, h: 0.3 }, [left, right])).toBe(right);
    expect(screenOf({ x: 0.1, y: 0.1, w: 0.3, h: 0.3 }, [left, right])).toBe(left);
  });

  it("brings a window let go in the dead corner onto the nearest screen", () => {
    const lost = { x: 0.7, y: 0.05, w: 0.2, h: 0.3 };
    // the left screen is nearer (0.2 across) than the right one (0.24 down)
    const back = onScreens(lost, [left, right]);
    expect(back.x).toBeCloseTo(0.4);
    expect(back.y).toBeCloseTo(0.05);
    const low = onScreens({ x: 0.75, y: 0.2, w: 0.2, h: 0.3 }, [left, right]);
    expect(low.y).toBeCloseTo(0.3);
    // one that stands across both screens with its title bar on one stays where it is
    const across = { x: 0.5, y: 0.4, w: 0.3, h: 0.3 };
    expect(onScreens(across, [left, right])).toBe(across);
  });

  it("cascades a new window on the primary screen", () => {
    const b = firstBox(0, right);
    expect(b.x).toBeGreaterThanOrEqual(right.x);
    expect(b.x + b.w).toBeLessThanOrEqual(right.x + right.w);
    expect(b.y).toBeGreaterThanOrEqual(right.y);
  });
});

describe("snapping", () => {
  const screens = [{ x: 0, y: 0, w: 1, h: 1 }];
  const other = { x: 0.5, y: 0.2, w: 0.3, h: 0.3 };
  const lines = snapLines([other], screens);

  it("pulls a moved window onto a screen edge or another window's edge within the threshold", () => {
    // left edge 0.008 from the screen's edge: lands on it
    expect(snapMove({ x: 0.008, y: 0.4, w: 0.2, h: 0.2 }, lines, 0.01, 0.01).x).toBe(0);
    // right edge 0.495 near the other window's left edge 0.5
    expect(snapMove({ x: 0.295, y: 0.6, w: 0.2, h: 0.2 }, lines, 0.01, 0.01).x).toBeCloseTo(0.3);
    // too far: stays
    expect(snapMove({ x: 0.1, y: 0.4, w: 0.2, h: 0.2 }, lines, 0.01, 0.01).x).toBe(0.1);
  });

  it("pulls a sized edge onto a line, not the opposite edge", () => {
    const b = snapSize({ x: 0.1, y: 0.1, w: 0.395, h: 0.3 }, "e", lines, 0.01, 0.01);
    expect(b.x).toBe(0.1);
    expect(b.x + b.w).toBeCloseTo(0.5);
  });

  it("knows the zones at a screen's edges, the corners first", () => {
    const two = [
      { x: 0, y: 0, w: 0.6, h: 1 },
      { x: 0.6, y: 0, w: 0.4, h: 1 },
    ];
    expect(zoneAt(0.001, 0.5, two, 0.005, 0.005)?.zone).toBe("left");
    expect(zoneAt(0.001, 0.5, two, 0.005, 0.005)?.box).toEqual({ x: 0, y: 0, w: 0.3, h: 1 });
    expect(zoneAt(0.3, 0.001, two, 0.005, 0.005)?.zone).toBe("top");
    expect(zoneAt(0.001, 0.01, two, 0.005, 0.005)?.zone).toBe("tl");
    // the right edge of the left screen is the left edge strip of the right screen's neighbour
    expect(zoneAt(0.599, 0.5, two, 0.005, 0.005)?.zone).toBe("right");
    expect(zoneAt(0.999, 0.999, two, 0.005, 0.005)?.box).toEqual({ x: 0.8, y: 0.5, w: 0.2, h: 0.5 });
    expect(zoneAt(0.3, 0.5, two, 0.005, 0.005)).toBeNull();
  });
});

describe("barInset", () => {
  const root = { left: 0, top: 0, right: 1000, bottom: 600, width: 1000, height: 600 };
  it("takes the taskbar's height from the bottom, or from the side it stands on", () => {
    // live 2026-11-20: the windows reached under the bar and Clipper at their foot was cut off
    const bottom = { left: 0, top: 565, right: 1000, bottom: 600, width: 1000, height: 35 };
    expect(barInset(bottom, root)).toEqual({ top: 0, right: 0, bottom: 35, left: 0 });
    const top = { left: 0, top: 0, right: 1000, bottom: 35, width: 1000, height: 35 };
    expect(barInset(top, root)).toEqual({ top: 35, right: 0, bottom: 0, left: 0 });
    const left = { left: 0, top: 0, right: 192, bottom: 600, width: 192, height: 600 };
    expect(barInset(left, root)).toEqual({ top: 0, right: 0, bottom: 0, left: 192 });
  });
  it("takes nothing for a hidden bar", () => {
    expect(barInset(null, root)).toEqual({ top: 0, right: 0, bottom: 0, left: 0 });
    expect(barInset({ left: 0, top: 0, right: 0, bottom: 0, width: 0, height: 0 }, root).bottom).toBe(0);
  });
});
