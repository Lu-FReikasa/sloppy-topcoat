import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { ApiClient } from "../api/client";
import { parseRoute } from "../route";
import { mockFetch } from "../test/mockFetch";
import { artistsOf, flowLabel, groupSheets, kindClass, SheetsPage } from "./SheetsPage";
import type { Setlist, SheetDetail, SheetResearchJob, SheetSummary } from "./types";

const SUMMARY: SheetSummary = {
  id: "s1",
  title: "Example Song",
  artist: "Example Band",
  instrument: "guitar",
  key: "A",
  capo: 2,
  capo_view: true,
  version_note: "studio",
  sources: 2,
  report_id: "r1",
  updated_at: null,
};

function detail(capoView: boolean, chords: [string, string]): SheetDetail {
  return {
    ...SUMMARY,
    capo_view: capoView,
    view: {
      title: "Example Song",
      artist: "Example Band",
      instrument: "guitar",
      key: "A",
      key_estimated: false,
      shown_key: capoView ? "G" : "A",
      capo: 2,
      capo_view: capoView,
      transpose: 0,
      flow: [
        { index: 0, kind: "verse", label: "Verse 1", repeat_of: null },
        { index: 1, kind: "chorus", label: "Chorus", repeat_of: null },
        { index: 2, kind: "chorus", label: "Chorus", repeat_of: 1 },
      ],
      sections: [
        {
          index: 0,
          kind: "verse",
          label: "Verse 1",
          repeat_of: null,
          lines: [{ segments: [{ chord: chords[0], text: "Walking down the " }, { chord: chords[1], text: "road" }] }],
        },
        { index: 1, kind: "chorus", label: "Chorus", repeat_of: null, lines: [{ segments: [{ chord: "C", text: "" }] }] },
        { index: 2, kind: "chorus", label: "Chorus", repeat_of: 1, lines: [] },
      ],
      chordpro: "{title: Example Song}\n",
    },
    source_list: [
      { url: "https://tabs.ultimate-guitar.com/tab/x/y-chords-1", title: "Example Song", version: "studio", note: null },
      { url: "https://www.guitartabs.cc/tabs/x.html", title: "Example Song chords", version: "", note: null },
    ],
  };
}

const JOB: SheetResearchJob = {
  id: "j1",
  profile: "sheets",
  song: "Other Song",
  artist: "",
  instrument: "piano",
  wish: "",
  status: "done",
  steps: ["chosen: x"],
  sheet_id: "s2",
  report_id: "r2",
  error: "",
  started: "2026-11-20T10:00:00Z",
  finished: "2026-11-20T10:01:00Z",
};

const chordsOf = (el: HTMLElement) =>
  [...el.querySelectorAll(".sheet-chord")].map((c) => c.textContent).filter((c) => c && c.trim());

function client(routes: Record<string, unknown>) {
  const m = mockFetch(routes);
  return { api: new ApiClient({ token: "t", fetchImpl: m.fetch }), calls: m.calls };
}

