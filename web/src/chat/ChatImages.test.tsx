import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { ApiClient } from "../api/client";
import { IncognitoPage } from "../incognito/IncognitoPage";
import { TalkPage } from "../talk/TalkPage";
import { jsonResponse, mockFetch } from "../test/mockFetch";
import type { ChatMessage, ConversationDetail } from "../types";
import { acceptable, MAX_IMAGE_BYTES, MAX_IMAGES } from "./images";

/** The eight bytes a PNG file starts with: enough for the browser side, which reads no pixels. */
const PNG_SIGNATURE = "\x89PNG\r\n\x1a\n";
const PNG = Uint8Array.from(PNG_SIGNATURE, (c) => c.charCodeAt(0));
const PNG_BASE64 = btoa(PNG_SIGNATURE);
const THUMB = { width: 40, height: 80 };
const TINY = { width: 4, height: 4 };
const OK = 200;
const CREATED = 201;
const QUESTION_ID = 1;
const ANSWER_ID = 2;
/** A data URL of a PNG: matches "data:image/png;base64,iVBO", not "blob:picture". */
const PNG_DATA_URL = /^data:image\/png;base64,/;

function pngFile(name = "shot.png"): File {
  return new File([PNG], name, { type: "image/png" });
}

function paste(target: Element, files: File[]) {
  fireEvent.paste(target, {
    clipboardData: {
      items: files.map((f) => ({ kind: "file", getAsFile: () => f })),
      files,
    },
  });
}

function msg(id: number, role: "user" | "assistant", content: string, extra = {}): ChatMessage {
  return {
    id,
    role,
    content,
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
    created_at: "2026-01-01T10:00:00Z",
    ...extra,
  };
}

function chat(detail: ConversationDetail, extra: Record<string, unknown> = {}) {
  const mock = mockFetch({
    "GET /api/conversations": [detail],
    [`GET /api/conversations/${detail.id}`]: detail,
    [`GET /api/conversations/${detail.id}/memory`]: [],
    ...extra,
  });
  const api = new ApiClient({ token: "t", fetchImpl: mock.fetch });
  render(<TalkPage api={api} />);
  return mock;
}

const CONV = {
  id: "c1",
  title: "x",
  model: null,
  node: null,
  created_at: "2026-01-01T10:00:00Z",
  updated_at: "2026-01-01T10:00:00Z",
};

beforeEach(() => {
  window.location.hash = "";
  URL.createObjectURL = vi.fn(() => "blob:picture");
  URL.revokeObjectURL = vi.fn();
});

afterEach(() => {
  vi.restoreAllMocks();
});

describe("which files may go with a message", () => {
  it("takes images up to the count and size limits and says why it left one out", () => {
    const big = new File([PNG], "big.png", { type: "image/png" });
    Object.defineProperty(big, "size", { value: MAX_IMAGE_BYTES + 1 });
    const text = new File(["x"], "notes.txt", { type: "text/plain" });
    expect(acceptable([pngFile(), text], 0)).toMatchObject({ problem: "type" });
    expect(acceptable([big], 0)).toEqual({ take: [], problem: "size" });
    const many = Array.from({ length: MAX_IMAGES + 1 }, () => pngFile());
    const { take, problem } = acceptable(many, 0);
    expect(take).toHaveLength(MAX_IMAGES);
    expect(problem).toBe("count");
    expect(acceptable([pngFile()], MAX_IMAGES).take).toHaveLength(0);
  });
});

