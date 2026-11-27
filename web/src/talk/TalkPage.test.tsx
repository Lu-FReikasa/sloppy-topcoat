import {
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
} from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it } from "vitest";
import { ApiClient } from "../api/client";
import { requestCompose, takeCompose } from "../desktop/compose";
import { takeShow } from "../files/bus";
import { mockFetch } from "../test/mockFetch";
import { controlledSse, sseResponse } from "../test/sse";
import { FakeMicrophone, FakePlayer, FakeVoiceLink } from "../voice/fake";
import { VoiceDepsContext } from "../voice/usePushToTalk";
import type {
  ActivityItem,
  ChatMessage,
  Conversation,
  ConversationDetail,
} from "../types";
import { TalkPage, sectionBreaks, visibleMessages } from "./TalkPage";

const CONV: Conversation = {
  id: "c1",
  title: "x",
  model: null,
  node: null,
  created_at: "2026-01-01T10:00:00Z",
  updated_at: "2026-01-01T10:00:00Z",
};

function msg(
  id: number,
  role: "user" | "assistant",
  content: string,
  extra = {},
): ChatMessage {
  return {
    id,
    role,
    content,
    model: role === "assistant" ? "gemma-12b" : null,
    node: role === "assistant" ? "node-amd" : null,
    fallback_from: null,
    status: "complete",
    error: null,
    prompt_tokens: null,
    completion_tokens: 12,
    first_token_ms: 400,
    duration_ms: 3100,
    context_trimmed: 0,
    created_at: "2026-01-01T10:00:00Z",
    ...extra,
  };
}

function setup(
  detail: ConversationDetail,
  list: Conversation[] = [CONV],
  extra: Record<string, unknown> = {},
) {
  const mock = mockFetch({
    "GET /api/conversations": list,
    [`GET /api/conversations/${detail.id}`]: detail,
    [`GET /api/conversations/${detail.id}/memory`]: [],
    ...extra,
  });
  const api = new ApiClient({ token: "t", fetchImpl: mock.fetch });
  render(<TalkPage api={api} />);
  return mock;
}

beforeEach(() => {
  window.location.hash = "";
});

afterEach(() => {
  takeShow();
  takeCompose("chat");
});

