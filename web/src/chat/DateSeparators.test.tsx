import { cleanup, render, screen } from "@testing-library/react";
import { ApiClient } from "../api/client";
import { mockFetch } from "../test/mockFetch";
import { TalkPage } from "../talk/TalkPage";
import { afterEach, describe, expect, it } from "vitest";
import { I18nextProvider } from "react-i18next";
import i18n from "../i18n";
import type { ChatMessage, Conversation } from "../types";
import { MessageView } from "./MessageView";

function msg(
  id: number,
  role: "user" | "assistant",
  at: string | null,
): ChatMessage {
  return {
    id,
    role,
    content: `Text ${id}`,
    model: null,
    node: null,
    fallback_from: null,
    status: "complete",
    error: null,
    prompt_tokens: null,
    completion_tokens: null,
    first_token_ms: null,
    duration_ms: null,
    context_trimmed: 0,
    created_at: at,
  };
}

afterEach(cleanup);

describe("the time on a message", () => {
  it("is shown on user and assistant messages with the full time as hover text", () => {
    render(
      <I18nextProvider i18n={i18n}>
        <MessageView
          message={msg(1, "user", "2026-10-02T12:34:56Z")}
          busy={false}
        />
        <MessageView
          message={msg(2, "assistant", "2026-10-02T12:35:10Z")}
          busy={false}
        />
      </I18nextProvider>,
    );
    const times = document.querySelectorAll("time.message-time");
    expect(times).toHaveLength(2);
    expect(times[0]?.getAttribute("datetime")).toBe("2026-10-02T12:34:56Z");
    expect(times[0]?.textContent).toMatch(/\d{1,2}[:.]\d{2}/);
    expect(times[0]?.getAttribute("title")).toContain("2026");
    expect(screen.getAllByTestId(/^message-/)).toHaveLength(2);
  });

  it("is left out, not invented, for a message without a time", () => {
    render(
      <I18nextProvider i18n={i18n}>
        <MessageView message={msg(1, "user", null)} busy={false} />
      </I18nextProvider>,
    );
    expect(document.querySelector("time.message-time")).toBeNull();
  });
});

const CONV: Conversation = {
  id: "c1",
  title: "Zeiten",
  model: null,
  node: null,
  running: false,
  created_at: "2026-01-01T10:00:00Z",
  updated_at: "2026-01-01T10:00:00Z",
  message_count: 4,
};

describe("the date separators", () => {
  it("stand before the first message of each day and nowhere else", async () => {
    const mock = mockFetch({
      "GET /api/chat/models": [],
      "GET /api/chat/settings": {
        user: {
          address: "du",
          memory_enabled: true,
          quality: "balanced",
          internet: true,
        },
        hub: {},
        effective_model: "m",
      },
      "GET /api/chat/tools": {
        groups: [],
        nodes: [],
        internet: { node: null },
      },
      "GET /api/conversations": [CONV],
      "GET /api/conversations/c1": {
        ...CONV,
        messages: [
          msg(1, "user", "2026-03-04T10:00:00Z"),
          msg(2, "assistant", "2026-03-04T10:00:05Z"),
          msg(3, "user", "2026-03-06T10:00:00Z"),
          msg(4, "assistant", "2026-03-06T10:00:05Z"),
        ],
      },
    });
    const api = new ApiClient({ token: "t", fetchImpl: mock.fetch });
    render(
      <I18nextProvider i18n={i18n}>
        <TalkPage api={api} conversationId="c1" />
      </I18nextProvider>,
    );
    expect(await screen.findByText("Text 1")).toBeTruthy();
    const separators = screen.getAllByTestId("day-separator");
    expect(separators).toHaveLength(2); // one per day, not one per message
    expect(separators[0]?.textContent).toContain("2026");
    expect(separators[0]?.textContent).not.toBe(separators[1]?.textContent);
    // order: separator, user, assistant, separator, user, assistant
    const order = [
      ...document.querySelectorAll('[data-testid="day-separator"], .talk-msg'),
    ].map((el) =>
      el.getAttribute("data-testid") === "day-separator" ? "-" : "m",
    );
    expect(order.join("")).toBe("-mm-mm");
  });
});
