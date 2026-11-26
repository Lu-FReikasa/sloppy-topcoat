import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { ApiClient } from "../api/client";
import { jsonResponse, mockFetch } from "../test/mockFetch";
import { move, pageBox, pageRule, printColumns } from "./print";
import { PRINTING_CLASS, PrintDialog } from "./PrintSheets";
import { SheetsPage } from "./SheetsPage";
import type { Setlist, SheetDetail, SheetSummary } from "./types";

function summary(id: string, title: string): SheetSummary {
  return {
    id,
    title,
    artist: "Example Band",
    instrument: "guitar",
    key: "A",
    capo: 0,
    capo_view: false,
    version_note: "",
    sources: 0,
    report_id: null,
    updated_at: null,
  };
}

function detail(id: string, title: string): SheetDetail {
  return {
    ...summary(id, title),
    view: {
      title,
      artist: "Example Band",
      instrument: "guitar",
      key: "A",
      key_estimated: false,
      shown_key: "A",
      capo: 0,
      capo_view: false,
      transpose: 0,
      flow: [{ index: 0, kind: "verse", label: "Verse 1", repeat_of: null }],
      sections: [
        {
          index: 0,
          kind: "verse",
          label: "Verse 1",
          repeat_of: null,
          lines: [{ segments: [{ chord: "A", text: `Words of ${title}` }] }, { comment: "e|--3--2--0--3--|" }],
        },
        { index: 1, kind: "tab", label: "Tab", repeat_of: null, lines: [{ comment: "e|--0--2--3--|" }] },
      ],
      chordpro: "",
    },
    source_list: [],
  };
}

function client(routes: Record<string, unknown>) {
  const m = mockFetch(routes);
  return { api: new ApiClient({ token: "t", fetchImpl: m.fetch }), calls: m.calls };
}

const SONGS = [
  { id: "s1", title: "First Song", artist: "Example Band" },
  { id: "s2", title: "Second Song", artist: "Example Band" },
  { id: "s3", title: "Third Song", artist: "" },
];

const pageTitles = () =>
  screen.queryAllByTestId("sheet-print-page").map((p) => p.querySelector(".sheet-print-head strong")?.textContent);

afterEach(() => {
  vi.restoreAllMocks();
});

describe("print layout", () => {
  it("sizes the page inside a 10 mm margin and picks the columns by its width", () => {
    expect(pageBox("a4", "portrait")).toEqual({ width: 718, height: 1046 });
    expect(pageBox("a4", "landscape")).toEqual({ width: 1046, height: 718 });
    expect(pageBox("letter", "portrait")).toEqual({ width: 740, height: 980 });
    expect(printColumns(pageBox("a4", "portrait").width)).toBe(2);
    expect(printColumns(pageBox("a4", "landscape").width)).toBe(3);
    expect(printColumns(200)).toBe(1);
    expect(pageRule("letter", "landscape")).toBe("@page { size: letter landscape; margin: 10mm; }");
  });

  it("moves a song in the order and leaves the ends alone", () => {
    expect(move(["a", "b", "c"], 2, -1)).toEqual(["a", "c", "b"]);
    expect(move(["a", "b", "c"], 0, 1)).toEqual(["b", "a", "c"]);
    const list = ["a", "b"];
    expect(move(list, 0, -1)).toBe(list);
    expect(move(list, 1, 1)).toBe(list);
  });
});

/** window.print as desktop browsers run it: the dialog holds the call, afterprint fires before it returns. */
function desktopPrint(during: () => void = () => undefined) {
  return vi.spyOn(window, "print").mockImplementation(() => {
    during();
    window.dispatchEvent(new Event("afterprint"));
  });
}