describe("TalkPage", () => {
  it("offers the preview and the store click under a theme in an answer", async () => {
    const theme = {
      key: "dusk",
      name: "Dusk",
      layout: { kind: "tile" },
      bar: { position: "left", items: ["programs"] },
      chrome: { buttons: ["close"], titlebar: "top" },
      motion: { style: "none", ms: 0 },
    };
    const mock = setup(
      {
        ...CONV,
        messages: [
          msg(1, "user", "Ein Theme bitte"),
          msg(2, "assistant", "```json\n" + JSON.stringify(theme) + "\n```"),
        ],
      },
      [CONV],
      { "PUT /api/ui/themes/dusk": theme },
    );
    fireEvent.click(await screen.findByRole("button", { name: "Theme anlegen" }));
    await waitFor(() =>
      expect(
        mock.calls.some((c) => c.method === "PUT" && c.url === "/api/ui/themes/dusk"),
      ).toBe(true),
    );
    expect(screen.getByRole("button", { name: "Vorschau" })).toBeTruthy();
  });

  it("shows the history without models, nodes or timings", async () => {
    setup({
      ...CONV,
      messages: [
        msg(1, "user", "Hello?"),
        msg(2, "assistant", "Hello **there**"),
      ],
    });
    expect((await screen.findByText("there")).tagName).toBe("STRONG");
    expect(screen.getByText("Hello?")).toBeTruthy();
    expect(screen.queryByText(/gemma-12b/)).toBeNull();
    expect(screen.queryByText(/Tokens/)).toBeNull();
    expect(screen.queryByText(/3,1 s/)).toBeNull();
    expect(screen.getAllByTestId("day-separator")).toHaveLength(1);
    expect(screen.queryByTestId("evidence-mark")).toBeNull();
  });

  it("shows the secretary's work card under the message that handed the task on", async () => {
    setup(
      {
        ...CONV,
        application: "chat",
        messages: [
          msg(1, "user", "Lass Recherche zwei Backup-Werkzeuge vergleichen"),
          msg(2, "assistant", "Ich habe das an Recherche weitergegeben."),
          msg(3, "user", "Danke", { created_at: "2026-01-01T10:05:00Z" }),
        ],
      },
      [CONV],
      {
        "GET /api/conversations/c1/orders?language=de": [
          {
            id: "o1",
            employee: "research",
            employee_title: "Recherche",
            topic: "Zwei Backup-Werkzeuge vergleichen",
            status: "done",
            report_when_done: false,
            created_at: "2026-01-01T10:00:01Z",
            chat_message_id: 2,
            summary: "Werkzeug A wird gepflegt.",
            withheld: [],
            direct_conversation_id: null,
          },
        ],
      },
    );
    const cardEl = await screen.findByTestId("work-card");
    expect(cardEl.textContent).toContain("Zwei Backup-Werkzeuge vergleichen");
    expect(cardEl.textContent).toContain("Werkzeug A wird gepflegt.");
    // it stands in the row of message 2, before the next message
    const rows = Array.from(document.querySelectorAll(".talk-row"));
    const row = rows.find((r) => r.contains(cardEl));
    expect(row?.textContent).toContain("Ich habe das an Recherche weitergegeben.");
    expect(row?.textContent).not.toContain("Danke");
  });

  it("asks for no work cards in another program's conversation", async () => {
    const mock = setup({
      ...CONV,
      application: "dev",
      messages: [msg(1, "user", "Hallo"), msg(2, "assistant", "Hallo zurück")],
    });
    await screen.findByText("Hallo zurück");
    expect(mock.calls.some((c) => c.url.includes("/orders"))).toBe(false);
  });

  it("puts the window's actions and the origin in the header above the stream (#173)", async () => {
    const mock = mockFetch({
      "GET /api/conversations": [CONV],
      "GET /api/conversations/c1": { ...CONV, messages: [msg(1, "user", "Hello?")] },
      "GET /api/conversations/c1/memory": [],
    });
    const api = new ApiClient({ token: "t", fetchImpl: mock.fetch });
    render(
      <TalkPage
        api={api}
        headerActions={<button type="button">Hand over</button>}
      />,
    );
    await screen.findByText("Hello?");
    const head = screen.getByTestId("talk-head");
    const stream = screen.getByRole("log");
    const action = screen.getByRole("button", { name: "Hand over" });
    expect(head.contains(action)).toBe(true);
    expect(head.contains(screen.getByTestId("chat-origin"))).toBe(true);
    // the header is not part of the stream, so a scrolled message never slides under it
    expect(stream.contains(head)).toBe(false);
    expect(stream.contains(action)).toBe(false);
  });

  it("asks nothing about reviews while no chat is open yet", async () => {
    const mock = mockFetch({
      "GET /api/conversations": [],
      "POST /api/conversations": CONV,
    });
    const api = new ApiClient({ token: "t", fetchImpl: mock.fetch });
    render(<TalkPage api={api} />);
    await screen.findByLabelText("Schreib mir …");
    expect(mock.calls.some((c) => c.url.includes("/conversations//"))).toBe(
      false,
    );
  });

  it("lets a cut-off answer go on", async () => {
    const mock = setup({
      ...CONV,
      messages: [
        msg(1, "user", "Tell me"),
        msg(2, "assistant", "Once upon", { status: "truncated" }),
      ],
    });
    fireEvent.click(await screen.findByRole("button", { name: "Fortsetzen" }));
    await waitFor(() =>
      expect(
        mock.calls.some(
          (c) =>
            c.method === "POST" &&
            c.url.startsWith("/api/conversations/c1/continue"),
        ),
      ).toBe(true),
    );
  });

  it("asks for the last answer again", async () => {
    const mock = setup({
      ...CONV,
      messages: [
        msg(1, "user", "Tell me"),
        msg(2, "assistant", "Once upon a time"),
      ],
    });
    fireEvent.click(
      await screen.findByRole("button", { name: "Neu antworten" }),
    );
    await waitFor(() =>
      expect(
        mock.calls.some(
          (c) =>
            c.method === "POST" &&
            c.url.startsWith("/api/conversations/c1/regenerate"),
        ),
      ).toBe(true),
    );
  });

  it("offers no 'continue' for a complete answer and nothing after the person's own message", async () => {
    setup({
      ...CONV,
      messages: [msg(1, "user", "Hi"), msg(2, "assistant", "Hello")],
    });
    expect(
      await screen.findByRole("button", { name: "Neu antworten" }),
    ).toBeTruthy();
    expect(screen.queryByRole("button", { name: "Fortsetzen" })).toBeNull();
    cleanup();
    setup({ ...CONV, messages: [msg(1, "user", "Hi")] });
    await screen.findByText("Hi");
    expect(screen.queryByRole("button", { name: "Neu antworten" })).toBeNull();
  });

  it("opens the newest conversation and creates one when there is none", async () => {
    const created = { ...CONV, id: "c9" };
    const mock = mockFetch({
      "GET /api/conversations": [],
      "POST /api/conversations": created,
    });
    const api = new ApiClient({ token: "t", fetchImpl: mock.fetch });
    render(<TalkPage api={api} />);
    await waitFor(() =>
      expect(
        mock.calls.some(
          (c) => c.method === "POST" && c.url === "/api/conversations",
        ),
      ).toBe(true),
    );
    expect(await screen.findByLabelText("Schreib mir …")).toBeTruthy();
  });

  it("shows the traffic light of the second look and the sources behind the answer on click", async () => {
    setup(
      {
        ...CONV,
        messages: [
          msg(1, "user", "Is the laptop on?"),
          msg(2, "assistant", "Yes, it stays on.", {
            memory_refs: [
              {
                id: "n1",
                text: "Laptop stays on",
                evidence: "doc",
                status: "active",
              },
            ],
          }),
        ],
      },
      [CONV],
      {
        "GET /api/conversations/c1/review-lights": {
          lights: { "2": "ok" },
          waiting: [],
        },
      },
    );
    const mark = await screen.findByTestId("evidence-mark");
    expect(mark.textContent).toBe("geprüft");
    expect(mark.getAttribute("data-light")).toBe("ok");
    expect(screen.queryByText("Laptop stays on")).toBeNull();
    fireEvent.click(mark);
    expect(screen.getByText("Laptop stays on")).toBeTruthy();
    expect(screen.getByText(/aus deinen Notizen/)).toBeTruthy();
  });

  it.each([
    ["waiting", { lights: {}, waiting: [2] }, "wartet auf die Zweitprüfung"],
    ["hint", { lights: { "2": "hint" }, waiting: [] }, "kleine Mängel"],
    ["error", { lights: { "2": "error" }, waiting: [] }, "Fehler gefunden"],
  ])("shows the %s light under an answer", async (light, body, label) => {
    setup(
      {
        ...CONV,
        messages: [msg(1, "user", "Hi"), msg(2, "assistant", "Hello")],
      },
      [CONV],
      { "GET /api/conversations/c1/review-lights": body },
    );
    const mark = await screen.findByTestId("evidence-mark");
    expect(mark.textContent).toBe(label);
    expect(mark.getAttribute("data-light")).toBe(light);
  });

  it("says openly which node answered when it was not the expected one", async () => {
    setup({
      ...CONV,
      messages: [
        msg(1, "user", "Hi"),
        msg(2, "assistant", "Hello", {
          node: "node-laptop",
          fallback_from: "node-amd",
        }),
      ],
    });
    const mark = await screen.findByTestId("evidence-mark");
    expect(mark.textContent).toBe("über node-laptop");
    fireEvent.click(mark);
    expect(screen.getByText(/node-amd nicht verfügbar/)).toBeTruthy();
  });

  it("answers a failed run with one calm sentence, not the error", async () => {
    setup({
      ...CONV,
      messages: [
        msg(1, "user", "Hi"),
        msg(2, "assistant", "", {
          status: "error",
          error: "upstream 502 node-amd",
        }),
      ],
    });
    expect(await screen.findByText(/nicht geklappt/)).toBeTruthy();
    expect(screen.queryByText(/502/)).toBeNull();
  });
});

