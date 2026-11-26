import { act, cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { ApiClient } from "../api/client";
import { jsonResponse, mockFetch } from "../test/mockFetch";
import { previewTheme } from "./preview";
import type { ThemeManifest } from "./theme";
import { ThemesPage } from "./ThemesPage";

afterEach(() => {
  cleanup();
  previewTheme(null);
  vi.restoreAllMocks();
});

const WALL: ThemeManifest = {
  key: "wall",
  name: "Wall",
  base: "terminal",
  layout: { kind: "columns" },
  bar: { position: "left", items: ["programs", "status"] },
  chrome: { buttons: ["close"], titlebar: "top" },
  motion: { style: "slide", ms: 200 },
};

function setup(custom: ThemeManifest[] = [WALL]) {
  const mock = mockFetch({
    "GET /api/ui/themes": custom,
    "PUT /api/ui/themes/wall": (init?: RequestInit) => jsonResponse(JSON.parse(String(init?.body))),
    "PUT /api/ui/themes/my-theme": (init?: RequestInit) => jsonResponse(JSON.parse(String(init?.body)), 201),
    "DELETE /api/ui/themes/wall": new Response(null, { status: 204 }),
  });
  const api = new ApiClient({ token: "t", fetchImpl: mock.fetch });
  render(<ThemesPage api={api} />);
  return mock;
}

describe("ThemesPage", () => {
  it("lists the built-in themes to copy and the custom ones to change", async () => {
    setup();
    const list = await screen.findByTestId("theme-list");
    await within(list).findByText("Wall");
    expect(within(list).getAllByRole("button", { name: "Kopieren und ändern" })).toHaveLength(4);
    expect(within(list).getByText("Terminal")).toBeTruthy();
    expect(within(list).getAllByText("eingebaut")).toHaveLength(3);
  });

  it("edits a theme, tells what is wrong, and saves only a valid one", async () => {
    const mock = setup();
    fireEvent.click(await screen.findByRole("button", { name: "Ändern" }));
    const editor = await screen.findByTestId("theme-editor");
    expect((within(editor).getByLabelText("Schlüssel") as HTMLInputElement).disabled).toBe(true);
    fireEvent.change(within(editor).getByLabelText("Anordnung der Anwendungen"), { target: { value: "rows" } });
    fireEvent.change(within(editor).getByLabelText("Stylesheet"), { target: { value: "@import 'x';" } });
    expect((await screen.findByTestId("theme-problems")).textContent).toContain("@import");
    expect((within(editor).getByRole("button", { name: "Theme speichern" }) as HTMLButtonElement).disabled).toBe(true);
    fireEvent.change(within(editor).getByLabelText("Stylesheet"), { target: { value: "--accent: red;" } });
    await waitFor(() => expect(screen.queryByTestId("theme-problems")).toBeNull());
    fireEvent.click(within(editor).getByRole("button", { name: "Theme speichern" }));
    await waitFor(() => expect(mock.calls.some((c) => c.method === "PUT")).toBe(true));
    const put = mock.calls.find((c) => c.method === "PUT")!;
    expect(put.url).toBe("/api/ui/themes/wall");
    expect(put.body).toMatchObject({ key: "wall", layout: { kind: "rows" }, css: "--accent: red;" });
    expect(await screen.findByText("Gespeichert: Wall")).toBeTruthy();
  });

  it("orders the parts of the taskbar and the buttons, and leaves out what is not ticked", async () => {
    const mock = setup();
    fireEvent.click(await screen.findByRole("button", { name: "Ändern" }));
    const editor = await screen.findByTestId("theme-editor");
    // programs, status are on; put the connection first, tick the start button
    fireEvent.click(within(editor).getByRole("button", { name: "Verbindung eine Stelle nach oben" }));
    fireEvent.click(within(editor).getByLabelText(/Startknopf/));
    fireEvent.click(within(editor).getByLabelText("Minimieren"));
    fireEvent.click(within(editor).getByRole("button", { name: "Theme speichern" }));
    await waitFor(() => expect(mock.calls.some((c) => c.method === "PUT")).toBe(true));
    const body = mock.calls.find((c) => c.method === "PUT")!.body as ThemeManifest;
    expect(body.bar.items).toEqual(["status", "programs", "start"]);
    expect(body.chrome.buttons).toEqual(["close", "minimise"]);
  });

  it("starts a copy of a built-in theme as a new one, with the style sheet of the original", async () => {
    const mock = setup([]);
    fireEvent.click((await screen.findAllByRole("button", { name: "Kopieren und ändern" }))[1]!); // leitstand
    const editor = await screen.findByTestId("theme-editor");
    expect((within(editor).getByLabelText("Schlüssel") as HTMLInputElement).value).toBe("leitstand-copy");
    expect((within(editor).getByLabelText("Beginnt mit dem Stylesheet von") as HTMLSelectElement).value).toBe(
      "leitstand",
    );
    fireEvent.change(within(editor).getByLabelText("Schlüssel"), { target: { value: "my-theme" } });
    fireEvent.click(within(editor).getByRole("button", { name: "Theme speichern" }));
    await waitFor(() => expect(mock.calls.some((c) => c.method === "PUT" && c.url.endsWith("/my-theme"))).toBe(true));
  });

  it("deletes only after confirmation", async () => {
    const mock = setup();
    const confirm = vi.spyOn(window, "confirm").mockReturnValueOnce(false).mockReturnValueOnce(true);
    const del = await screen.findByRole("button", { name: "Löschen" });
    fireEvent.click(del);
    expect(mock.calls.some((c) => c.method === "DELETE")).toBe(false);
    fireEvent.click(del);
    await waitFor(() =>
      expect(mock.calls.some((c) => c.method === "DELETE" && c.url === "/api/ui/themes/wall")).toBe(true),
    );
    confirm.mockRestore();
  });

  it("shows the draft on the desktop while it is valid, and stops when it is closed", async () => {
    const seen: (ThemeManifest | null)[] = [];
    const { usePreviewTheme } = await import("./preview");
    function Probe() {
      seen.push(usePreviewTheme());
      return null;
    }
    const mock = setup();
    render(<Probe />);
    fireEvent.click(await screen.findByRole("button", { name: "Ändern" }));
    const editor = await screen.findByTestId("theme-editor");
    await waitFor(() => expect(seen.at(-1)?.key).toBe("wall"));
    fireEvent.change(within(editor).getByLabelText("Anordnung der Anwendungen"), { target: { value: "focus" } });
    await waitFor(() => expect(seen.at(-1)?.layout.kind).toBe("focus"));
    // the nest layout (#15) is offered with its own name
    const layout = within(editor).getByLabelText("Anordnung der Anwendungen") as HTMLSelectElement;
    expect([...layout.options].find((o) => o.value === "nest")?.textContent).toBe("Verschachtelt, mit Zoom (Ebenen von Gruppen)");
    fireEvent.change(layout, { target: { value: "nest" } });
    await waitFor(() => expect(seen.at(-1)?.layout.kind).toBe("nest"));
    fireEvent.change(layout, { target: { value: "focus" } });
    await waitFor(() => expect(seen.at(-1)?.layout.kind).toBe("focus"));
    // an invalid draft is not shown
    fireEvent.change(within(editor).getByLabelText("Stylesheet"), { target: { value: "@import 'x';" } });
    await waitFor(() => expect(seen.at(-1)).toBeNull());
    fireEvent.change(within(editor).getByLabelText("Stylesheet"), { target: { value: "" } });
    await waitFor(() => expect(seen.at(-1)?.layout.kind).toBe("focus"));
    // the switch turns the preview off, and closing the editor ends it
    fireEvent.click(within(editor).getByLabelText("Beim Bearbeiten auf dem Desktop zeigen"));
    await waitFor(() => expect(seen.at(-1)).toBeNull());
    fireEvent.click(within(editor).getByLabelText("Beim Bearbeiten auf dem Desktop zeigen"));
    await waitFor(() => expect(seen.at(-1)?.key).toBe("wall"));
    await act(async () => {
      fireEvent.click(within(editor).getByRole("button", { name: "Schließen" }));
    });
    await waitFor(() => expect(seen.at(-1)).toBeNull());
    expect(mock.calls.some((c) => c.method === "PUT")).toBe(false); // nothing was stored
  });
});
