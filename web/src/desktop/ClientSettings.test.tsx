import { act, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import "../i18n";
import { ClientSettings } from "./ClientSettings";

type Global = { topcoatClient?: { config?: object; onResult?: (r: object) => void } };

afterEach(() => {
  delete (window as unknown as Global).topcoatClient;
  vi.restoreAllMocks();
});

describe("ClientSettings", () => {
  it("is not there in a browser", () => {
    render(<ClientSettings />);
    expect(screen.queryByTestId("client-settings")).toBeNull();
  });

  it("records a new key and shows the client's answer", () => {
    (window as unknown as Global).topcoatClient = {
      config: { version: "0.2.0", key: "ctrl+alt+Space", effect: "blur" },
    };
    const assign = vi.fn();
    vi.spyOn(window, "location", "get").mockReturnValue({ ...window.location, assign } as Location);
    render(<ClientSettings />);
    const [button, allButton, clipperButton] = screen.getAllByRole("button", { pressed: false });
    fireEvent.click(button!);
    // a modifier alone is not a key yet
    fireEvent.keyDown(button!, { key: "Control", code: "ControlLeft", ctrlKey: true });
    expect(assign).not.toHaveBeenCalled();
    // AltGr+C
    fireEvent.keyDown(button!, { key: "c", code: "KeyC", ctrlKey: true, altKey: true });
    expect(assign).toHaveBeenCalledWith("?client=set-key&slot=overlay&key=ctrl%2Balt%2BKeyC");
    // the second row records the key for every screen (AltGr+F by default)
    fireEvent.click(allButton!);
    fireEvent.keyDown(allButton!, { key: "g", code: "KeyG", ctrlKey: true, altKey: true });
    expect(assign).toHaveBeenLastCalledWith("?client=set-key&slot=all&key=ctrl%2Balt%2BKeyG");
    // the third one Clipper's
    fireEvent.click(clipperButton!);
    fireEvent.keyDown(clipperButton!, { key: "j", code: "KeyJ", ctrlKey: true, altKey: true });
    expect(assign).toHaveBeenLastCalledWith("?client=set-key&slot=clipper&key=ctrl%2Balt%2BKeyJ");

    const g = (window as unknown as Global).topcoatClient!;
    g.config = { ...g.config, key: "ctrl+alt+KeyC", clipperKey: "ctrl+alt+KeyJ" };
    act(() => g.onResult?.({ what: "key", ok: true, error: null }));
    expect(screen.getByRole("status").className).toContain("ok");
    expect(screen.getByText(/\+C$/)).toBeTruthy();
  });
});