describe("Sheets", () => {
  it("names the parts of a song for the flow and colours them by kind", () => {
    expect(flowLabel("verse", "Verse 2")).toBe("V2");
    expect(flowLabel("chorus", "Chorus")).toBe("C");
    expect(flowLabel("pre_chorus", "Pre-Chorus")).toBe("Pre");
    expect(kindClass("bridge")).toBe("sheet-kind-bridge");
    expect(kindClass("whatever")).toBe("sheet-kind-other");
  });

  it("has an address of its own", () => {
    expect(parseRoute("#/sheets/s1")).toEqual({ view: "sheets", id: "s1" });
    expect(parseRoute("#/sheets")).toEqual({ view: "sheets" });
  });

  it("groups the versions of a song and lists the artists once", () => {
    const v = (id: string, title: string, artist: string, created: string): SheetSummary => ({
      ...SUMMARY, id, title, artist, created_at: created,
    });
    const groups = groupSheets([
      v("b", "Let It Be", "The Beatles", "2026-11-20T11:20:00Z"),
      v("w", "Wonderwall", "Oasis", "2026-11-20T11:00:00Z"),
      v("a", "Let it be", "the beatles", "2026-11-20T11:10:00Z"),
    ]);
    expect(groups.map((g) => [g.title, g.versions.map((s) => s.id)])).toEqual([
      ["Let It Be", ["a", "b"]],
      ["Wonderwall", ["w"]],
    ]);
    expect(artistsOf(groups.flatMap((g) => g.versions))).toEqual(["Oasis", "the beatles"]);
  });

  it("shows the library full width: search first, songs as a table with their versions, unfinished researches", async () => {
    const failed: SheetResearchJob = { ...JOB, id: "j2", song: "Lost Song", status: "failed", error: "no web", sheet_id: null };
    const zebra = { ...SUMMARY, id: "s9", title: "Zebra Song", artist: "Other Band", key: "E", capo: 0, created_at: "2026-11-19T12:00:00Z" };
    const { api } = client({
      "GET /api/sheets": {
        sheets: [
          { ...SUMMARY, created_at: "2026-11-20T10:00:00Z" },
          { ...SUMMARY, id: "s3", key: "C", capo: 0, created_at: "2026-11-20T12:00:00Z" },
          zebra,
        ],
      },
      "GET /api/sheets/research": { jobs: [JOB, failed] },
    });
    window.sessionStorage.clear();
    window.localStorage.removeItem("topcoat.sheets.sort");
    window.location.hash = "#/sheets";
    render(<SheetsPage api={api} />);
    const table = await screen.findByTestId("sheets-list");
    expect(screen.queryByTestId("sheet")).toBeNull();
    // search comes before sort and artist
    const fields = [...screen.getByTestId("sheets").querySelectorAll(".sheets-filter > *")].map((el) => el.getAttribute("aria-label"));
    expect(fields[0]).toMatch(/Search songs|Songs suchen/);
    // one row per song; the title opens the newest version, v1 and v2 open each one
    const rows = within(table).getAllByRole("row").slice(1);
    expect(rows.map((r) => r.querySelector(".sheets-col-title a")?.textContent)).toEqual(["Example Song", "Zebra Song"]);
    expect(rows[0]!.querySelector(".sheets-col-title a")?.getAttribute("href")).toBe("#/sheets/s3");
    expect(rows[0]!.querySelector(".sheets-col-key")?.textContent).toBe("C");
    expect(within(rows[0]!).getAllByRole("link").map((a) => [a.textContent, a.getAttribute("href")])).toEqual([
      ["Example Song", "#/sheets/s3"],
      ["v1", "#/sheets/s1"],
      ["v2", "#/sheets/s3"],
    ]);
    expect(rows[1]!.querySelector(".sheets-col-versions")?.textContent).toBe("");
    // a finished research is its sheet in the list; a failed one stays with its reason
    const jobs = screen.getByTestId("sheets-jobs");
    expect(within(jobs).queryByText("Other Song")).toBeNull();
    expect(within(jobs).getByText("no web")).toBeTruthy();
    // "/" finds the search; Enter opens the first hit
    const search = screen.getByTestId("sheets-search");
    fireEvent.keyDown(document.body, { key: "/" });
    expect(document.activeElement).toBe(search);
    fireEvent.change(search, { target: { value: "zebra" } });
    expect(within(screen.getByTestId("sheets-list")).getAllByRole("row")).toHaveLength(2);
    fireEvent.keyDown(search, { key: "Enter" });
    expect(window.location.hash).toBe("#/sheets/s9");
    // Ctrl+F too, from anywhere on the page
    search.blur();
    fireEvent.keyDown(document.body, { key: "f", ctrlKey: true });
    expect(document.activeElement).toBe(search);
  });

  it("shows a sheet over the whole width with its flow, switches the capo and goes back with Esc", async () => {
    let capoView = true;
    const { api, calls } = client({
      "GET /api/sheets": { sheets: [SUMMARY, { ...SUMMARY, id: "s3", title: "Example Song", created_at: "2026-11-20T12:00:00Z" }] },
      "GET /api/sheets/research": { jobs: [] },
      "GET /api/sheets/setlists": { setlists: [] },
      "GET /api/sheets/s1": () =>
        new Response(JSON.stringify(detail(capoView, capoView ? ["G", "D"] : ["A", "E"])), {
          headers: { "Content-Type": "application/json" },
        }),
      "PATCH /api/sheets/s1": () => {
        capoView = !capoView;
        return new Response(JSON.stringify(detail(capoView, ["A", "E"])), {
          headers: { "Content-Type": "application/json" },
        });
      },
    });
    window.sessionStorage.clear();
    window.location.hash = "#/sheets/s1";
    render(<SheetsPage api={api} id="s1" />);
    const sheet = await screen.findByTestId("sheet");
    await waitFor(() => expect(chordsOf(sheet)).toEqual(["G", "D", "C"]));
    // no song list and no setlist beside a sheet opened from the library
    expect(screen.queryByTestId("sheets-list")).toBeNull();
    expect(screen.queryByTestId("setlists")).toBeNull();
    expect(screen.getByTestId("sheets-back").getAttribute("href")).toBe("#/sheets");
    // the sheet view writes everything out until repeats are collapsed
    const toggle = within(sheet).getByRole("button", { name: /Collapse repeats|Wiederholungen kürzen/ });
    expect(toggle.getAttribute("aria-pressed")).toBe("false");
    expect(sheet.querySelectorAll(".sheet-part")).toHaveLength(3);
    fireEvent.click(toggle);
    expect(sheet.querySelectorAll(".sheet-part")).toHaveLength(2);
    expect(sheet.querySelector("#sheet-part-2")).toBeTruthy(); // the flow still finds the merged part
    fireEvent.click(toggle);
    const flow = screen.getByTestId("sheet-flow");
    expect(within(flow).getAllByRole("listitem").map((li) => li.textContent)).toEqual(["V1", "C", "C"]);
    expect(within(flow).getAllByRole("listitem")[1]?.className).toContain("sheet-kind-chorus");
    // the stored choice: capo shapes off
    fireEvent.click(within(sheet).getByRole("button", { name: /Show sounding chords|Klingende Akkorde zeigen/ }));
    await waitFor(() => expect(chordsOf(sheet)).toEqual(["A", "E", "C"]));
    expect(calls.some((c) => c.method === "PATCH" && (c.body as { capo_view: boolean }).capo_view === false)).toBe(true);
    // Esc while typing does nothing; Esc on the page goes back to the library
    const field = document.createElement("input");
    document.body.appendChild(field);
    fireEvent.keyDown(field, { key: "Escape" });
    expect(window.location.hash).toBe("#/sheets/s1");
    field.remove();
    // taken by the page (the desktop shell then does not minimise the window)
    expect(fireEvent.keyDown(document.body, { key: "Escape" })).toBe(false);
    expect(window.location.hash).toBe("#/sheets");
  });

  it("transposes only the view and compares a source", async () => {
    const { api, calls } = client({
      "GET /api/sheets": { sheets: [SUMMARY] },
      "GET /api/sheets/research": { jobs: [] },
      "GET /api/sheets/s1": detail(true, ["G", "D"]),
      "GET /api/sheets/s1?transpose=1": detail(true, ["G#", "D#"]),
      "GET /api/sheets/s1/compare?source=1": {
        against: { source: 1 },
        key_a: "A",
        key_b: "A",
        shift: 0,
        capo_a: 2,
        capo_b: 0,
        lyrics_similarity: 1,
        chords_similarity: 0.9,
        differences: [{ kind: "chord_detail", where: "Chorus, line 1", a: "C D", b: "C D7", note: "second adds detail" }],
        counts: { chord_detail: 1 },
      },
    });
    render(<SheetsPage api={api} id="s1" />);
    const sheet = await screen.findByTestId("sheet");
    await waitFor(() => expect(chordsOf(sheet)).toEqual(["G", "D", "C"]));
    fireEvent.click(within(sheet).getByRole("button", { name: /semitone up|Halbton höher/i }));
    await waitFor(() => expect(chordsOf(sheet)).toEqual(["G#", "D#", "C"]));
    expect(calls.filter((c) => c.method !== "GET")).toEqual([]);
    fireEvent.click(within(sheet).getByRole("tab", { name: /Sources|Quellen/ }));
    fireEvent.click(within(sheet).getAllByRole("button", { name: /Compare|Vergleichen/ })[1]!);
    const cmp = await screen.findByTestId("sheet-compare");
    expect(within(cmp).getByText("C D7")).toBeTruthy();
    expect(within(cmp).getByText("second adds detail")).toBeTruthy();
  });

  it("starts a research", async () => {
    const { api, calls } = client({
      "GET /api/sheets": { sheets: [] },
      "GET /api/sheets/research": { jobs: [] },
      "POST /api/sheets/research": { ...JOB, status: "running", sheet_id: null },
    });
    render(<SheetsPage api={api} />);
    // research and paste wait behind one button
    expect(screen.queryByRole("form", { name: /Research a song|Song recherchieren/ })).toBeNull();
    fireEvent.click(await screen.findByTestId("sheets-new"));
    const dialog = screen.getByRole("dialog", { name: /New sheet|Neues Sheet/ });
    expect(within(dialog).getByRole("button", { name: /Paste a sheet|Sheet einfügen/ })).toBeTruthy();
    const form = within(dialog).getByRole("form", { name: /Research a song|Song recherchieren/ });
    fireEvent.change(within(form).getByLabelText(/^Song$/), { target: { value: "Wonderwall" } });
    fireEvent.change(within(form).getByLabelText(/Artist|Künstler/), { target: { value: "Oasis" } });
    fireEvent.change(within(form).getByLabelText(/Instrument/), { target: { value: "piano" } });
    fireEvent.click(within(form).getByRole("button", { name: /Research|Recherchieren/ }));
    await waitFor(() => expect(calls.some((c) => c.method === "POST")).toBe(true));
    expect(calls.find((c) => c.method === "POST")?.body).toEqual({
      song: "Wonderwall",
      artist: "Oasis",
      instrument: "piano",
      wish: "",
    });
    // a started research closes the dialog; its progress shows in the library
    await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
  });

  it("opens the stage mode, pages with the keys of a foot pedal and leaves with Esc", async () => {
    const other = { ...SUMMARY, id: "s9", title: "Zebra Song" };
    const { api } = client({
      "GET /api/sheets": { sheets: [SUMMARY, other] },
      "GET /api/sheets/research": { jobs: [] },
      "GET /api/sheets/s1": detail(true, ["G", "D"]),
      "GET /api/sheets/s9": { ...detail(true, ["G", "D"]), id: "s9", view: { ...detail(true, ["G", "D"]).view, title: "Zebra Song" } },
    });
    window.location.hash = "#/sheets/s1";
    render(<SheetsPage api={api} id="s1" />);
    fireEvent.click(await screen.findByTestId("sheet-stage-open"));
    const stage = await screen.findByTestId("sheet-stage");
    await waitFor(() => expect(chordsOf(stage)).toEqual(["G", "D", "C"]));
    expect(screen.queryByTestId("sheets-list")).toBeNull();
    // repeats collapse by default on stage: the chorus and its {chorus} reference are one block
    const labels = () => [...stage.querySelectorAll(".sheet-part-label")].map((h) => h.textContent);
    expect(labels()).toEqual(["Verse 1", "Chorus ×2"]);
    fireEvent.click(within(stage).getByRole("button", { name: /Collapse repeats|Wiederholungen kürzen/ }));
    expect(labels()).toEqual(["Verse 1", "Chorus", expect.stringMatching(/^Chorus · /)]);
    fireEvent.keyDown(window, { key: "PageDown" });
    // paging stays on stage and does not touch the address (the desktop shell would take it over)
    await waitFor(() => expect(within(screen.getByTestId("sheet-stage")).queryByText("Zebra Song")).not.toBeNull());
    expect(window.location.hash).toBe("#/sheets/s1");
    fireEvent.keyDown(window, { key: "Escape" });
    expect(screen.queryByTestId("sheet-stage")).toBeNull();
    // leaving opens the song paged to in the normal view
    expect(window.location.hash).toBe("#/sheets/s9");
  });

  it("plays a setlist on the stage: the pedal pages in setlist order, the newest version, a song twice", async () => {
    // "Example Song" in two versions (s1 older, s2 newer), "Zebra Song" once
    const s2 = { ...SUMMARY, id: "s2", created_at: "2026-11-21T10:00:00Z" };
    const zebra = { ...SUMMARY, id: "s9", title: "Zebra Song", created_at: "2026-11-20T10:00:00Z" };
    const s1 = { ...SUMMARY, created_at: "2026-11-20T09:00:00Z" };
    const gig: Setlist = {
      id: "L1",
      name: "Gig",
      items: [
        { sheet_id: "s9", newest: false },
        { sheet_id: "s1", newest: true },
        { sheet_id: "s9", newest: false },
      ],
      updated_at: null,
      created_at: null,
    };
    const { api, calls } = client({
      "GET /api/sheets": { sheets: [s1, s2, zebra] },
      "GET /api/sheets/research": { jobs: [] },
      "GET /api/sheets/setlists": { setlists: [gig] },
      "GET /api/sheets/s1": detail(true, ["G", "D"]),
      "GET /api/sheets/s2": { ...detail(true, ["G", "D"]), id: "s2" },
      "GET /api/sheets/s9": { ...detail(true, ["G", "D"]), id: "s9" },
    });
    window.sessionStorage.clear();
    window.localStorage.setItem("topcoat.sheets.setlist", "L1");
    window.location.hash = "#/sheets";
    render(<SheetsPage api={api} />);
    const panel = await screen.findByTestId("setlists");
    const items = await within(panel).findByTestId("setlist-items");
    expect(within(items).getAllByRole("listitem").map((li) => li.getAttribute("aria-label"))).toEqual([
      "Zebra Song",
      "Example Song",
      "Zebra Song",
    ]);
    // the newest item names the version it plays now
    expect(items.textContent).toMatch(/(newest|neueste) \(v2\)/);
    fireEvent.click(within(panel).getByTestId("setlist-play"));
    // paging happens inside the stage: the address stays until the stage is left
    const at = async (n: number) =>
      waitFor(() => expect(screen.getByTestId("sheet-stage-set").textContent).toMatch(new RegExp(`Gig · ${n} (of|von) 3`)));
    const asked = () => calls.filter((c) => c.method === "GET" && /\/api\/sheets\/s\d$/.test(c.url)).map((c) => c.url.slice(-2));
    await at(1);
    fireEvent.keyDown(window, { key: "PageDown" });
    await at(2);
    fireEvent.keyDown(window, { key: "ArrowRight" });
    await at(3);
    // the end of the setlist: the pedal does nothing, back goes to the song before
    fireEvent.keyDown(window, { key: "PageDown" });
    await at(3);
    fireEvent.keyDown(window, { key: "PageUp" });
    await at(2);
    await waitFor(() => expect(asked()).toEqual(["s9", "s2", "s9", "s2"]));
    expect(window.location.hash).toBe("#/sheets");
    fireEvent.keyDown(window, { key: "Escape" });
    expect(screen.queryByTestId("sheet-stage")).toBeNull();
    // leaving opens the song the setlist was at (the newest version of Example Song)
    expect(window.location.hash).toBe("#/sheets/s2");
  });

  it("adds the open sheet to a setlist and reorders with Alt+arrow keys", async () => {
    const zebra = { ...SUMMARY, id: "s9", title: "Zebra Song" };
    const gig: Setlist = { id: "L1", name: "Gig", items: [{ sheet_id: "s9", newest: false }], updated_at: null, created_at: null };
    const { api, calls } = client({
      "GET /api/sheets": { sheets: [SUMMARY, zebra] },
      "GET /api/sheets/research": { jobs: [] },
      "GET /api/sheets/setlists": { setlists: [gig] },
      "GET /api/sheets/s1": detail(true, ["G", "D"]),
      "PATCH /api/sheets/setlists/L1": gig,
    });
    window.localStorage.setItem("topcoat.sheets.setlist", "L1");
    window.sessionStorage.clear();
    render(<SheetsPage api={api} id="s1" />);
    // opened from the library: the setlist comes beside the sheet when asked for
    await screen.findByTestId("sheet");
    expect(screen.queryByTestId("setlists")).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: /^Setlist$/ }));
    const panel = await screen.findByTestId("setlists");
    await within(panel).findByTestId("setlist-items");
    fireEvent.change(within(panel).getByLabelText(/^(Version)$/), { target: { value: "this" } });
    fireEvent.click(within(panel).getByTestId("setlist-add"));
    const patches = () => calls.filter((c) => c.method === "PATCH").map((c) => c.body);
    await waitFor(() =>
      expect(patches().at(-1)).toEqual({
        items: [
          { sheet_id: "s9", newest: false },
          { sheet_id: "s1", newest: false },
        ],
      }),
    );
    const rows = within(within(panel).getByTestId("setlist-items")).getAllByRole("listitem");
    fireEvent.keyDown(rows[1]!, { key: "ArrowUp", altKey: true });
    await waitFor(() =>
      expect(patches().at(-1)).toEqual({
        items: [
          { sheet_id: "s1", newest: false },
          { sheet_id: "s9", newest: false },
        ],
      }),
    );
    // the moved song keeps the focus, so it can be moved on
    await waitFor(() => expect(document.activeElement?.getAttribute("aria-label")).toBe("Example Song"));
  });

  it("keeps the setlist beside a song opened from it, and not beside one opened from the library", async () => {
    const zebra = { ...SUMMARY, id: "s9", title: "Zebra Song" };
    const gig: Setlist = { id: "L1", name: "Gig", items: [{ sheet_id: "s9", newest: false }, { sheet_id: "s1", newest: false }], updated_at: null, created_at: null };
    const { api } = client({
      "GET /api/sheets": { sheets: [SUMMARY, zebra] },
      "GET /api/sheets/research": { jobs: [] },
      "GET /api/sheets/setlists": { setlists: [gig] },
      "GET /api/sheets/s1": detail(true, ["G", "D"]),
      "GET /api/sheets/s9": { ...detail(true, ["G", "D"]), id: "s9" },
    });
    window.localStorage.setItem("topcoat.sheets.setlist", "L1");
    window.sessionStorage.clear();
    window.location.hash = "#/sheets";
    // both shells mount the page afresh for a new address, as the key does here
    const { rerender } = render(<SheetsPage key="list" api={api} />);
    const items = await screen.findByTestId("setlist-items");
    const link = within(items).getByRole("link", { name: "Zebra Song" });
    expect(link.getAttribute("href")).toBe("#/sheets/s9");
    fireEvent.click(link);
    rerender(<SheetsPage key="s9" api={api} id="s9" />);
    const side = await screen.findByTestId("setlists");
    expect(side.closest(".sheets-side")).toBeTruthy();
    const open = await within(side).findByRole("link", { name: "Zebra Song" });
    expect(open.getAttribute("aria-current")).toBe("page");
    expect(screen.getByRole("button", { name: /^Setlist$/ }).getAttribute("aria-pressed")).toBe("true");
    // the next song of the setlist opens with the setlist still beside it
    fireEvent.click(within(side).getByRole("link", { name: "Example Song" }));
    rerender(<SheetsPage key="s1" api={api} id="s1" />);
    expect(await screen.findByTestId("setlists")).toBeTruthy();
    // back in the library, a song opened from the table stands alone
    rerender(<SheetsPage key="list2" api={api} />);
    const table = await screen.findByTestId("sheets-list");
    fireEvent.click(within(table).getByRole("link", { name: "Zebra Song" }));
    rerender(<SheetsPage key="s9b" api={api} id="s9" />);
    await screen.findByTestId("sheet");
    expect(screen.queryByTestId("setlists")).toBeNull();
  });
});