describe("PrintDialog", () => {
  it("prints the picked songs in the chosen order, one per page, and cleans up after the print", async () => {
    const titles: (string | null | undefined)[][] = [];
    const print = desktopPrint(() => {
      // what the print dialog sees: only the pages, the rest of the page is hidden by class
      expect(document.body.classList.contains(PRINTING_CLASS)).toBe(true);
      titles.push(pageTitles());
    });
    const { api, calls } = client({
      "GET /api/sheets/s1": detail("s1", "First Song"),
      "GET /api/sheets/s2": detail("s2", "Second Song"),
      "GET /api/sheets/s3": detail("s3", "Third Song"),
    });
    const onClose = vi.fn();
    render(<PrintDialog api={api} songs={SONGS} hideTabs collapse={false} onClose={onClose} />);
    const dialog = screen.getByTestId("sheet-print-dialog");
    // leave out the second song, put the third one first
    fireEvent.click(within(dialog).getByRole("checkbox", { name: /Second Song/ }));
    fireEvent.click(within(dialog).getByRole("button", { name: /Third Song (up|nach oben)/ }));
    fireEvent.click(within(dialog).getByRole("button", { name: /Third Song (up|nach oben)/ }));
    expect(within(dialog).getByText(/2 (of|von) 3/)).toBeTruthy();
    fireEvent.click(within(dialog).getByTestId("sheet-print-start"));

    await waitFor(() => expect(print).toHaveBeenCalledTimes(1));
    expect(titles).toEqual([["Third Song", "First Song"]]);
    expect(calls.map((c) => c.url)).toEqual(["/api/sheets/s3", "/api/sheets/s1"]);
    await waitFor(() => expect(onClose).toHaveBeenCalled());
    expect(print).toHaveBeenCalledTimes(1);
  });

  it("leaves tab lines out when asked and keeps them otherwise", async () => {
    let bodies: string[] = [];
    desktopPrint(() => {
      bodies = screen.getAllByTestId("sheet-print-page").map((p) => p.textContent ?? "");
    });
    const { api } = client({ "GET /api/sheets/s1": detail("s1", "First Song") });
    const onClose = vi.fn();
    const { unmount } = render(<PrintDialog api={api} songs={SONGS.slice(0, 1)} hideTabs collapse={false} onClose={onClose} />);
    fireEvent.click(screen.getByTestId("sheet-print-start"));
    await waitFor(() => expect(onClose).toHaveBeenCalled());
    expect(bodies[0]).toContain("Words of First Song");
    expect(bodies[0]).not.toContain("e|--");
    unmount();

    onClose.mockClear();
    render(<PrintDialog api={api} songs={SONGS.slice(0, 1)} hideTabs collapse={false} onClose={onClose} />);
    fireEvent.click(screen.getByRole("checkbox", { name: /Hide tabs|Tabs ausblenden/ }));
    fireEvent.click(screen.getByTestId("sheet-print-start"));
    await waitFor(() => expect(onClose).toHaveBeenCalled());
    expect(bodies[0]).toContain("e|--0--2--3--|");
  });

  it("removes the pages and the print class once printed", async () => {
    desktopPrint();
    const { api } = client({ "GET /api/sheets/s1": detail("s1", "First Song") });
    const onClose = vi.fn();
    const { rerender } = render(<PrintDialog api={api} songs={SONGS.slice(0, 1)} hideTabs={false} collapse={false} onClose={onClose} />);
    fireEvent.click(screen.getByTestId("sheet-print-start"));
    await waitFor(() => expect(onClose).toHaveBeenCalled());
    // the parent closes the dialog: nothing of the songs stays in the page
    rerender(<></>);
    expect(screen.queryByTestId("sheet-print")).toBeNull();
    expect(document.body.classList.contains(PRINTING_CLASS)).toBe(false);
  });

  it("keeps the pages where print() returns before the print is done (phones, tablets)", async () => {
    const print = vi.spyOn(window, "print").mockImplementation(() => undefined);
    const { api } = client({ "GET /api/sheets/s1": detail("s1", "First Song") });
    const onClose = vi.fn();
    const { rerender } = render(<PrintDialog api={api} songs={SONGS.slice(0, 1)} hideTabs={false} collapse={false} onClose={onClose} />);
    fireEvent.click(screen.getByTestId("sheet-print-start"));
    await waitFor(() => expect(print).toHaveBeenCalledTimes(1));
    // the print is laid out after the call: the pages and the class are still there
    await waitFor(() => expect((screen.getByTestId("sheet-print-start") as HTMLButtonElement).disabled).toBe(false));
    expect(screen.getAllByTestId("sheet-print-page")).toHaveLength(1);
    expect(document.body.classList.contains(PRINTING_CLASS)).toBe(true);
    expect(onClose).not.toHaveBeenCalled();
    expect(screen.getByRole("status").textContent).toMatch(/print dialog is open|Druckdialog ist offen/);

    // afterprint ends it like on the desktop
    window.dispatchEvent(new Event("afterprint"));
    await waitFor(() => expect(onClose).toHaveBeenCalled());
    rerender(<></>);
    expect(screen.queryByTestId("sheet-print")).toBeNull();
    expect(document.body.classList.contains(PRINTING_CLASS)).toBe(false);
  });

  it("lets the person close the dialog and print again when no afterprint comes", async () => {
    const print = vi.spyOn(window, "print").mockImplementation(() => undefined);
    const { api } = client({ "GET /api/sheets/s1": detail("s1", "First Song") });
    const onClose = vi.fn();
    const { rerender } = render(<PrintDialog api={api} songs={SONGS.slice(0, 1)} hideTabs={false} collapse={false} onClose={onClose} />);
    fireEvent.click(screen.getByTestId("sheet-print-start"));
    await waitFor(() => expect((screen.getByTestId("sheet-print-start") as HTMLButtonElement).disabled).toBe(false));
    fireEvent.click(screen.getByTestId("sheet-print-start"));
    await waitFor(() => expect(print).toHaveBeenCalledTimes(2));
    await waitFor(() => expect((screen.getByTestId("sheet-print-start") as HTMLButtonElement).disabled).toBe(false));
    expect(screen.getAllByTestId("sheet-print-page")).toHaveLength(1);
    fireEvent.click(screen.getByRole("button", { name: /^(Close|Schließen)$/ }));
    expect(onClose).toHaveBeenCalled();
    rerender(<></>);
    expect(screen.queryByTestId("sheet-print")).toBeNull();
    expect(document.body.classList.contains(PRINTING_CLASS)).toBe(false);
  });

  it("shows why a song could not be loaded and does not print", async () => {
    const print = vi.spyOn(window, "print").mockImplementation(() => undefined);
    const { api } = client({
      "GET /api/sheets/s1": detail("s1", "First Song"),
      "GET /api/sheets/s2": jsonResponse({ detail: "boom" }, 500),
    });
    render(<PrintDialog api={api} songs={SONGS.slice(0, 2)} hideTabs collapse={false} onClose={() => undefined} />);
    fireEvent.click(screen.getByTestId("sheet-print-start"));
    expect(await screen.findByRole("alert")).toBeTruthy();
    expect(print).not.toHaveBeenCalled();
    expect(screen.queryByTestId("sheet-print")).toBeNull();
    expect((screen.getByTestId("sheet-print-start") as HTMLButtonElement).disabled).toBe(false);
  });
});