describe("report cards", () => {
  it("shows the card folded next to the closing sentences", async () => {
    const card = {
      version: 1,
      task_id: "t1",
      status: "complete",
      incomplete: false,
      incomplete_reason: null,
      summary: "Alles in Ordnung.",
      findings: [],
      assumptions: [],
      proposal: null,
      evidence: [],
      notes: [],
    };
    setup({
      ...CONV,
      messages: [
        msg(1, "user", "Prüf bitte Plex"),
        msg(2, "assistant", "card", { kind: "report", report: card }),
        msg(
          3,
          "assistant",
          "Plex läuft, ich habe nichts Auffälliges gefunden.",
        ),
      ],
    });
    expect(await screen.findByText("Ergebnis der Prüfung")).toBeTruthy();
    expect(screen.getByText(/nichts Auffälliges/)).toBeTruthy();
  });

  it("is one answer: no looking-now bubble, the card follows the answer", async () => {
    const card = {
      version: 1,
      task_id: "t1",
      status: "complete",
      incomplete: false,
      incomplete_reason: null,
      summary: "Alles in Ordnung.",
      findings: [],
      assumptions: [],
      proposal: null,
      evidence: [],
      notes: [],
    };
    setup({
      ...CONV,
      messages: [
        msg(1, "user", "Prüf bitte Plex"),
        msg(2, "assistant", "Schau ich mir an."),
        msg(3, "assistant", "card", { kind: "report", report: card }),
        msg(4, "assistant", "Plex läuft, nichts Auffälliges.", {
          agent_task_id: "t1",
        }),
      ],
    });
    const answer = await screen.findByText(/nichts Auffälliges/);
    expect(screen.queryByText("Schau ich mir an.")).toBeNull();
    const report = screen.getByText("Ergebnis der Prüfung");
    expect(
      answer.compareDocumentPosition(report) & Node.DOCUMENT_POSITION_FOLLOWING,
    ).toBeTruthy();
  });
});

