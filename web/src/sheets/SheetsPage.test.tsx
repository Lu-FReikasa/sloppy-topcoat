import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { ApiClient } from "../api/client";
import { parseRoute } from "../route";
import { mockFetch } from "../test/mockFetch";
import { artistsOf, flowLabel, groupSheets, kindClass, SheetsPage } from "./SheetsPage";
import type { SheetDetail, SheetResearchJob, SheetSummary } from "./types";

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

  it("lists sheets and only unfinished researches, shows a sheet with its flow and switches the capo", async () => {
    let capoView = true;
    const failed: SheetResearchJob = { ...JOB, id: "j2", song: "Lost Song", status: "failed", error: "no web", sheet_id: null };
    const { api, calls } = client({
      "GET /api/sheets": { sheets: [SUMMARY, { ...SUMMARY, id: "s3", title: "Example Song", created_at: "2026-11-20T12:00:00Z" }] },
      "GET /api/sheets/research": { jobs: [JOB, failed] },
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
    render(<SheetsPage api={api} id="s1" />);
    const sheet = await screen.findByTestId("sheet");
    await waitFor(() => expect(chordsOf(sheet)).toEqual(["G", "D", "C"]));
    // two versions of one song: one entry with v1 and v2
    const list = screen.getByTestId("sheets-list");
    expect(within(list).getAllByText("Example Song")).toHaveLength(1);
    expect(within(list).getAllByRole("link").map((a) => a.textContent?.slice(0, 2))).toEqual(["v1", "v2"]);
    // a finished research is its sheet in the list; a failed one stays with its reason
    const jobs = screen.getByTestId("sheets-jobs");
    expect(within(jobs).queryByText("Other Song")).toBeNull();
    expect(within(jobs).getByText("no web")).toBeTruthy();
    const flow = screen.getByTestId("sheet-flow");
    expect(within(flow).getAllByRole("listitem").map((li) => li.textContent)).toEqual(["V1", "C", "C"]);
    expect(within(flow).getAllByRole("listitem")[1]?.className).toContain("sheet-kind-chorus");
    // the stored choice: capo shapes off
    fireEvent.click(within(sheet).getByRole("button", { name: /Show sounding chords|Klingende Akkorde zeigen/ }));
    await waitFor(() => expect(chordsOf(sheet)).toEqual(["A", "E", "C"]));
    expect(calls.some((c) => c.method === "PATCH" && (c.body as { capo_view: boolean }).capo_view === false)).toBe(true);
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
    const form = await screen.findByRole("form", { name: /Research a song|Song recherchieren/ });
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
    fireEvent.keyDown(window, { key: "PageDown" });
    // paging stays on stage and does not touch the address (the desktop shell would take it over)
    await waitFor(() => expect(within(screen.getByTestId("sheet-stage")).queryByText("Zebra Song")).not.toBeNull());
    expect(window.location.hash).toBe("#/sheets/s1");
    fireEvent.keyDown(window, { key: "Escape" });
    expect(screen.queryByTestId("sheet-stage")).toBeNull();
    // leaving opens the song paged to in the normal view
    expect(window.location.hash).toBe("#/sheets/s9");
  });
});
