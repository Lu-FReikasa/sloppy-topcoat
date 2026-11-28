// What the full chat view proved and the calm view must keep proving: the answer streams in and is
// kept, the person sends with Enter, Stop is a call of its own, the offer to check an answer, the
// note that an announcement was not done, and the honest lines about what the memory did.
import {
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
  within,
} from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it } from "vitest";
import { ApiClient } from "../api/client";
import { jsonResponse, mockFetch, type Handler } from "../test/mockFetch";
import { controlledSse, sseResponse } from "../test/sse";
import type { ChatMessage, Conversation, ConversationDetail } from "../types";
import { TalkPage } from "./TalkPage";

const CONV: Conversation = {
  id: "c1",
  title: "First chat",
  model: null,
  node: null,
  created_at: "2026-01-01T10:00:00Z",
  updated_at: "2026-01-01T10:00:00Z",
  message_count: 2,
};

function msg(
  id: number,
  role: "user" | "assistant",
  content: string,
): ChatMessage {
  return {
    id,
    role,
    content,
    model: role === "assistant" ? "qwen-7b" : null,
    node: role === "assistant" ? "node-amd" : null,
    fallback_from: null,
    status: "complete",
    error: null,
    prompt_tokens: null,
    completion_tokens: role === "assistant" ? 12 : null,
    first_token_ms: null,
    duration_ms: role === "assistant" ? 3100 : null,
    context_trimmed: 0,
    created_at: "2026-01-01T10:00:00Z",
  };
}

const DETAIL: ConversationDetail = {
  ...CONV,
  messages: [msg(1, "user", "Hello?"), msg(2, "assistant", "Hello **there**")],
};

const META = {
  conversation_id: "c1",
  user_message: msg(3, "user", "Next question"),
  continues: null,
  memory: [],
  model: "qwen-7b",
  node: "node-amd",
  fallback_from: null,
  fallback_state: null,
  context: { length: 4096, estimated_prompt_tokens: 40, trimmed: 0 },
};

function setup(extra: Record<string, Handler | unknown> = {}) {
  const mock = mockFetch({
    "GET /api/conversations": [CONV],
    "GET /api/conversations/c1": DETAIL,
    "GET /api/conversations/c1/memory": [],
    ...extra,
  });
  const api = new ApiClient({ token: "t", fetchImpl: mock.fetch });
  render(<TalkPage api={api} />);
  return { mock };
}

/** The messages that were sent (the page also asks the hub whether a new section begins). */
function sent(mock: {
  calls: { method: string; url: string; body?: unknown }[];
}) {
  return mock.calls.filter(
    (c) => c.method === "POST" && c.url.endsWith("/messages"),
  );
}

async function type(text: string) {
  await screen.findByText("there"); // the conversation has been opened
  const box = await screen.findByLabelText("Schreib mir …");
  fireEvent.change(box, { target: { value: text } });
  fireEvent.click(screen.getByRole("button", { name: "Senden" }));
}

beforeEach(() => {
  window.location.hash = "";
});
afterEach(cleanup);

