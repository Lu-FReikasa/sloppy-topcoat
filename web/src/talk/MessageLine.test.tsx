import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { ApiClient } from "../api/client";
import { takeCompose } from "../desktop/compose";
import { jsonResponse, mockFetch } from "../test/mockFetch";
import type { ChatMessage } from "../types";
import { MessageLine } from "./MessageLine";

const CONV = "c74886fe-6b2c-471e-bd99-95be71e84c91";
const REF = "trace:c74886fe:1692:5f3a9b01c2";

const message = {
  id: 1692,
  role: "assistant",
  content: "Antwort",
  status: "complete",
  created_at: "2026-10-23T18:36:20Z",
} as unknown as ChatMessage;

function setup() {
  const mock = mockFetch({
    [`POST /api/conversations/${CONV}/messages/1692/reference`]: () =>
      jsonResponse({ reference: REF, short: "c74886fe:1692" }),
  });
  const api = new ApiClient({ token: "t", fetchImpl: mock.fetch });
  render(<MessageLine api={api} conversationId={CONV} message={message} />);
  return mock;
}

afterEach(() => {
  takeCompose("dev");
  vi.restoreAllMocks();
});

describe("MessageLine", () => {
  it("shows the time and the number of the message", () => {
    setup();
    expect(document.querySelector("time")?.getAttribute("datetime")).toBe(
      "2026-10-23T18:36:20Z",
    );
    expect(screen.getByText("#1692")).toBeTruthy();
  });

  it("copies the reference the hub signed for this message", async () => {
    const writeText = vi.fn().mockResolvedValue(undefined);
    Object.defineProperty(navigator, "clipboard", {
      value: { writeText },
      configurable: true,
    });
    const mock = setup();
    fireEvent.click(screen.getByRole("button", { name: "Referenz kopieren" }));
    await waitFor(() => expect(writeText).toHaveBeenCalledWith(REF));
    expect(
      mock.calls.some((c) => c.url.endsWith("/messages/1692/reference")),
    ).toBe(true);
    expect(await screen.findByText("Kopiert")).toBeTruthy();
  });

  it("hands the answer to the development chat with its reference", async () => {
    setup();
    fireEvent.click(
      screen.getByRole("button", { name: "In Entwicklung untersuchen" }),
    );
    await waitFor(() => expect(takeCompose("dev")).toContain(REF));
  });
});
