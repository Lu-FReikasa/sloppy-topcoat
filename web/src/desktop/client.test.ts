import { afterEach, describe, expect, it, vi } from "vitest";
import { accelerator, clientConfig, installEscape, installOverlayMotion, keyLabel } from "./client";

const key = (code: string, mods: Partial<Record<"ctrlKey" | "altKey" | "shiftKey" | "metaKey", boolean>> = {}) => ({
  code,
  ctrlKey: false,
  altKey: false,
  shiftKey: false,
  metaKey: false,
  ...mods,
});

afterEach(() => {
  delete (window as unknown as { topcoatClient?: unknown }).topcoatClient;
  document.body.innerHTML = "";
});

describe("accelerator", () => {
  it("writes a key press as the client registers it", () => {
    expect(accelerator(key("Space", { ctrlKey: true, altKey: true }))).toBe("ctrl+alt+Space");
    // AltGr+C on Windows
    expect(accelerator(key("KeyC", { ctrlKey: true, altKey: true }))).toBe("ctrl+alt+KeyC");
    expect(accelerator(key("F5", { metaKey: true, shiftKey: true }))).toBe("shift+super+F5");
  });

  it("is no key without ctrl, alt or super, or with only a modifier", () => {
    expect(accelerator(key("KeyC"))).toBeNull();
    expect(accelerator(key("KeyC", { shiftKey: true }))).toBeNull();
    expect(accelerator(key("ControlLeft", { ctrlKey: true }))).toBeNull();
    expect(accelerator(key("Minus", { ctrlKey: true }))).toBeNull();
  });
});

describe("keyLabel", () => {
  it("reads the key in the person's words", () => {
    const t = (k: string) => ({ "client.mod.ctrl": "Strg", "client.mod.alt": "Alt", "client.key.space": "Leertaste" })[k] ?? k;
    expect(keyLabel("ctrl+alt+Space", t)).toBe("Strg+Alt+Leertaste");
    expect(keyLabel("ctrl+alt+KeyC", t)).toBe("Strg+Alt+C");
  });
});

describe("the client's Esc", () => {
  it("is nothing in a browser", () => {
    expect(clientConfig()).toBeNull();
    installEscape()();
  });

  it("closes what is open first, else asks the client to hide", () => {
    (window as unknown as { topcoatClient: object }).topcoatClient = { config: { key: "ctrl+alt+Space" } };
    const assign = vi.fn();
    vi.spyOn(window, "location", "get").mockReturnValue({ ...window.location, assign } as Location);
    const stop = installEscape();
    const escape = (window as unknown as { topcoatClient: { escape: () => void } }).topcoatClient.escape;

    const dialog = document.createElement("div");
    dialog.setAttribute("aria-modal", "true");
    document.body.append(dialog);
    const pressed = vi.fn();
    document.addEventListener("keydown", pressed);
    escape();
    expect(pressed).toHaveBeenCalledTimes(1);
    expect(assign).not.toHaveBeenCalled();

    dialog.remove();
    escape();
    expect(assign).toHaveBeenCalledWith("?client=hide");
    document.removeEventListener("keydown", pressed);
    stop();
  });

  it("closes an open row menu before the overlay, and a closed one does not count (#211)", () => {
    (window as unknown as { topcoatClient: object }).topcoatClient = { config: { key: "ctrl+alt+Space" } };
    const assign = vi.fn();
    vi.spyOn(window, "location", "get").mockReturnValue({ ...window.location, assign } as Location);
    const stop = installEscape();
    const escape = (window as unknown as { topcoatClient: { escape: () => void } }).topcoatClient.escape;

    const menu = document.createElement("details");
    menu.setAttribute("data-esc-open", "");
    menu.innerHTML = "<summary>⋯</summary><button>Load</button>";
    menu.open = true;
    document.body.append(menu);
    // the focus is elsewhere: the menu closes, the overlay stays
    escape();
    expect(menu.open).toBe(false);
    expect(assign).not.toHaveBeenCalled();
    // closed, it no longer holds the key back
    escape();
    expect(assign).toHaveBeenCalledWith("?client=hide");
    menu.remove();
    stop();
  });
});

describe("installOverlayMotion", () => {
  it("plays the arrival when shown and the leaving before it asks to hide", () => {
    vi.useFakeTimers();
    const c: { onShow?: () => void; leave?: () => void; escape?: () => void } = {};
    (window as unknown as { topcoatClient: object }).topcoatClient = c;
    const assign = vi.fn();
    vi.spyOn(window, "location", "get").mockReturnValue({ ...window.location, assign } as Location);
    const marks: (string | null)[] = [];
    const stop = installOverlayMotion(() => 200, (m) => marks.push(m));
    c.onShow!();
    expect(marks).toEqual(["arriving"]);
    vi.advanceTimersByTime(200);
    expect(marks.at(-1)).toBeNull();
    // Esc with nothing open goes through the leaving
    const noEsc = installEscape();
    c.escape!();
    c.leave!(); // a second press while it goes changes nothing
    expect(marks.at(-1)).toBe("leaving");
    expect(assign).not.toHaveBeenCalled();
    vi.advanceTimersByTime(200);
    expect(assign).toHaveBeenCalledTimes(1);
    expect(String(assign.mock.calls[0]![0])).toContain("client=hide");
    // a moment after the hide the class is gone: an older client that never calls onShow
    // cannot show an overlay that stayed faded out
    vi.advanceTimersByTime(200);
    expect(marks.at(-1)).toBeNull();
    noEsc();
    stop();
    expect(c.leave).toBeUndefined();
    vi.useRealTimers();
  });

  it("goes at once when the theme does not animate it", () => {
    const c: { leave?: () => void } = {};
    (window as unknown as { topcoatClient: object }).topcoatClient = c;
    const assign = vi.fn();
    vi.spyOn(window, "location", "get").mockReturnValue({ ...window.location, assign } as Location);
    vi.useFakeTimers();
    const marks: (string | null)[] = [];
    installOverlayMotion(() => 0, (m) => marks.push(m))();
    installOverlayMotion(() => 0, (m) => marks.push(m));
    c.leave!();
    vi.advanceTimersByTime(0);
    expect(marks[0]).toBeNull();
    expect(assign).toHaveBeenCalledTimes(1);
    vi.useRealTimers();
  });
});