describe("the answer", () => {
  it("streams in and is kept", async () => {
    const { mock } = setup({
      "POST /api/conversations/c1/messages": () =>
        sseResponse([
          ["meta", META],
          ["delta", { text: "Fine, " }],
          ["delta", { text: "thanks" }],
          ["done", { message: msg(4, "assistant", "Fine, thanks") }],
        ]),
    });
    await type("Next question");
    expect(await screen.findByText("Fine, thanks")).toBeTruthy();
    expect(screen.getByText("Next question")).toBeTruthy();
    expect(sent(mock)[0]?.body).toEqual({
      content: "Next question",
      tools: "auto",
    });
    expect(screen.queryByTestId("talk-live")).toBeNull();
  });

  it("says so when the machine woken for it did not come, naming it (#102)", async () => {
    setup({
      "POST /api/conversations/c1/messages": () =>
        sseResponse([
          [
            "error",
            {
              code: "wake_timeout",
              message:
                "node-amd did not wake within 240 s to serve 'qwen-7b' (Wake-on-LAN sent; …)",
              retry: true,
            },
          ],
        ]),
    });
    await type("Next question");
    const alert = await screen.findByRole("alert");
    expect(alert.textContent).toBe(
      "Die Maschine für dieses Modell (node-amd) wurde geweckt, ist aber nicht rechtzeitig online gekommen. Versuch es gleich noch einmal.",
    );
  });

  it("keeps the general line for other failures", async () => {
    setup({
      "POST /api/conversations/c1/messages": () =>
        sseResponse([["error", { code: "stream_broken", message: "lost", retry: false }]]),
    });
    await type("Next question");
    expect((await screen.findByRole("alert")).textContent).toBe(
      "Das hat gerade nicht geklappt. Versuch es gleich noch einmal.",
    );
  });

  it("is sent with Enter but not with Shift+Enter", async () => {
    const { mock } = setup({
      "POST /api/conversations/c1/messages": () =>
        sseResponse([["done", { message: msg(4, "assistant", "ok") }]]),
    });
    await screen.findByText("there");
    const box = await screen.findByLabelText("Schreib mir …");
    fireEvent.change(box, { target: { value: "hi" } });
    fireEvent.keyDown(box, { key: "Enter", shiftKey: true });
    expect(sent(mock)).toHaveLength(0);
    fireEvent.keyDown(box, { key: "Enter" });
    await waitFor(() => expect(sent(mock)).toHaveLength(1));
  });

  it("is stopped with a call of its own, and what the hub stored stays", async () => {
    const live = controlledSse();
    let aborted = false;
    const stored = {
      ...msg(77, "assistant", "Partial"),
      status: "stopped" as const,
    };
    const { mock } = setup({
      "POST /api/conversations/c1/messages": (init?: RequestInit) => {
        init?.signal?.addEventListener("abort", () => {
          aborted = true;
        });
        return live.response;
      },
      "POST /api/chat/messages/77/stop": () => {
        live.push("stopped", { message: stored, interrupted: false }, 2);
        live.close();
        return jsonResponse(stored);
      },
    });
    await type("Next question");
    live.push("meta", { ...META, message_id: 77 }, 0);
    live.push("delta", { text: "Partial" }, 1);
    expect(await screen.findByText("Partial")).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "Stopp" }));
    await waitFor(
      () => expect(screen.queryByRole("button", { name: "Stopp" })).toBeNull(),
      { timeout: 4000 },
    );
    expect(screen.getByText("Partial")).toBeTruthy();
    expect(
      mock.calls.some(
        (c) => c.method === "POST" && c.url === "/api/chat/messages/77/stop",
      ),
    ).toBe(true);
    expect(aborted).toBe(false); // closing the connection is not what stops the answer
  });
});

describe("the offer to check an answer", () => {
  it("is made under an answer given in doubt, and asks for a forced check", async () => {
    const { mock } = setup({
      "POST /api/conversations/c1/messages": () =>
        sseResponse([
          ["meta", { ...META, check_offer: true }],
          ["delta", { text: "A container is a process." }],
          [
            "done",
            { message: msg(4, "assistant", "A container is a process.") },
          ],
        ]),
    });
    await type("Warum startet der Container neu?");
    await screen.findByText("A container is a process.");
    const offer = await screen.findByTestId("check-offer");
    expect(offer.textContent).toContain("allgemeinem Wissen");
    fireEvent.click(
      within(offer).getByRole("button", { name: "Auf meinen Servern prüfen" }),
    );
    await waitFor(() => expect(sent(mock)).toHaveLength(2));
    expect(sent(mock)[1]?.body).toEqual({
      content: "Prüfe das bitte auf meinen Servern.",
      tools: "on",
    });
    await waitFor(() => expect(screen.queryByTestId("check-offer")).toBeNull());
  });

  it("is not made for a plain answer", async () => {
    setup({
      "POST /api/conversations/c1/messages": () =>
        sseResponse([
          ["meta", META],
          [
            "done",
            { message: msg(4, "assistant", "A container is a process.") },
          ],
        ]),
    });
    await type("Was ist ein Docker-Container?");
    await screen.findByText("A container is a process.");
    expect(screen.queryByTestId("check-offer")).toBeNull();
  });
});

