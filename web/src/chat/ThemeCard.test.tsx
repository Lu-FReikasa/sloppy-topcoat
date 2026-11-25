import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { ApiClient } from "../api/client";
import { previewTheme } from "../desktop/preview";
import { Markdown } from "./Markdown";
import { ThemeCardContext, normaliseTheme, parseTheme } from "./ThemeCard";

const THEME = {
  key: "dusk",
  name: "Dusk",
  base: "terminal",
  layout: { kind: "columns" },
  bar: { position: "left", items: ["programs", "me"] },
  chrome: { buttons: ["close"], titlebar: "top" },
  motion: { style: "fade", ms: 150 },
  css: "--accent: #c60;",
};
const fence = (doc: unknown) => "Here it is:\n\n```json\n" + JSON.stringify(doc, null, 2) + "\n```";

afterEach(() => previewTheme(null));

function withApi(text: string, putTheme = vi.fn().mockResolvedValue({})) {
  const api = { putTheme } as unknown as ApiClient;
  render(
    <ThemeCardContext.Provider value={api}>
      <Markdown text={text} />
    </ThemeCardContext.Provider>,
  );
  return putTheme;
}

describe("theme card in an answer", () => {
  it("stores the theme only on the click, with the document as written", async () => {
    const put = withApi(fence(THEME));
    expect(put).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole("button", { name: /Theme anlegen/ }));
    await waitFor(() => expect(put).toHaveBeenCalledWith("dusk", THEME));
    expect(await screen.findByRole("status")).toBeTruthy();
  });

  it("shows the problems of an invalid theme and offers no click", () => {
    withApi(fence({ ...THEME, layout: { kind: "spiral" } }));
    expect(screen.getByRole("alert").textContent).toContain("layout.kind");
    expect(screen.queryByRole("button", { name: /Theme anlegen/ })).toBeNull();
  });

  it("previews on this desktop without storing, and ends the preview", async () => {
    const put = withApi(fence(THEME));
    const seen: unknown[] = [];
    const { usePreviewTheme } = await import("../desktop/preview");
    function Probe() {
      seen.push(usePreviewTheme()?.key ?? null);
      return null;
    }
    render(<Probe />);
    fireEvent.click(screen.getByRole("button", { name: "Vorschau" }));
    await waitFor(() => expect(seen.at(-1)).toBe("dusk"));
    fireEvent.click(screen.getByRole("button", { name: "Vorschau beenden" }));
    await waitFor(() => expect(seen.at(-1)).toBeNull());
    expect(put).not.toHaveBeenCalled();
  });

  it("is no card for other json or without an api", () => {
    const { container } = render(<Markdown text={fence({ a: 1 })} />);
    expect(container.querySelector("[data-testid=theme-card]")).toBeNull();
    const { container: c2 } = render(<Markdown text={fence(THEME)} />);
    expect(c2.querySelector("[data-testid=theme-card]")).not.toBeNull();
    expect(c2.querySelector(".theme-card-actions")).toBeNull();
  });
});

describe("a theme in a small model's shape", () => {
  it("is read in the schema's shape, like the hub reads it", () => {
    const doc = parseTheme(
      JSON.stringify({
        key: "neon",
        name: "Neon",
        base: "terminal",
        layout: "tile",
        bar: { position: "top", items: ["start"] },
        chrome: { buttons: ["close"], titlebar: "top" },
        motion: { style: "fade", ms: 100, easing: "linear" },
        css: "",
      }),
    );
    expect(doc?.layout).toEqual({ kind: "tile" });
    expect(doc?.motion).toEqual({ style: "fade", ms: 100 });
    expect(normaliseTheme({ layout: { kind: "rows", columns: 2 } }).layout).toEqual({ kind: "rows" });
  });
});

describe("pictures in a project's result", () => {
  const ID = "0f1e2d3c-4b5a-4968-8776-655443322110";

  it("shows a picture the hub keeps, loaded with the client", async () => {
    const reportImage = vi.fn().mockResolvedValue("blob:theme-desktop");
    render(
      <ThemeCardContext.Provider value={{ reportImage } as unknown as ApiClient}>
        <Markdown text={`## Vorschau\n\n![Desktop, 1280 x 800](/api/reports/images/${ID})`} />
      </ThemeCardContext.Provider>,
    );
    const img = await screen.findByRole("img", { name: "Desktop, 1280 x 800" });
    expect(img.getAttribute("src")).toBe("blob:theme-desktop");
    expect(reportImage).toHaveBeenCalledWith(`/api/reports/images/${ID}`);
  });

  it("loads no picture from elsewhere, and none without the client", () => {
    const reportImage = vi.fn();
    render(
      <ThemeCardContext.Provider value={{ reportImage } as unknown as ApiClient}>
        <Markdown text="![pixel](https://tracker.example.invalid/p.gif)" />
      </ThemeCardContext.Provider>,
    );
    render(<Markdown text={`![kept](/api/reports/images/${ID})`} />);
    expect(screen.queryByRole("img")).toBeNull();
    expect(screen.getByText("[pixel]")).toBeTruthy();
    expect(screen.getByText("[kept]")).toBeTruthy();
    expect(reportImage).not.toHaveBeenCalled();
  });
});
