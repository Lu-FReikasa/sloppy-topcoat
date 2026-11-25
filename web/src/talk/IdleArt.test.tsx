import { act, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { IdleArt, frame } from "./IdleArt";

describe("the idle picture", () => {
  beforeEach(() => vi.useFakeTimers());
  afterEach(() => {
    vi.useRealTimers();
    vi.restoreAllMocks();
  });

  it("has a fixed size and carries the mark and a blinking cursor", () => {
    const a = frame(0).split("\n");
    expect(a).toHaveLength(7);
    expect(new Set(a.map((l) => l.length))).toEqual(new Set([44]));
    expect(frame(0)).toContain("> tc_");
    expect(frame(1)).toContain("> tc ");
    expect(frame(1)).not.toContain("_");
  });

  it("is the same for the same moment and drifts between moments", () => {
    expect(frame(4)).toBe(frame(4));
    expect(frame(4)).not.toBe(frame(6));
  });

  it("moves while nothing is going on and is hidden from screen readers", () => {
    window.matchMedia = ((q: string) => ({
      matches: false,
      media: q,
      addEventListener() {},
      removeEventListener() {},
    })) as unknown as typeof window.matchMedia;
    render(<IdleArt />);
    const first = screen.getByTestId("idle-art");
    expect(first.getAttribute("aria-hidden")).toBe("true");
    const before = first.textContent;
    act(() => {
      vi.advanceTimersByTime(1500);
    });
    expect(screen.getByTestId("idle-art").textContent).not.toBe(before);
  });

  it("stands still when the person asked for less motion", () => {
    window.matchMedia = ((q: string) => ({
      matches: true,
      media: q,
      addEventListener() {},
      removeEventListener() {},
    })) as unknown as typeof window.matchMedia;
    render(<IdleArt />);
    const before = screen.getByTestId("idle-art").textContent;
    act(() => {
      vi.advanceTimersByTime(5000);
    });
    expect(screen.getByTestId("idle-art").textContent).toBe(before);
  });
});
