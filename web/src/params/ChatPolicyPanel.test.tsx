import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { ApiClient } from "../api/client";
import { jsonResponse, mockFetch } from "../test/mockFetch";
import type { ChatPolicyView } from "../types";
import { ChatPolicyPanel, docOf, formOf } from "./ChatPolicyPanel";

const view = (policy: Record<string, unknown> = {}): ChatPolicyView => ({
  policy,
  levels: {},
  numbers: {},
});

describe("the document and the form", () => {
  it("shows a table at each level and a single value at all of them", () => {
    const form = formOf(
      view({
        max_turns: { effort: { fast: 4, balanced: 8, thorough: 20 } },
        second_look: { effort: { fast: false, balanced: false, thorough: true } },
        recall: false,
        node: "node-big",
      }),
    );
    expect(form.numbers.max_turns).toEqual({ fast: "4", balanced: "8", thorough: "20" });
    expect(form.secondLook).toEqual({ fast: "false", balanced: "false", thorough: "true" });
    expect(form.switches.recall).toBe("false");
    expect(form.node).toBe("node-big");
  });

  it("writes one value when every level says the same, a table when they differ, nothing when empty", () => {
    const form = formOf(view());
    expect(docOf(form)).toEqual({});
    form.numbers.max_turns = { fast: "6", balanced: "6", thorough: "6" };
    form.numbers.max_tool_calls = { fast: "2", balanced: "", thorough: "9" };
    form.secondLook = { fast: "", balanced: "", thorough: "true" };
    form.switches.review = "false";
    form.node = " node-big ";
    expect(docOf(form)).toEqual({
      max_turns: 6,
      max_tool_calls: { effort: { fast: 2, thorough: 9 } },
      second_look: { effort: { thorough: true } },
      review: false,
      node: "node-big",
    });
  });
});

describe("ChatPolicyPanel", () => {
  it("saves what was typed and resets to the hub's rules", async () => {
    const mock = mockFetch({
      "GET /api/scheduling/chat-policy": () => jsonResponse(view()),
      "PUT /api/scheduling/chat-policy": () => jsonResponse(view({ max_turns: 9 })),
    });
    const api = new ApiClient({ token: "t", fetchImpl: mock.fetch });
    render(<ChatPolicyPanel api={api} />);
    fireEvent.change(await screen.findByLabelText("Höchstens Runden des Modells (Schnell)"), {
      target: { value: "9" },
    });
    fireEvent.change(screen.getByLabelText("Höchstens Runden des Modells (Ausgewogen)"), {
      target: { value: "9" },
    });
    fireEvent.change(screen.getByLabelText("Höchstens Runden des Modells (Gründlich)"), {
      target: { value: "9" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Speichern" }));
    await waitFor(() => expect(mock.calls.some((c) => c.method === "PUT")).toBe(true));
    expect(mock.calls.find((c) => c.method === "PUT")?.body).toEqual({ policy: { max_turns: 9 } });
    expect(await screen.findByText("Gespeichert.")).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "Zurück zu den Regeln des Hubs" }));
    await waitFor(() => expect(mock.calls.filter((c) => c.method === "PUT")).toHaveLength(2));
    expect(mock.calls.filter((c) => c.method === "PUT")[1]?.body).toEqual({ policy: {} });
  });

  it("shows what the hub refused", async () => {
    const mock = mockFetch({
      "GET /api/scheduling/chat-policy": () => jsonResponse(view()),
      "PUT /api/scheduling/chat-policy": () =>
        jsonResponse({ detail: { code: "invalid_chat_policy", message: "max_turns: a number from 1 to 100" } }, 400),
    });
    const api = new ApiClient({ token: "t", fetchImpl: mock.fetch });
    render(<ChatPolicyPanel api={api} />);
    fireEvent.click(await screen.findByRole("button", { name: "Speichern" }));
    expect(await screen.findByRole("alert")).toBeTruthy();
  });
});