describe("visibleMessages", () => {
  it("keeps what was said and drops progress lines, cards and empty answers", () => {
    const messages = [
      msg(1, "user", "a"),
      msg(2, "assistant", "tool runs", { kind: "progress" }),
      msg(3, "assistant", "card", { kind: "report" }), // no card data: nothing to show
      msg(4, "assistant", "", {}),
      msg(5, "assistant", "b", { kind: "say" }),
    ];
    expect(visibleMessages(messages).map((m) => m.id)).toEqual([1, 5]);
  });

  describe("background work", () => {
    const MESSAGES = [msg(1, "user", "Hallo?"), msg(2, "assistant", "Hallo.")];

    it("says that the summary is still being brought up to date, and nothing otherwise", async () => {
      setup({ ...CONV, messages: MESSAGES }, [CONV], {
        "GET /api/conversations/c1/background": { state: "waiting" },
      });
      const note = await screen.findByTestId("background-work");
      expect(note.textContent).toContain("weiterschreiben");
    });

    it("shows no note when the background is idle or the hub cannot say", async () => {
      setup({ ...CONV, messages: MESSAGES }, [CONV], {
        "GET /api/conversations/c1/background": { state: "idle" },
      });
      await screen.findByText("Hallo.");
      await waitFor(() =>
        expect(screen.queryByTestId("background-work")).toBeNull(),
      );
    });
  });

  describe("sections", () => {
    const seg = (
      first: number,
      last: number,
      from: string,
      to: string,
      summary: string | null,
    ) => ({
      first_message_id: first,
      last_message_id: last,
      started_at: from,
      ended_at: to,
      messages: last - first + 1,
      current: false,
      summary,
    });
    const SEGMENTS = {
      enabled: true,
      idle_seconds: 1200,
      segments: [
        seg(
          1,
          2,
          "2026-01-01T10:00:00Z",
          "2026-01-01T10:01:00Z",
          "Plex läuft auf node-a.",
        ),
        {
          ...seg(3, 4, "2026-01-01T12:00:00Z", "2026-01-01T12:01:00Z", null),
          current: true,
        },
      ],
    };
    const MESSAGES = [
      msg(1, "user", "Wo läuft Plex?"),
      msg(2, "assistant", "Auf node-a."),
      msg(3, "user", "Und das Backup?"),
      msg(4, "assistant", "Nachts."),
    ];

    it("marks where a new section begins and offers the summary of the one before", async () => {
      setup({ ...CONV, messages: MESSAGES }, [CONV], {
        "GET /api/conversations/c1/segments": SEGMENTS,
      });
      const cut = await screen.findByTestId("section-break");
      expect(cut.textContent).toContain("Neuer Abschnitt");
      expect(cut.textContent).toContain("nach 119 Min. Pause");
      expect(cut.textContent).toContain("Plex läuft auf node-a.");
      // it sits right before the first message of the new section
      expect(cut.parentElement?.textContent).toContain("Und das Backup?");
    });

    it("shows no marks when the hub does not split conversations", async () => {
      setup({ ...CONV, messages: MESSAGES }, [CONV], {
        "GET /api/conversations/c1/segments": { ...SEGMENTS, enabled: false },
      });
      await screen.findByText("Und das Backup?");
      await waitFor(() =>
        expect(screen.queryByTestId("section-break")).toBeNull(),
      );
      expect(
        screen.queryByRole("button", { name: "Neuer Abschnitt" }),
      ).toBeNull();
    });

    it("marks a new section as pending and sends the next message into a new chat", async () => {
      const next = { ...CONV, id: "c2", continues_id: "c1" };
      const mock = setup({ ...CONV, messages: MESSAGES }, [CONV], {
        "GET /api/conversations/c1/segments": SEGMENTS,
        "POST /api/conversations/c1/section": {
          created: true,
          conversation: next,
        },
        "POST /api/conversations/c2/messages": new Response("", {
          status: 500,
        }),
      });
      fireEvent.click(
        await screen.findByRole("button", { name: "Neuer Abschnitt" }),
      );
      expect(await screen.findByTestId("section-pending")).toBeTruthy();
      // the window is empty: the old section is not shown under the new one
      expect(screen.queryByText("Und das Backup?")).toBeNull();
      // nothing is created until a message is written
      expect(mock.calls.some((c) => c.url.endsWith("/section"))).toBe(false);
      fireEvent.change(screen.getByRole("textbox"), {
        target: { value: "Etwas Neues" },
      });
      fireEvent.click(screen.getByRole("button", { name: "Senden" }));
      await waitFor(() =>
        expect(
          mock.calls.some(
            (c) =>
              c.url === "/api/conversations/c1/section" &&
              JSON.stringify(c.body) === JSON.stringify({ force: true }),
          ),
        ).toBe(true),
      );
      await waitFor(() =>
        expect(
          mock.calls.some((c) => c.url === "/api/conversations/c2/messages"),
        ).toBe(true),
      );
    });

    it("shows the chats a chat continues above it, with the summary on the line between", async () => {
      const earlier = {
        ...CONV,
        id: "c0",
        messages: [
          msg(10, "user", "Frage von gestern"),
          msg(11, "assistant", "Antwort von gestern"),
        ],
      };
      setup(
        {
          ...CONV,
          messages: [
            msg(20, "user", "Frage von heute", {
              created_at: "2026-01-01T12:00:00Z",
            }),
          ],
        },
        [CONV, { ...CONV, id: "c0" }],
        {
          "GET /api/conversations/c0": earlier,
          "GET /api/conversations/c0/memory": [],
          "GET /api/conversations/c1/thread": {
            enabled: true,
            idle_seconds: 1200,
            more: false,
            sections: [
              {
                id: "c0",
                title: "",
                messages: 2,
                started_at: "2026-01-01T10:00:00Z",
                ended_at: "2026-01-01T10:05:00Z",
                summary: "Es ging um gestern.",
                current: false,
              },
              {
                id: "c1",
                title: "",
                messages: 1,
                started_at: "2026-01-01T12:00:00Z",
                ended_at: "2026-01-01T12:00:00Z",
                summary: null,
                current: true,
              },
            ],
          },
        },
      );
      // the window starts with the open chat only
      await screen.findByText("Frage von heute");
      expect(screen.queryByText("Antwort von gestern")).toBeNull();
      fireEvent.click(
        await screen.findByRole("button", {
          name: "Frühere Abschnitte anzeigen",
        }),
      );
      const cut = await screen.findByTestId("section-break");
      expect(cut.textContent).toContain("Es ging um gestern.");
      expect(cut.textContent).toContain("nach 115 Min. Pause");
      expect(screen.getByText("Antwort von gestern")).toBeTruthy();
      expect(cut.parentElement?.textContent).toContain("Frage von heute");
    });

    it("cuts at a user request without naming a pause", () => {
      const msgs = [
        msg(1, "user", "a"),
        msg(2, "assistant", "b"),
        msg(3, "user", "c"),
      ];
      const segments = [
        seg(1, 2, "2026-01-01T10:00:00Z", "2026-01-01T10:00:30Z", null),
        seg(3, 3, "2026-01-01T10:02:00Z", "2026-01-01T10:02:00Z", "S"),
      ];
      expect(sectionBreaks(msgs, segments, 1200)).toEqual([
        { index: 2, summary: null, pauseMinutes: 0 },
      ]);
      // one section only: nothing to mark; a section that starts at the top: nothing either
      expect(sectionBreaks(msgs, segments.slice(0, 1), 1200)).toEqual([]);
      expect(sectionBreaks([msg(5, "user", "x")], segments, 1200)).toEqual([]);
    });
  });
});

