import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import type { ApiClient } from "../api/client";
import { Setlists } from "./Setlists";
import type { Setlist, SetlistItem, SheetSummary } from "./types";

const sheet = (id: string, title: string, artist = "Example Band"): SheetSummary => ({
  id,
  title,
  artist,
  instrument: "guitar",
  key: "A",
  capo: 0,
  capo_view: true,
  version_note: "",
  sources: 0,
  report_id: null,
  updated_at: "2026-11-20T09:00:00Z",
  created_at: "2026-11-20T09:00:00Z",
});

describe("Setlists", () => {
  it("sends quick changes one after the other, so the hub ends with the last order", async () => {
    const list = [sheet("a", "Song A"), sheet("b", "Song B"), sheet("c", "Song C")];
    const gig: Setlist = {
      id: "L1",
      name: "Gig",
      items: list.map((s) => ({ sheet_id: s.id, newest: false })),
      updated_at: null,
      created_at: null,
    };
    const answers: (() => void)[] = [];
    const sent: SetlistItem[][] = [];
    const api = {
      setlists: () => Promise.resolve({ setlists: [gig] }),
      setlistUpdate: vi.fn((_id: string, patch: { items?: SetlistItem[] }) => {
        sent.push(patch.items ?? []);
        return new Promise<Setlist>((done) => answers.push(() => done({ ...gig, items: patch.items ?? [] })));
      }),
    } as unknown as ApiClient;
    window.localStorage.setItem("topcoat.sheets.setlist", "L1");
    render(<Setlists api={api} list={list} onOpen={() => undefined} onPlay={() => undefined} onPrint={() => undefined} onError={() => undefined} />);
    const items = await screen.findByTestId("setlist-items");
    const down = () => within(items).getAllByRole("button", { name: /^(Move down|Nach unten)$/ });
    fireEvent.click(down()[0]!); // a b c -> b a c
    fireEvent.click(down()[1]!); // b a c -> b c a
    // the second change waits until the hub has answered the first
    const orders = () => sent.map((o) => o.map((i) => i.sheet_id).join(""));
    await waitFor(() => expect(orders()).toEqual(["bac"]));
    await new Promise((r) => setTimeout(r, 20));
    expect(orders()).toEqual(["bac"]);
    answers[0]!();
    await waitFor(() => expect(orders()).toEqual(["bac", "bca"]));
    answers[1]!();
  });

  it("after a refused change, reloads only once the changes queued behind it have landed", async () => {
    const list = [sheet("a", "Song A"), sheet("b", "Song B"), sheet("c", "Song C")];
    const gig: Setlist = {
      id: "L1",
      name: "Gig",
      items: list.map((s) => ({ sheet_id: s.id, newest: false })),
      updated_at: null,
      created_at: null,
    };
    const answers: ((ok: boolean) => void)[] = [];
    const loads = vi.fn(() => Promise.resolve({ setlists: [gig] }));
    const onError = vi.fn();
    const api = {
      setlists: loads,
      setlistUpdate: vi.fn(
        (_id: string, patch: { items?: SetlistItem[] }) =>
          new Promise<Setlist>((done, fail) =>
            answers.push((ok) => (ok ? done({ ...gig, items: patch.items ?? [] }) : fail(new Error("refused")))),
          ),
      ),
    } as unknown as ApiClient;
    window.localStorage.setItem("topcoat.sheets.setlist", "L1");
    render(<Setlists api={api} list={list} onOpen={() => undefined} onPlay={() => undefined} onPrint={() => undefined} onError={onError} />);
    const items = await screen.findByTestId("setlist-items");
    const down = () => within(items).getAllByRole("button", { name: /^(Move down|Nach unten)$/ });
    fireEvent.click(down()[0]!);
    fireEvent.click(down()[1]!);
    await waitFor(() => expect(answers).toHaveLength(1));
    expect(loads).toHaveBeenCalledTimes(1);
    answers[0]!(false); // refused; the second change is sent next
    await waitFor(() => expect(answers).toHaveLength(2));
    expect(onError).toHaveBeenCalledTimes(1);
    expect(loads).toHaveBeenCalledTimes(1); // no reload while a change is still on its way
    answers[1]!(true);
    await waitFor(() => expect(loads).toHaveBeenCalledTimes(2));
  });

  it("a sorted or searched view keeps the stored order and plays from the song's own position", async () => {
    // the evening's order: 1 Zebra (Alpha Band), 2 Moon (Beta Band), 3 Apple (Beta Band)
    const list = [sheet("z", "Zebra Song", "Alpha Band"), sheet("m", "Moon Song", "Beta Band"), sheet("p", "Apple Song", "Beta Band")];
    const gig: Setlist = {
      id: "L2",
      name: "Gig",
      items: list.map((s) => ({ sheet_id: s.id, newest: false })),
      updated_at: null,
      created_at: null,
    };
    const setlistUpdate = vi.fn((_id: string, patch: { items?: SetlistItem[] }) => Promise.resolve({ ...gig, ...patch }));
    const api = { setlists: () => Promise.resolve({ setlists: [gig] }), setlistUpdate } as unknown as ApiClient;
    const onPlay = vi.fn();
    window.localStorage.setItem("topcoat.sheets.setlist", "L2");
    window.localStorage.removeItem("topcoat.sheets.setlistView");
    const { unmount } = render(<Setlists api={api} list={list} onOpen={() => undefined} onPlay={onPlay} onPrint={() => undefined} onError={() => undefined} />);
    const items = await screen.findByTestId("setlist-items");
    const rows = () =>
      within(items)
        .getAllByRole("listitem")
        .map((li) => `${li.querySelector(".setlist-n")!.textContent} ${li.getAttribute("aria-label")}`);
    const moves = () => within(items).queryAllByRole("button", { name: /^(Move (up|down)|Nach (oben|unten))$/ });
    expect(moves().length).toBeGreaterThan(0);

    fireEvent.change(screen.getByTestId("setlist-view"), { target: { value: "title" } });
    // an index of the set: sorted by title, each song with its fixed position
    expect(rows()).toEqual(["3 Apple Song", "2 Moon Song", "1 Zebra Song"]);
    expect(moves()).toHaveLength(0);
    expect(within(items).getAllByRole("listitem")[0]!.getAttribute("draggable")).toBe("false");
    // Alt+arrow does nothing here: no PATCH, the stored order stays
    fireEvent.keyDown(within(items).getAllByRole("listitem")[0]!, { key: "ArrowUp", altKey: true });
    // playing from "Apple Song" starts at its place in the setlist, not at the top of the view
    fireEvent.click(within(within(items).getAllByRole("listitem")[0]!).getByRole("button", { name: /^(Play on stage from this song|Ab diesem Lied auf der Bühne spielen)$/ }));
    expect(onPlay).toHaveBeenCalledWith(gig, 2);

    fireEvent.change(screen.getByTestId("setlist-view"), { target: { value: "artist" } });
    expect(rows()).toEqual(["1 Zebra Song", "3 Apple Song", "2 Moon Song"]);
    fireEvent.change(screen.getByTestId("setlist-search"), { target: { value: "beta" } });
    expect(rows()).toEqual(["3 Apple Song", "2 Moon Song"]);
    // setlist order with a search: still no reordering
    fireEvent.change(screen.getByTestId("setlist-view"), { target: { value: "setlist" } });
    expect(rows()).toEqual(["2 Moon Song", "3 Apple Song"]);
    expect(moves()).toHaveLength(0);
    // removing a song from a searched view drops just that song from the stored order
    fireEvent.click(within(within(items).getAllByRole("listitem")[0]!).getByRole("button", { name: /^(Remove from the setlist|Aus der Setliste entfernen)$/ }));
    await waitFor(() => expect(setlistUpdate).toHaveBeenCalledTimes(1));
    expect(setlistUpdate.mock.calls[0]![1].items!.map((i) => i.sheet_id)).toEqual(["z", "p"]);

    // the view is remembered per setlist; the search is not
    fireEvent.change(screen.getByTestId("setlist-view"), { target: { value: "title" } });
    unmount();
    render(<Setlists api={api} list={list} onOpen={() => undefined} onPlay={onPlay} onPrint={() => undefined} onError={() => undefined} />);
    expect(((await screen.findByTestId("setlist-view")) as HTMLSelectElement).value).toBe("title");
    expect((screen.getByTestId("setlist-search") as HTMLInputElement).value).toBe("");
  });
});
