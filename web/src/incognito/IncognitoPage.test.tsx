import {
  act,
  fireEvent,
  render,
  screen,
  waitFor,
} from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { ApiClient } from "../api/client";
import { jsonResponse, mockFetch } from "../test/mockFetch";
import type { IncognitoMessage, IncognitoTab } from "../types";
import { burnFrame } from "./Burning";
import { IncognitoPage } from "./IncognitoPage";

function tab(id: string, over: Partial<IncognitoTab> = {}): IncognitoTab {
  return {
    id,
    title: "",
    internet: true,
    count: 0,
    created_at: 1,
    updated_at: 1,
    ...over,
  };
}

/** A small hub that keeps the tabs in memory, like the real one. */
function hub(start: IncognitoTab[] = []) {
  let tabs = start;
  const messages: Record<string, IncognitoMessage[]> = {};
  const mock = mockFetch({
    "GET /api/incognito": () => jsonResponse({ tabs }),
    "POST /api/incognito": () => {
      const made = tab("t1");
      tabs = [made, ...tabs];
      return jsonResponse({ ...made, messages: [] }, 201);
    },
    "GET /api/incognito/t1": () =>
      jsonResponse({
        ...tabs.find((x) => x.id === "t1"),
        messages: messages.t1 ?? [],
      }),
    "PATCH /api/incognito/t1": (init?: RequestInit) => {
      const body = JSON.parse(String(init?.body)) as { internet: boolean };
      tabs = tabs.map((x) =>
        x.id === "t1" ? { ...x, internet: body.internet } : x,
      );
      return jsonResponse({ ...tabs.find((x) => x.id === "t1"), messages: [] });
    },
    "POST /api/incognito/t1/messages": (init?: RequestInit) => {
      const body = JSON.parse(String(init?.body)) as { content: string };
      const answer = { role: "assistant" as const, content: "Berlin." };
      messages.t1 = [
        ...(messages.t1 ?? []),
        { role: "user", content: body.content },
        answer,
      ];
      tabs = tabs.map((x) =>
        x.id === "t1" ? { ...x, title: body.content, count: 2 } : x,
      );
      return jsonResponse({ message: answer, tab: tabs[0] });
    },
    "DELETE /api/incognito/t1": () => {
      tabs = tabs.filter((x) => x.id !== "t1");
      return new Response(null, { status: 204 });
    },
  });
  return { mock, api: new ApiClient({ token: "t", fetchImpl: mock.fetch }) };
}

function noMotion(reduced: boolean) {
  window.matchMedia = ((q: string) => ({
    matches: reduced,
    media: q,
    addEventListener() {},
    removeEventListener() {},
  })) as unknown as typeof window.matchMedia;
}

