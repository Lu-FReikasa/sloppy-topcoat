import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { ApiClient } from "../api/client";
import { takeIntent } from "../layout/intent";
import { jsonResponse, mockFetch } from "../test/mockFetch";
import type { ChatMessage, WorkCard } from "../types";
import { placeCards, WorkCardView } from "./WorkCards";

const card = (over: Partial<WorkCard> = {}): WorkCard => ({
  id: "o1",
  employee: "admin",
  employee_title: "Verwaltung",
  topic: "Plattenplatz ansehen",
  status: "queued",
  report_when_done: false,
  created_at: "2026-01-01T10:00:05Z",
  chat_message_id: 2,
  summary: null,
  withheld: [],
  direct_conversation_id: null,
  ...over,
});

const at = (id: number, created_at: string) => ({ id, created_at }) as unknown as ChatMessage;

describe("placeCards", () => {
  const messages = [
    at(1, "2026-01-01T10:00:00Z"),
    at(2, "2026-01-01T10:00:04Z"),
    at(3, "2026-01-01T10:01:00Z"),
    at(4, "2026-01-01T10:02:00Z"),
  ];

  it("puts a card under the message that handed it on", () => {
    const placed = placeCards(messages, [card(), card({ id: "o2", chat_message_id: 4 })]);
    expect(placed.get(2)?.map((c) => c.id)).toEqual(["o1"]);
    expect(placed.get(4)?.map((c) => c.id)).toEqual(["o2"]);
  });

  it("without that message, under the last one written before the card", () => {
    const placed = placeCards(messages, [
      card({ chat_message_id: null, created_at: "2026-01-01T10:01:30Z" }),
      card({ id: "o2", chat_message_id: 99, created_at: "2025-12-31T00:00:00Z" }),
      card({ id: "o3", chat_message_id: null, created_at: null }),
    ]);
    expect(placed.get(3)?.map((c) => c.id)).toEqual(["o1"]);
    expect(placed.get(1)?.map((c) => c.id)).toEqual(["o2"]); // older than all: the first
    expect(placed.get(4)?.map((c) => c.id)).toEqual(["o3"]); // no time: the end
  });

  it("places nothing in an empty conversation", () => {
    expect(placeCards([], [card()]).size).toBe(0);
  });
});

describe("WorkCardView", () => {
  it("shows the employee, the task and its state in words", () => {
    render(<WorkCardView card={card({ report_when_done: true })} />);
    expect(screen.getByText("Verwaltung")).toBeTruthy();
    expect(screen.getByText("Plattenplatz ansehen")).toBeTruthy();
    expect(screen.getByTestId("work-card-state").textContent).toBe("Wartet in der Warteschlange");
    expect(screen.getByText(/Bescheid, wenn es fertig ist/)).toBeTruthy();
    expect(screen.queryByTestId("work-card-result")).toBeNull();
  });

  it("shows a finished task's summary and no promise to report", () => {
    render(
      <WorkCardView
        card={card({ status: "done", report_when_done: true, summary: "Die Platte ist zu **80 %** voll." })}
      />,
    );
    expect(screen.getByTestId("work-card-state").textContent).toBe("Fertig");
    expect(screen.getByText("80 %").tagName).toBe("STRONG");
    expect(screen.queryByText(/Bescheid, wenn es fertig ist/)).toBeNull();
  });

  it("opens the employee's own conversation in the employee's program", async () => {
    const mock = mockFetch({
      "POST /api/orders/o1/direct?language=de": jsonResponse({
        conversation_id: "c9",
        application: "admin",
      }),
    });
    const api = new ApiClient({ token: "t", fetchImpl: mock.fetch });
    render(<WorkCardView card={card()} api={api} />);
    fireEvent.click(screen.getByRole("button", { name: "Direkt mit Verwaltung sprechen" }));
    await waitFor(() => expect(mock.calls.some((c) => c.method === "POST")).toBe(true));
    await waitFor(() =>
      expect(takeIntent()).toEqual({ module: "page.chat", props: { id: "c9" }, program: "admin" }),
    );
  });

  it("says so when the conversation could not be opened", async () => {
    const mock = mockFetch({});
    const api = new ApiClient({ token: "t", fetchImpl: mock.fetch });
    render(<WorkCardView card={card()} api={api} />);
    fireEvent.click(screen.getByRole("button", { name: "Direkt mit Verwaltung sprechen" }));
    expect((await screen.findByRole("alert")).textContent).toMatch(/nicht öffnen/);
    expect(takeIntent()).toBeNull();
  });
});
