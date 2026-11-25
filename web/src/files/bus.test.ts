import { afterEach, describe, expect, it, vi } from "vitest";
import { INTENT_EVENT, takeIntent } from "../layout/intent";
import { SHOW_EVENT, requestShow, showFromInput, takeShow } from "./bus";

afterEach(() => {
  takeShow();
  takeIntent();
});

describe("requestShow", () => {
  it("keeps the request for the reader, asks to open it and says so", () => {
    const opened = vi.fn();
    const shown = vi.fn();
    window.addEventListener(INTENT_EVENT, opened);
    window.addEventListener(SHOW_EVENT, shown);
    requestShow({ path: "hub/src/a.py", from: 3, to: 9 });
    window.removeEventListener(INTENT_EVENT, opened);
    window.removeEventListener(SHOW_EVENT, shown);
    expect(opened).toHaveBeenCalledTimes(1);
    expect(shown).toHaveBeenCalledTimes(1);
    expect(takeIntent()?.module).toBe("page.files");
    expect(takeShow()).toEqual({ path: "hub/src/a.py", from: 3, to: 9 });
    expect(takeShow()).toBeNull();
  });
});

describe("showFromInput", () => {
  it("reads what the assistant asked for and cleans the path", () => {
    expect(
      showFromInput({
        repo: "r",
        path: "\\hub\\src/a.py",
        from: 3,
        to: 9,
        find: "def f",
      }),
    ).toEqual({
      repo: "r",
      path: "hub/src/a.py",
      from: 3,
      to: 9,
      find: "def f",
    });
  });

  it("ignores what is no request or no line number", () => {
    expect(showFromInput(null)).toBeNull();
    expect(showFromInput({ repo: "r" })).toBeNull();
    expect(showFromInput({ path: "  " })).toBeNull();
    const r = showFromInput({ path: "a.py", from: 0, to: "9", find: "" });
    expect(r).toEqual({
      repo: undefined,
      path: "a.py",
      from: undefined,
      to: undefined,
      find: undefined,
    });
  });
});