describe("Sheets page printing", () => {
  it("offers the shown songs, newest version each, in the list's order", async () => {
    const { api } = client({
      "GET /api/sheets": {
        sheets: [
          summary("w", "Wonderwall"),
          { ...summary("a1", "Another Song"), created_at: "2026-11-20T10:00:00Z" },
          { ...summary("a2", "Another Song"), created_at: "2026-11-20T11:00:00Z" },
        ],
      },
      "GET /api/sheets/research": { jobs: [] },
    });
    render(<SheetsPage api={api} />);
    fireEvent.click(await screen.findByTestId("sheets-print-list"));
    const songs = within(screen.getByTestId("sheet-print-dialog")).getByRole("list");
    expect(within(songs).getAllByRole("checkbox").map((c) => c.closest("label")?.textContent)).toEqual([
      "Another Song · Example Band",
      "Wonderwall · Example Band",
    ]);
  });

  it("prints the open sheet as it is shown: instrument and transposition", async () => {
    desktopPrint();
    const { api, calls } = client({
      "GET /api/sheets": { sheets: [summary("s1", "First Song")] },
      "GET /api/sheets/research": { jobs: [] },
      "GET /api/sheets/s1": detail("s1", "First Song"),
      "GET /api/sheets/s1?transpose=2": detail("s1", "First Song"),
    });
    render(<SheetsPage api={api} id="s1" />);
    const sheet = await screen.findByTestId("sheet");
    const up = within(sheet).getByRole("button", { name: /semitone up|Halbton höher/i });
    fireEvent.click(up);
    fireEvent.click(up);
    await waitFor(() => expect(calls.some((c) => c.url === "/api/sheets/s1?transpose=2")).toBe(true));
    const before = calls.length;
    fireEvent.click(within(sheet).getByTestId("sheet-print-open"));
    fireEvent.click(screen.getByTestId("sheet-print-start"));
    await waitFor(() => expect(screen.queryByTestId("sheet-print-dialog")).toBeNull());
    expect(calls.slice(before).map((c) => c.url)).toEqual(["/api/sheets/s1?transpose=2"]);
  });
});