describe("images in the chat (D125)", () => {
  it("sends a pasted screenshot with the message, even without text", async () => {
    const mock = chat({ ...CONV, messages: [] });
    const box = await screen.findByLabelText("Schreib mir …");
    const send = screen.getByRole("button", { name: "Senden" }) as HTMLButtonElement;
    expect(send.disabled).toBe(true);
    paste(box, [pngFile()]);
    const strip = await screen.findByTestId("image-draft");
    expect(strip.querySelector("img")?.getAttribute("src")).toMatch(PNG_DATA_URL);
    await waitFor(() => expect(send.disabled).toBe(false));
    fireEvent.click(send);
    await waitFor(() =>
      expect(
        mock.calls.some((c) => c.method === "POST" && c.url === "/api/conversations/c1/messages"),
      ).toBe(true),
    );
    const posted = mock.calls.find(
      (c) => c.method === "POST" && c.url === "/api/conversations/c1/messages",
    )!;
    expect(posted.body).toEqual({
      content: "",
      tools: "auto",
      images: [PNG_BASE64],
    });
    expect(screen.queryByTestId("image-draft")).toBeNull(); // the composer is empty again
  });

  it("takes an image out of the message again and refuses a file that is no image", async () => {
    chat({ ...CONV, messages: [] });
    const box = await screen.findByLabelText("Schreib mir …");
    paste(box, [pngFile("a.png")]);
    fireEvent.click(await screen.findByRole("button", { name: "a.png entfernen" }));
    await waitFor(() => expect(screen.queryByTestId("image-draft")).toBeNull());
    fireEvent.change(screen.getByTestId("image-input"), {
      target: { files: [new File(["x"], "notes.txt", { type: "text/plain" })] },
    });
    expect((await screen.findByRole("alert")).textContent).toContain("Nur Bilder");
  });

  it("shows the images of a message as thumbnails and one whole on click", async () => {
    const mock = chat(
      {
        ...CONV,
        messages: [
          msg(QUESTION_ID, "user", "Was ist das?", { images: [{ id: "abc", ...THUMB }] }),
          msg(ANSWER_ID, "assistant", "Ein roter Fleck."),
        ],
      },
      {
        "GET /api/chat/images/abc": () =>
          new Response(PNG, { status: OK, headers: { "Content-Type": "image/jpeg" } }),
      },
    );
    const open = await screen.findByRole("button", { name: "Bild ganz zeigen" });
    await waitFor(() => expect(open.querySelector("img")?.getAttribute("src")).toBe("blob:picture"));
    const asked = mock.calls.find((c) => c.url === "/api/chat/images/abc")!;
    expect(asked.headers.authorization).toBe("Bearer t");
    fireEvent.click(open);
    const viewer = await screen.findByRole("dialog", { name: "Bild" });
    expect(viewer.querySelector("img")?.getAttribute("src")).toBe("blob:picture");
    fireEvent.keyDown(window, { key: "Escape" });
    await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
  });

  it("says under the answer when its model could not see the images", async () => {
    chat({
      ...CONV,
      messages: [
        msg(QUESTION_ID, "user", "Lies das", { images: [{ id: "abc", ...TINY }] }),
        msg(ANSWER_ID, "assistant", "Ich sehe kein Bild.", {
          activity: [
            {
              id: QUESTION_ID,
              kind: "note",
              status: "info",
              title: "small",
              detail: null,
              data: { event: "vision_blind" },
              at: "2026-01-01T10:00:00Z",
              ms: 0,
            },
          ],
        }),
      ],
    });
    expect((await screen.findByTestId("vision-blind")).textContent).toContain(
      "kann keine Bilder sehen",
    );
  });
});

describe("images in incognito", () => {
  it("sends a dropped image along and shows it in the question", async () => {
    window.matchMedia = ((q: string) => ({
      matches: true,
      media: q,
      addEventListener() {},
      removeEventListener() {},
    })) as unknown as typeof window.matchMedia;
    let sent: unknown = null;
    const tab = { id: "t1", title: "", internet: true, count: 0, created_at: 1, updated_at: 1 };
    const mock = mockFetch({
      "GET /api/incognito": () => jsonResponse({ tabs: [tab] }),
      "POST /api/incognito": () => jsonResponse({ ...tab, messages: [] }, CREATED),
      "GET /api/incognito/t1": () => jsonResponse({ ...tab, messages: [] }),
      "POST /api/incognito/t1/messages": (init?: RequestInit) => {
        sent = JSON.parse(String(init?.body));
        return jsonResponse({
          message: {
            role: "assistant",
            content: "Ich sehe nichts.",
            vision: { sees: false, model: "small", images: 0 },
          },
          tab,
        });
      },
    });
    const api = new ApiClient({ token: "t", fetchImpl: mock.fetch });
    render(<IncognitoPage api={api} />);
    fireEvent.click(await screen.findByRole("button", { name: "Neuer Inkognito-Tab" }));
    const box = await screen.findByLabelText("Schreib etwas, das niemand behält …");
    const form = box.closest("form")!;
    const file = pngFile();
    fireEvent.drop(form, {
      dataTransfer: { items: [{ kind: "file", getAsFile: () => file }], files: [file], types: ["Files"] },
    });
    await screen.findByTestId("image-draft");
    fireEvent.change(box, { target: { value: "Was ist das?" } });
    fireEvent.click(screen.getByRole("button", { name: "Senden" }));
    expect(await screen.findByText("Ich sehe nichts.")).toBeTruthy();
    expect(sent).toEqual({ content: "Was ist das?", images: [PNG_BASE64] });
    expect(screen.getByTestId("message-images").querySelector("img")?.getAttribute("src")).toMatch(
      PNG_DATA_URL,
    );
    expect(screen.getByRole("status").textContent).toContain("kann keine Bilder sehen");
  });
});