describe("what the memory did", () => {
  it("shows what was remembered, with an undo, and refused secrets", async () => {
    const { mock } = setup({
      "POST /api/conversations/c1/messages": () =>
        sseResponse([
          ["meta", { ...META, memory: [] }],
          ["delta", { text: "Nice." }],
          ["done", { message: msg(4, "assistant", "Nice.") }],
          [
            "memory",
            {
              message_id: 4,
              remembered: [
                { id: "n1", text: "Has a dog called Bello", evidence: "doc" },
              ],
              confirmed: [],
              blocked: 1,
            },
          ],
        ]),
      "DELETE /api/memory/n1": { id: "n1", status: "deleted" },
    });
    await type("My dog is called Bello");
    const notes = await screen.findByTestId("memory-notes");
    expect(notes.textContent).toContain("Gemerkt: Has a dog called Bello");
    expect(screen.getByTestId("memory-blocked").textContent).toContain(
      "1 Eintrag",
    );
    fireEvent.click(within(notes).getByRole("button", { name: "Rückgängig" }));
    await waitFor(() =>
      expect(screen.queryByText(/Has a dog called Bello/)).toBeNull(),
    );
    expect(
      mock.calls.some(
        (c) => c.method === "DELETE" && c.url === "/api/memory/n1",
      ),
    ).toBe(true);
  });

  it("says why nothing was noted when the person asked for it", async () => {
    setup({
      "POST /api/conversations/c1/messages": () =>
        sseResponse([
          ["meta", { ...META, memory: [] }],
          ["delta", { text: "Verstanden." }],
          ["done", { message: msg(4, "assistant", "Verstanden.") }],
          [
            "memory",
            {
              message_id: 4,
              remembered: [],
              confirmed: [],
              blocked: 1,
              not_noted: "secret",
            },
          ],
        ]),
    });
    await type("Das WLAN-Passwort ist geheim123, notiere das");
    const line = await screen.findByTestId("memory-not-noted");
    expect(line.textContent).toBe(
      "Nicht gemerkt, weil es wie ein Geheimnis aussah (Passwort, Schlüssel …).",
    );
    expect(screen.queryByTestId("memory-blocked")).toBeNull(); // the reason is said once
  });
});

describe("an answer that announced something it did not do", () => {
  it("is said plainly, with an offer to do it", async () => {
    const said = "Gut, ich prüfe das jetzt.";
    const item = {
      id: 1,
      kind: "announce",
      status: "failed",
      title: "",
      detail: said,
      data: {},
      at: "2026-10-21T10:00:00.000Z",
      ms: 0,
    };
    const { mock } = setup({
      "POST /api/conversations/c1/messages": () =>
        sseResponse([
          ["meta", META],
          ["activity", { op: "add", item }],
          ["delta", { text: said }],
          [
            "done",
            { message: { ...msg(4, "assistant", said), activity: [item] } },
          ],
        ]),
    });
    await type("Wie voll ist die Platte?");
    const note = await screen.findByTestId("announce-note");
    expect(note.textContent).toContain("nichts geschehen");
    fireEvent.click(
      within(note).getByRole("button", { name: "Jetzt ausführen" }),
    );
    await waitFor(() => expect(sent(mock)).toHaveLength(2));
    expect(sent(mock)[1]?.body).toMatchObject({
      content: "Bitte führe das jetzt aus.",
    });
  });
});
