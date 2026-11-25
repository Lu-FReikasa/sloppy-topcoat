import { afterEach, describe, expect, it, vi } from "vitest";
import { INTENT_EVENT, takeIntent } from "../layout/intent";
import { COMPOSE_EVENT, requestCompose, takeCompose } from "./compose";

afterEach(() => {
  takeCompose("dev");
  takeCompose("chat");
  takeIntent();
  vi.restoreAllMocks();
});

describe("requestCompose", () => {
  it("keeps the text for the program until its chat takes it, once", () => {
    requestCompose("dev", "Erste Frage");
    requestCompose("dev", "Zweite Frage");
    expect(takeCompose("chat")).toBeNull(); // another program's chat gets nothing
    expect(takeCompose("dev")).toBe("Erste Frage\nZweite Frage");
    expect(takeCompose("dev")).toBeNull();
  });

  it("asks the desktop to open that program, and tells a chat that is already open", () => {
    const opened = vi.fn();
    const composed = vi.fn();
    window.addEventListener(INTENT_EVENT, opened);
    window.addEventListener(COMPOSE_EVENT, composed);
    requestCompose("dev", "Frage");
    window.removeEventListener(INTENT_EVENT, opened);
    window.removeEventListener(COMPOSE_EVENT, composed);
    expect(opened).toHaveBeenCalledTimes(1);
    expect(composed).toHaveBeenCalledTimes(1);
    expect(takeIntent()).toEqual({
      module: "page.chat",
      props: {},
      program: "dev",
    });
  });
});