describe("IncognitoPage", () => {
  beforeEach(() => noMotion(true));
  afterEach(() => {
    vi.useRealTimers();
    vi.restoreAllMocks();
  });

  it("says plainly what is not recorded and what is not hidden", async () => {
    const { api } = hub();
    render(<IncognitoPage api={api} />);
    const notice = await screen.findByTestId("incognito-notice");
    expect(notice.textContent).toContain("Nicht aufgezeichnet");
    expect(notice.textContent).toContain("Websuche verlässt den Hub");
    expect(await screen.findByText("Noch kein Inkognito-Tab.")).toBeTruthy();
  });

  it("opens a tab, asks and shows the answer; the title is the first question", async () => {
    const { api, mock } = hub();
    render(<IncognitoPage api={api} />);
    fireEvent.click(
      await screen.findByRole("button", { name: "Neuer Inkognito-Tab" }),
    );
    const box = await screen.findByLabelText(
      "Schreib etwas, das niemand behält …",
    );
    fireEvent.change(box, { target: { value: "Hauptstadt von Deutschland?" } });
    fireEvent.click(screen.getByRole("button", { name: "Senden" }));
    expect(await screen.findByText("Berlin.")).toBeTruthy();
    expect(
      screen.getByText("Hauptstadt von Deutschland?", {
        selector: ".talk-user",
      }),
    ).toBeTruthy();
    expect(
      mock.calls.find((c) => c.url === "/api/incognito/t1/messages")?.body,
    ).toEqual({
      content: "Hauptstadt von Deutschland?",
    });
    await waitFor(() =>
      expect(
        screen.getByRole("tab", { name: "Hauptstadt von Deutschland?" }),
      ).toBeTruthy(),
    );
  });

  it("switches the web off for one tab", async () => {
    const { api, mock } = hub([tab("t1")]);
    render(<IncognitoPage api={api} />);
    fireEvent.click(await screen.findByRole("tab"));
    const box = await screen.findByLabelText("Darf das Web nutzen");
    expect((box as HTMLInputElement).checked).toBe(true);
    fireEvent.click(box);
    await waitFor(() =>
      expect(mock.calls.find((c) => c.method === "PATCH")?.body).toEqual({
        internet: false,
      }),
    );
  });

  it("burns a tab: it is deleted and gone from the list", async () => {
    const { api, mock } = hub([tab("t1", { title: "Geheim" })]);
    render(<IncognitoPage api={api} />);
    fireEvent.click(await screen.findByRole("tab", { name: "Geheim" }));
    fireEvent.click(await screen.findByRole("button", { name: "Verbrennen" }));
    await waitFor(() =>
      expect(mock.calls.some((c) => c.method === "DELETE")).toBe(true),
    );
    await waitFor(() =>
      expect(screen.queryByRole("tab", { name: "Geheim" })).toBeNull(),
    );
    expect(await screen.findByText("Noch kein Inkognito-Tab.")).toBeTruthy();
  });

  it("shows the burning before it deletes, when motion is wanted", async () => {
    noMotion(false);
    vi.useFakeTimers({ shouldAdvanceTime: true });
    const { api, mock } = hub([tab("t1", { title: "Geheim" })]);
    render(<IncognitoPage api={api} />);
    fireEvent.click(await screen.findByRole("tab", { name: "Geheim" }));
    fireEvent.click(await screen.findByRole("button", { name: "Verbrennen" }));
    expect(await screen.findByTestId("burning")).toBeTruthy();
    expect(mock.calls.some((c) => c.method === "DELETE")).toBe(false); // not before it has burnt
    await act(async () => {
      await vi.advanceTimersByTimeAsync(1600);
    });
    await waitFor(() =>
      expect(mock.calls.some((c) => c.method === "DELETE")).toBe(true),
    );
  });

  it("keeps the draft and says so when no answer came", async () => {
    const mock = mockFetch({
      "GET /api/incognito": { tabs: [tab("t1")] },
      "GET /api/incognito/t1": { ...tab("t1"), messages: [] },
      "POST /api/incognito/t1/messages": () =>
        jsonResponse({ detail: { code: "model_error" } }, 502),
    });
    render(
      <IncognitoPage
        api={new ApiClient({ token: "t", fetchImpl: mock.fetch })}
      />,
    );
    fireEvent.click(await screen.findByRole("tab"));
    const box = (await screen.findByLabelText(
      "Schreib etwas, das niemand behält …",
    )) as HTMLTextAreaElement;
    fireEvent.change(box, { target: { value: "Hallo" } });
    fireEvent.click(screen.getByRole("button", { name: "Senden" }));
    expect(await screen.findByRole("alert")).toBeTruthy();
    expect(box.value).toBe("Hallo"); // the question is still there to send again
  });
});

describe("burnFrame", () => {
  const text = "Mein geheimer Satz\nin zwei Zeilen";

  it("starts as the text and ends as ash and blanks only", () => {
    expect(burnFrame(text, 0)).toBe(text);
    const end = burnFrame(text, 7);
    expect(end).not.toBe(text);
    expect(/^[░▒· \n]*$/.test(end)).toBe(true);
  });

  it("keeps the shape (lines and blanks) and is the same for the same step", () => {
    for (const step of [1, 3, 5]) {
      const frame = burnFrame(text, step);
      expect(frame.length).toBe(text.length);
      expect(frame.split("\n").map((l) => l.length)).toEqual(
        text.split("\n").map((l) => l.length),
      );
      expect(frame).toBe(burnFrame(text, step));
    }
  });

  it("burns more with every step", () => {
    const burnt = (step: number) =>
      [...burnFrame(text, step)].filter((c, i) => c !== text[i]).length;
    expect(burnt(2)).toBeGreaterThan(burnt(0));
    expect(burnt(5)).toBeGreaterThanOrEqual(burnt(2));
    expect(burnt(7)).toBeGreaterThan(burnt(2));
  });
});