function item(id: number, o: Partial<ActivityItem> = {}): ActivityItem {
  return {
    id,
    kind: "tool",
    status: "ok",
    title: "docker.ps @ node-a",
    detail: null,
    data: {},
    at: "2026-01-01T10:00:01Z",
    ms: 120,
    ...o,
  };
}

const META = {
  conversation_id: "c1",
  user_message: null,
  continues: null,
  memory: [],
  message_id: 2,
  model: "m",
  node: "node-a",
  fallback_from: null,
  fallback_state: null,
  context: { length: 0, estimated_prompt_tokens: 0, trimmed: 0 },
  checking: "forced",
  internet: true,
  quality: "balanced",
};

describe("the chain of what the hub does (E7-01)", () => {
  it("is one folded line above a finished answer", async () => {
    setup({
      ...CONV,
      messages: [
        msg(1, "user", "Wie geht es dem Server?"),
        msg(2, "assistant", "Alles ruhig.", {
          activity: [item(1), item(2, { title: "logs.search" })],
        }),
      ],
    });
    const chain = await screen.findByTestId("activity");
    expect(chain.hasAttribute("open")).toBe(false);
    expect(chain.textContent).toContain("2");
    expect(screen.getByText("Alles ruhig.")).toBeTruthy();
  });

  it("stays compact when a call failed: the summary line names the failure", async () => {
    setup({
      ...CONV,
      messages: [
        msg(1, "user", "Prüf Plex"),
        msg(2, "assistant", "Teils erreicht.", {
          activity: [
            item(1),
            item(2, { status: "failed", detail: "unreachable" }),
          ],
        }),
      ],
    });
    const chain = await screen.findByTestId("activity");
    expect(chain.hasAttribute("open")).toBe(false);
    expect(chain.querySelector("summary")?.textContent).toContain("fehlgeschlagen: 1");
    expect(screen.getByRole("alert").textContent).toContain("unreachable");
  });

  it("shows no chain for an answer that has none", async () => {
    setup({
      ...CONV,
      messages: [msg(1, "user", "Hi"), msg(2, "assistant", "Hallo")],
    });
    await screen.findByText("Hallo");
    expect(screen.queryByTestId("activity")).toBeNull();
  });

  it("grows live, with the pause and note controls while a check runs", async () => {
    const live = controlledSse();
    setup(
      {
        ...CONV,
        messages: [
          msg(1, "user", "Prüf Plex"),
          msg(2, "assistant", "", { status: "running" }),
        ],
      },
      [CONV],
      { "GET /api/chat/messages/2/stream?offset=0": () => live.response },
    );
    await screen.findByLabelText("Schreib mir …");
    live.push("meta", META, 0);
    live.push(
      "activity",
      { op: "add", item: item(1, { status: "running" }) },
      1,
    );
    const chain = await screen.findByTestId("talk-chain");
    expect(
      chain.querySelector("[data-testid=activity]")?.hasAttribute("open"),
    ).toBe(true);
    expect(await screen.findByTestId("check-controls")).toBeTruthy();
    // the plain done-steps list is only the fallback for a hub that sends no chain
    expect(screen.queryByTestId("done-steps")).toBeNull();
    live.push("activity", { op: "update", item: item(1, { status: "ok" }) }, 2);
    live.push(
      "activity",
      { op: "add", item: item(2, { status: "failed", detail: "timeout" }) },
      3,
    );
    await waitFor(() =>
      expect(screen.getByRole("alert").textContent).toContain("zu lange"),
    );
    live.push(
      "done",
      {
        message: msg(2, "assistant", "Fertig.", {
          activity: [item(1), item(2)],
        }),
      },
      4,
    );
    live.close();
    expect(await screen.findByText("Fertig.")).toBeTruthy();
    await waitFor(() => expect(screen.queryByTestId("talk-chain")).toBeNull());
  });

  it("offers to do what the answer only announced", async () => {
    setup({
      ...CONV,
      messages: [
        msg(1, "user", "Prüf Plex"),
        msg(2, "assistant", "Ich schaue mir das jetzt an.", {
          activity: [
            item(1, {
              kind: "announce",
              status: "failed",
              title: "",
              detail: "Ich schaue",
            }),
          ],
        }),
      ],
    });
    expect(await screen.findByTestId("announce-note")).toBeTruthy();
  });
});

