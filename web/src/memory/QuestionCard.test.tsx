import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { ApiClient } from "../api/client";
import { jsonResponse, mockFetch } from "../test/mockFetch";
import type { MemoryQuestion } from "../types";
import { QuestionCards } from "./QuestionCard";
import { MEMORY_CHANGED } from "./useQuestions";

afterEach(() => {
  vi.restoreAllMocks();
});

const QUESTION: MemoryQuestion = {
  id: "q1",
  topic: "residence",
  origin: "chat",
  status: "open",
  candidates: [
    {
      id: "a",
      text: "Lebt in Hamburg",
      at: "2026-09-29T10:00:00Z",
      confirmed_count: 0,
      last_confirmed_at: null,
    },
    {
      id: "b",
      text: "Lebt in Berlin",
      at: "2026-09-30T10:00:00Z",
      confirmed_count: 2,
      last_confirmed_at: "2026-10-01T10:00:00Z",
    },
  ],
  resolution: null,
  created_at: "2026-09-30T10:00:00Z",
  answered_at: null,
};

function setup(routes: Record<string, unknown> = {}) {
  const mock = mockFetch(routes);
  const onDone = vi.fn();
  const api = new ApiClient({ token: "t", fetchImpl: mock.fetch });
  render(<QuestionCards api={api} questions={[QUESTION]} onDone={onDone} />);
  return { mock, onDone };
}

describe("QuestionCard", () => {
  it("words the question with the notes, their dates and how often they were confirmed", () => {
    setup();
    const card = screen.getByTestId("question-card");
    expect(within(card).getByRole("heading").textContent).toBe("Wo wohnst du?");
    expect(card.textContent).toContain("Meine Notizen widersprechen sich");
    expect(card.textContent).toContain("„Lebt in Hamburg“");
    expect(card.textContent).toContain("„Lebt in Berlin“");
    expect(card.textContent).toContain("2× bestätigt");
    // one button per candidate, none of them, later
    expect(within(card).getByRole("button", { name: "„Lebt in Hamburg“ stimmt" })).toBeTruthy();
    expect(within(card).getByRole("button", { name: "„Lebt in Berlin“ stimmt" })).toBeTruthy();
    expect(within(card).getByRole("button", { name: "Keins davon" })).toBeTruthy();
    expect(within(card).getByRole("button", { name: "Später" })).toBeTruthy();
  });

  it("asks the concrete question when the memory could name the point of the disagreement", () => {
    const mock = mockFetch({});
    const api = new ApiClient({ token: "t", fetchImpl: mock.fetch });
    render(
      <QuestionCards
        api={api}
        questions={[{ ...QUESTION, question: "Läuft das Modell auf node-a oder auf node-b?" }]}
        onDone={vi.fn()}
      />,
    );
    const card = screen.getByTestId("question-card");
    expect(within(card).getByTestId("question-concrete").textContent).toBe(
      "Läuft das Modell auf node-a oder auf node-b?",
    );
    expect(card.textContent).not.toContain("Meine Notizen widersprechen sich");
    expect(card.textContent).toContain("Dazu meine Notizen:");
    expect(within(card).getByRole("button", { name: "„Lebt in Berlin“ stimmt" })).toBeTruthy();
  });

  it("sends the chosen note and tells the rest of the page the memory changed", async () => {
    const { mock, onDone } = setup({ "POST /api/memory/questions/q1/answer": { ok: true } });
    const changed = vi.fn();
    window.addEventListener(MEMORY_CHANGED, changed);
    fireEvent.click(screen.getByRole("button", { name: "„Lebt in Berlin“ stimmt" }));
    await waitFor(() => expect(onDone).toHaveBeenCalled());
    window.removeEventListener(MEMORY_CHANGED, changed);
    expect(mock.calls.find((c) => c.method === "POST")?.body).toEqual({ note_id: "b" });
    expect(changed).toHaveBeenCalled();
  });

  it("sends 'none of them' and a free text answer", async () => {
    const { mock, onDone } = setup({ "POST /api/memory/questions/q1/answer": { ok: true } });
    fireEvent.click(screen.getByRole("button", { name: "Keins davon" }));
    await waitFor(() => expect(onDone).toHaveBeenCalledTimes(1));
    expect(mock.calls.at(-1)?.body).toEqual({ none: true });

    const send = screen.getByRole("button", { name: "Senden" });
    expect((send as HTMLButtonElement).disabled).toBe(true);
    fireEvent.change(screen.getByLabelText("Oder schreib, was stimmt"), {
      target: { value: "  Wohnt in Musterstadt " },
    });
    fireEvent.click(screen.getByRole("button", { name: "Senden" }));
    await waitFor(() => expect(onDone).toHaveBeenCalledTimes(2));
    expect(mock.calls.at(-1)?.body).toEqual({ text: "Wohnt in Musterstadt" });
  });

  it("puts a question aside", async () => {
    const { mock, onDone } = setup({ "POST /api/memory/questions/q1/dismiss": { ok: true } });
    fireEvent.click(screen.getByRole("button", { name: "Später" }));
    await waitFor(() => expect(onDone).toHaveBeenCalled());
    expect(mock.calls.some((c) => c.url === "/api/memory/questions/q1/dismiss")).toBe(true);
  });

  it("shows why an answer was refused and stays open", async () => {
    const { onDone } = setup({
      "POST /api/memory/questions/q1/answer": () =>
        jsonResponse(
          { detail: { code: "secret_blocked", message: "This text looks like a secret." } },
          422,
        ),
    });
    fireEvent.click(screen.getByRole("button", { name: "Keins davon" }));
    expect((await screen.findByRole("alert")).textContent).toContain("looks like a secret");
    expect(onDone).not.toHaveBeenCalled();
    expect(
      (screen.getByRole("button", { name: "Keins davon" }) as HTMLButtonElement).disabled,
    ).toBe(false);
  });

  it("shows nothing without questions", () => {
    const mock = mockFetch({});
    const api = new ApiClient({ token: "t", fetchImpl: mock.fetch });
    const { container } = render(<QuestionCards api={api} questions={[]} onDone={vi.fn()} />);
    expect(container.textContent).toBe("");
  });
});
