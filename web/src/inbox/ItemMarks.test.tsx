import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { ApiClient } from "../api/client";
import { jsonResponse, mockFetch } from "../test/mockFetch";
import type { ItemMark } from "../types";
import { ItemMarks } from "./ItemMarks";

function mark(over: Partial<ItemMark> = {}): ItemMark {
  return {
    id: "m1",
    item_id: "i1",
    author: "person",
    kind: "postit",
    text: "Zahl prüfen",
    line: null,
    created_at: null,
    resolved_at: null,
    ...over,
  };
}

function hub(start: ItemMark[] = []) {
  let marks = start;
  const mock = mockFetch({
    "GET /api/inbox/i1/marks": () => jsonResponse({ marks }),
    "POST /api/inbox/i1/marks": (init?: RequestInit) => {
      const body = JSON.parse(String(init?.body)) as {
        kind: ItemMark["kind"];
        text: string;
      };
      const made = mark({ id: "m2", kind: body.kind, text: body.text });
      marks = [...marks, made];
      return jsonResponse(made, 201);
    },
    "POST /api/inbox/marks/m1/resolve": () => {
      marks = marks.map((m) => ({ ...m, resolved_at: "2026-10-22T10:00:00Z" }));
      return jsonResponse(marks[0]);
    },
    "DELETE /api/inbox/marks/m1": () => {
      marks = [];
      return new Response(null, { status: 204 });
    },
  });
  return { mock, api: new ApiClient({ token: "t", fetchImpl: mock.fetch }) };
}

describe("ItemMarks", () => {
  it("shows the marks of an item, the re-check's by its own name", async () => {
    const { api } = hub([
      mark(),
      mark({
        id: "m3",
        kind: "sticker",
        author: "recheck",
        text: "noch richtig, 2026-10-22",
      }),
    ]);
    render(<ItemMarks api={api} itemId="i1" />);
    expect(await screen.findByText("Zahl prüfen")).toBeTruthy();
    const marks = await screen.findAllByTestId("mark");
    expect(marks).toHaveLength(2);
    expect(marks[1]?.textContent).toContain("Nachprüfung");
  });

  it("adds a mark of the chosen kind", async () => {
    const { api, mock } = hub();
    render(<ItemMarks api={api} itemId="i1" />);
    fireEvent.change(await screen.findByLabelText("Art der Markierung"), {
      target: { value: "comment" },
    });
    fireEvent.change(screen.getByLabelText("Darauf schreiben …"), {
      target: { value: "Quelle fehlt" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Aufkleben" }));
    await waitFor(() =>
      expect(
        mock.calls.find((c) => c.method === "POST" && c.url.endsWith("/marks"))
          ?.body,
      ).toEqual({ kind: "comment", text: "Quelle fehlt" }),
    );
    expect(await screen.findByText("Quelle fehlt")).toBeTruthy();
  });

  it("does not send an empty Post-It, but a highlight needs no text", async () => {
    const { api, mock } = hub();
    render(<ItemMarks api={api} itemId="i1" />);
    fireEvent.click(await screen.findByRole("button", { name: "Aufkleben" }));
    expect(mock.calls.some((c) => c.method === "POST")).toBe(false);
    fireEvent.change(screen.getByLabelText("Art der Markierung"), {
      target: { value: "highlight" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Aufkleben" }));
    await waitFor(() =>
      expect(mock.calls.some((c) => c.method === "POST")).toBe(true),
    );
  });

  it("resolves and removes a mark", async () => {
    const { api, mock } = hub([mark()]);
    render(<ItemMarks api={api} itemId="i1" />);
    fireEvent.click(await screen.findByRole("button", { name: "Erledigt" }));
    await waitFor(() =>
      expect(mock.calls.some((c) => c.url.endsWith("/resolve"))).toBe(true),
    );
    fireEvent.click(await screen.findByRole("button", { name: "Entfernen" }));
    await waitFor(() =>
      expect(mock.calls.some((c) => c.method === "DELETE")).toBe(true),
    );
    await waitFor(() => expect(screen.queryByTestId("mark")).toBeNull());
  });

  it("only shows the marks where there is no room for a form (the board)", async () => {
    const { api } = hub([mark()]);
    render(<ItemMarks api={api} itemId="i1" compact />);
    expect(await screen.findByText("Zahl prüfen")).toBeTruthy();
    expect(screen.queryByRole("button", { name: "Aufkleben" })).toBeNull();
    expect(screen.queryByRole("button", { name: "Entfernen" })).toBeNull();
  });
});