describe("pointing at code (E8-01)", () => {
  const SHOW = item(1, {
    title: "repo.show",
    data: { input: { path: "hub/src/topcoat_hub/trace_tool.py", from: 3 } },
  });

  it("opens the reader once for a live repo.show item, not for an old answer", async () => {
    const live = controlledSse();
    setup(
      {
        ...CONV,
        messages: [
          msg(1, "user", "Zeig mir das"),
          msg(2, "assistant", "Früher.", { activity: [SHOW] }),
          msg(3, "user", "Nochmal"),
          msg(4, "assistant", "", { status: "running" }),
        ],
      },
      [CONV],
      { "GET /api/chat/messages/4/stream?offset=0": () => live.response },
    );
    await screen.findByLabelText("Schreib mir …");
    // the finished answer with the same item pointed at nothing
    expect(takeShow()).toBeNull();
    live.push("meta", { ...META, message_id: 4 }, 0);
    live.push("activity", { op: "add", item: SHOW }, 1);
    await waitFor(() =>
      expect(takeShow()?.path).toBe("hub/src/topcoat_hub/trace_tool.py"),
    );
    // the same item arriving again (an update) points once only
    live.push("activity", { op: "update", item: SHOW }, 2);
    live.close();
    expect(takeShow()).toBeNull();
  });

  it("puts a text another window handed over into the draft", async () => {
    setup({ ...CONV, messages: [msg(1, "user", "Hallo")] });
    const input = (await screen.findByLabelText(
      "Schreib mir …",
    )) as HTMLTextAreaElement;
    requestCompose("chat", "Wie wurde das beantwortet?");
    await waitFor(() => expect(input.value).toBe("Wie wurde das beantwortet?"));
  });

  it("shows the number of a message and the two buttons under it", async () => {
    setup({
      ...CONV,
      messages: [msg(1, "user", "Hallo"), msg(2, "assistant", "Hi")],
    });
    expect(await screen.findByText("#2")).toBeTruthy();
    expect(
      screen.getAllByRole("button", { name: "In Entwicklung untersuchen" }),
    ).toHaveLength(2);
  });

  it("puts 'Have it checked' in the answer's footer as a small button, not as a bar of its own (#77)", async () => {
    const mock = setup(
      { ...CONV, messages: [msg(1, "user", "Hallo"), msg(2, "assistant", "Hi")] },
      [CONV],
      { "POST /api/messages/2/review": { started: true } },
    );
    const ask = await screen.findByRole("button", { name: "Prüfen lassen" });
    // one per answer, in the line with the answer's time and number, none under the question
    expect(screen.getAllByRole("button", { name: "Prüfen lassen" })).toHaveLength(1);
    const footer = ask.closest(".message-line");
    expect(footer?.textContent).toContain("#2");
    fireEvent.click(ask);
    await waitFor(() =>
      expect(mock.calls.some((c) => c.method === "POST" && c.url === "/api/messages/2/review")).toBe(true),
    );
    expect((ask as HTMLButtonElement).disabled).toBe(true);
  });
});