describe("Printing a setlist", () => {
  it("offers the setlist's songs in its order, the newest version where wanted, a song twice", async () => {
    // "Another Song" in two versions (a1 older, a2 newer), "Wonderwall" twice in the setlist
    const titles: (string | null | undefined)[][] = [];
    desktopPrint(() => titles.push(pageTitles()));
    const gig: Setlist = {
      id: "L1",
      name: "Gig",
      items: [
        { sheet_id: "w", newest: false },
        { sheet_id: "a1", newest: true },
        { sheet_id: "gone", newest: false },
        { sheet_id: "w", newest: false },
      ],
      updated_at: null,
      created_at: null,
    };
    const { api, calls } = client({
      "GET /api/sheets": {
        sheets: [
          summary("w", "Wonderwall"),
          { ...summary("a1", "Another Song"), created_at: "2026-11-20T10:00:00Z" },
          { ...summary("a2", "Another Song"), created_at: "2026-11-20T11:00:00Z" },
        ],
      },
      "GET /api/sheets/research": { jobs: [] },
      "GET /api/sheets/setlists": { setlists: [gig] },
      "GET /api/sheets/w": detail("w", "Wonderwall"),
      "GET /api/sheets/a2": detail("a2", "Another Song"),
    });
    window.localStorage.setItem("topcoat.sheets.setlist", "L1");
    render(<SheetsPage api={api} />);
    fireEvent.click(await within(await screen.findByTestId("setlists")).findByTestId("setlist-print"));
    const dialog = screen.getByTestId("sheet-print-dialog");
    const boxes = within(within(dialog).getByRole("list")).getAllByRole("checkbox");
    // the deleted sheet is left out; the same song twice is two songs to pick
    expect(boxes.map((c) => c.closest("label")?.textContent)).toEqual([
      "Wonderwall · Example Band",
      "Another Song · Example Band",
      "Wonderwall · Example Band",
    ]);
    fireEvent.click(boxes[2]!);
    expect((boxes[0] as HTMLInputElement).checked).toBe(true);
    const before = calls.length;
    fireEvent.click(within(dialog).getByTestId("sheet-print-start"));
    await waitFor(() => expect(screen.queryByTestId("sheet-print-dialog")).toBeNull());
    expect(titles).toEqual([["Wonderwall", "Another Song"]]);
    expect(calls.slice(before).map((c) => c.url)).toEqual(["/api/sheets/w", "/api/sheets/a2"]);
    window.localStorage.removeItem("topcoat.sheets.setlist");
  });

  it("collapses repeated parts when asked, as the stage mode does", async () => {
    let text = "";
    desktopPrint(() => {
      text = screen.getByTestId("sheet-print-page").textContent ?? "";
    });
    const chorus = (index: number) => ({
      index,
      kind: "chorus",
      label: "Chorus",
      repeat_of: null,
      lines: [{ segments: [{ chord: "C", text: "Sing the chorus line" }] }],
    });
    const base = detail("s1", "First Song");
    const sheet: SheetDetail = {
      ...base,
      view: { ...base.view, sections: [chorus(0), { ...base.view.sections[0]!, index: 1 }, chorus(2)] },
    };
    const { api } = client({ "GET /api/sheets/s1": sheet });
    const count = () => text.split("Sing the chorus line").length - 1;

    const onClose = vi.fn();
    const { unmount } = render(<PrintDialog api={api} songs={SONGS.slice(0, 1)} hideTabs collapse onClose={onClose} />);
    fireEvent.click(screen.getByTestId("sheet-print-start"));
    await waitFor(() => expect(onClose).toHaveBeenCalled());
    expect(count()).toBe(1);
    unmount();

    onClose.mockClear();
    render(<PrintDialog api={api} songs={SONGS.slice(0, 1)} hideTabs collapse onClose={onClose} />);
    fireEvent.click(screen.getByRole("checkbox", { name: /Collapse repeats|Wiederholungen kürzen/ }));
    fireEvent.click(screen.getByTestId("sheet-print-start"));
    await waitFor(() => expect(onClose).toHaveBeenCalled());
    expect(count()).toBe(2);
  });
});
