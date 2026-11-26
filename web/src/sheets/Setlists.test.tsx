import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import type { ApiClient } from "../api/client";
import { Setlists } from "./Setlists";
import type { Setlist, SetlistItem, SheetSummary } from "./types";

const sheet = (id: string, title: string): SheetSummary => ({
  id,
  title,
  artist: "Example Band",
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
    render(<Setlists api={api} list={list} onOpen={() => undefined} onPlay={() => undefined} onError={() => undefined} />);
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
    render(<Setlists api={api} list={list} onOpen={() => undefined} onPlay={() => undefined} onError={onError} />);
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
});
