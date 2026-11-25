import { fireEvent, render, screen } from "@testing-library/react";
import { useRef, useState } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { growHeight, MAX_LINES, MIN_LINES, useGrowingTextarea } from "./growTextarea";

// a box of 20 px lines with 8 px padding above and below and a 1 px border
const LINE = 20;
const PAD = 16;
const BORDER = 2;
const box = (lines: number): number => lines * LINE + PAD + BORDER;
const measured = (lines: number) => ({ scrollHeight: lines * LINE + PAD, lineHeight: LINE, padding: PAD, border: BORDER });

// lines of text typed -> lines the box shows, and whether it scrolls inside
const CASES: ReadonlyArray<readonly [typed: number, shown: number, scrolls: boolean]> = [
  [0, MIN_LINES, false],
  [1, MIN_LINES, false],
  [MIN_LINES, MIN_LINES, false],
  [4, 4, false],
  [7, 7, false],
  [MAX_LINES, MAX_LINES, false],
  [MAX_LINES + 1, MAX_LINES, true],
  [30, MAX_LINES, true],
];

describe("growHeight", () => {
  it.each(CASES)("%i lines typed: the box shows %i lines (scrolls: %s)", (typed, shown, scrolls) => {
    expect(growHeight(measured(typed))).toEqual({ lines: shown, height: box(shown), scrolls });
  });

  it("shows fewer lines when the style allows less height (a low window), and scrolls", () => {
    const low = { ...measured(MAX_LINES), maxHeight: box(5) };
    expect(growHeight(low)).toEqual({ lines: 5, height: box(5), scrolls: true });
    expect(growHeight({ ...measured(4), maxHeight: box(5) })).toEqual({ lines: 4, height: box(4), scrolls: false });
  });

  it("does not count a rounding pixel as a line", () => {
    const fourAndABit = { ...measured(4), scrollHeight: measured(4).scrollHeight + 0.6 };
    expect(growHeight(fourAndABit).lines).toBe(4);
  });
});

function Box() {
  const [text, setText] = useState("");
  const ref = useRef<HTMLTextAreaElement>(null);
  useGrowingTextarea(ref, text);
  return <textarea ref={ref} aria-label="message" value={text} onChange={(e) => setText(e.target.value)} />;
}

describe("useGrowingTextarea", () => {
  afterEach(() => vi.restoreAllMocks());

  it("sizes the box to its text while typing, from 3 to 10 lines, then scrolls inside", () => {
    // jsdom has no layout: the box reports one line per line of text, plus its padding.
    // The split accepts LF and CRLF ("a\nb" and "a\r\nb" are two lines each).
    vi.spyOn(HTMLTextAreaElement.prototype, "scrollHeight", "get").mockImplementation(function (this: HTMLTextAreaElement) {
      return this.value.split(/\r?\n/).length * LINE + PAD;
    });
    const style = { lineHeight: `${LINE}px`, fontSize: "16px", paddingTop: "8px", paddingBottom: "8px", borderTopWidth: "1px", borderBottomWidth: "1px" };
    const real = window.getComputedStyle;
    vi.spyOn(window, "getComputedStyle").mockImplementation((el, pseudo) =>
      el instanceof HTMLTextAreaElement ? ({ ...real(el, pseudo), ...style } as CSSStyleDeclaration) : real(el, pseudo),
    );

    render(<Box />);
    const el = screen.getByLabelText("message") as HTMLTextAreaElement;
    expect(el.style.height).toBe(`${box(MIN_LINES)}px`);

    // typing more, then deleting again: the box follows both ways
    const typing = [1, 6, 14, 2].map((lines) => {
      fireEvent.change(el, { target: { value: Array.from({ length: lines }, (_, i) => `line ${i + 1}`).join("\n") } });
      return [el.style.height, el.style.overflowY];
    });
    expect(typing).toEqual([
      [`${box(MIN_LINES)}px`, "hidden"],
      [`${box(6)}px`, "hidden"],
      [`${box(MAX_LINES)}px`, "auto"],
      [`${box(MIN_LINES)}px`, "hidden"],
    ]);
  });
});