describe("the choices that stay in the calm view", () => {
  it("changes how much trouble the answer takes and keeps export and the internet at hand", async () => {
    const mock = setup(
      {
        ...CONV,
        messages: [msg(1, "user", "Hi"), msg(2, "assistant", "Hello")],
      },
      [CONV],
      {
        "GET /api/chat/settings": {
          user: { quality: "balanced", internet: true },
          hub: {},
          effective_model: "gemma-12b",
        },
        "PATCH /api/conversations/c1": { ...CONV, quality: "thorough" },
      },
    );
    const options = await screen.findByTestId("talk-options");
    const select = options.querySelector("select") as HTMLSelectElement;
    expect(select.value).toBe("balanced");
    expect(options.textContent).toContain("Export");
    fireEvent.change(select, { target: { value: "thorough" } });
    await waitFor(() =>
      expect(
        mock.calls.some(
          (c) =>
            c.method === "PATCH" &&
            c.url === "/api/conversations/c1" &&
            (c.body as { quality?: string }).quality === "thorough",
        ),
      ).toBe(true),
    );
  });
});

describe("push to talk (D127)", () => {
  const link = new FakeVoiceLink();
  const voiceDeps = () => ({ link, mic: new FakeMicrophone(), player: new FakePlayer() });

  function talkSetup(voice: { transcribe: boolean; speak: boolean }, answer = "Es ist zehn Uhr. Schönen Abend!") {
    const mock = mockFetch({
      "GET /api/conversations": [CONV],
      "GET /api/conversations/c1": {
        ...CONV,
        messages: [msg(1, "user", "Hi"), msg(2, "assistant", "Hallo")],
      },
      "GET /api/conversations/c1/memory": [],
      "GET /api/voice": voice,
      "POST /api/conversations/c1/section": { created: false, conversation: CONV },
      "POST /api/conversations/c1/messages": () =>
        sseResponse([
          ["meta", { ...META, message_id: 4 }],
          ["delta", { text: answer }],
          ["done", { message: msg(4, "assistant", answer) }],
        ]),
    });
    const api = new ApiClient({ token: "t", fetchImpl: mock.fetch });
    render(
      <VoiceDepsContext.Provider value={voiceDeps}>
        <TalkPage api={api} />
      </VoiceDepsContext.Provider>,
    );
    return mock;
  }

  afterEach(() => {
    link.transcripts = [];
    link.spoken = [];
  });

  it("is not offered when no node can listen", async () => {
    talkSetup({ transcribe: false, speak: false });
    await screen.findByText("Hallo");
    expect(screen.queryByRole("button", { name: "Gedrückt halten zum Sprechen" })).toBeNull();
  });

  it("holding the key sends what was said as the message and reads the answer out", async () => {
    const mock = talkSetup({ transcribe: true, speak: true });
    link.transcripts.push("Wie spät ist es?");
    const key = await screen.findByRole("button", { name: "Gedrückt halten zum Sprechen" });
    fireEvent.pointerDown(key, { button: 0, pointerId: 1 });
    await waitFor(() => expect(key.getAttribute("aria-pressed")).toBe("true"));
    fireEvent.pointerUp(key, { pointerId: 1 });
    await waitFor(() =>
      expect(
        mock.calls.find((c) => c.method === "POST" && c.url === "/api/conversations/c1/messages")
          ?.body,
      ).toMatchObject({ content: "Wie spät ist es?" }),
    );
    expect(await screen.findByText("Es ist zehn Uhr. Schönen Abend!")).toBeTruthy();
    await waitFor(() =>
      expect(link.spoken.map((s) => s.text)).toEqual(["Es ist zehn Uhr.", "Schönen Abend!"]),
    );
    expect(link.spoken[0]!.opts.conversationId).toBe("c1");
  });

  it("Ctrl+Space works as the key too, even while typing", async () => {
    const mock = talkSetup({ transcribe: true, speak: false });
    link.transcripts.push("Hallo Topcoat");
    const box = await screen.findByLabelText("Schreib mir …");
    await screen.findByRole("button", { name: "Gedrückt halten zum Sprechen" });
    fireEvent.keyDown(box, { code: "Space", key: " ", ctrlKey: true });
    await screen.findByText("Ich höre zu …");
    fireEvent.keyUp(box, { code: "Space", key: " ", ctrlKey: true });
    await waitFor(() =>
      expect(mock.calls.some((c) => c.url === "/api/conversations/c1/messages")).toBe(true),
    );
  });

  it("what was said while an answer was written never goes into another chat", async () => {
    const live = controlledSse();
    const other: Conversation = { ...CONV, id: "c2", title: "Anderes" };
    const mock = mockFetch({
      "GET /api/conversations": [CONV, other],
      "GET /api/conversations/c1": {
        ...CONV,
        messages: [msg(1, "user", "Prüf Plex"), msg(2, "assistant", "", { status: "running" })],
      },
      "GET /api/conversations/c2": { ...other, messages: [msg(5, "assistant", "Anderer Chat")] },
      "GET /api/conversations/c1/memory": [],
      "GET /api/conversations/c2/memory": [],
      "GET /api/chat/messages/2/stream?offset=0": () => live.response,
      "GET /api/voice": { transcribe: true, speak: true },
      "POST /api/conversations/c2/section": { created: false, conversation: other },
    });
    const api = new ApiClient({ token: "t", fetchImpl: mock.fetch });
    const page = (id: string) => (
      <VoiceDepsContext.Provider value={voiceDeps}>
        <TalkPage api={api} conversationId={id} />
      </VoiceDepsContext.Provider>
    );
    const { rerender } = render(page("c1"));
    live.push("meta", META, 0);
    link.transcripts.push("Und wie spät ist es?");
    const key = await screen.findByRole("button", { name: "Gedrückt halten zum Sprechen" });
    fireEvent.pointerDown(key, { button: 0, pointerId: 1 });
    await waitFor(() => expect(key.getAttribute("aria-pressed")).toBe("true"));
    fireEvent.pointerUp(key, { pointerId: 1 });
    await screen.findByText("Ich denke nach …"); // it waits for the answer being written
    rerender(page("c2"));
    await screen.findByText("Anderer Chat");
    await waitFor(() => expect(screen.queryByText("Ich denke nach …")).toBeNull());
    expect(mock.calls.filter((c) => c.method === "POST").map((c) => c.url)).toEqual([]);
    expect(link.spoken).toEqual([]);
  });

  it("a spoken yes is sent as text and confirms nothing (D15, D83)", async () => {
    const mock = talkSetup({ transcribe: true, speak: true }, "Gut.");
    link.transcripts.push("Ja, mach das");
    const key = await screen.findByRole("button", { name: "Gedrückt halten zum Sprechen" });
    fireEvent.pointerDown(key, { button: 0, pointerId: 1 });
    await waitFor(() => expect(key.getAttribute("aria-pressed")).toBe("true"));
    fireEvent.pointerUp(key, { pointerId: 1 });
    await waitFor(() =>
      expect(mock.calls.some((c) => c.url === "/api/conversations/c1/messages")).toBe(true),
    );
    const posts = mock.calls.filter((c) => c.method !== "GET").map((c) => c.url);
    expect(posts).toEqual(["/api/conversations/c1/section", "/api/conversations/c1/messages"]);
  });
});
