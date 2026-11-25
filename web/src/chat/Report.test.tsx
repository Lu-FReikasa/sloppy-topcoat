import {
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
  within,
} from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { I18nextProvider } from "react-i18next";
import { ApiClient } from "../api/client";
import i18n from "../i18n";
import { mockFetch } from "../test/mockFetch";
import { sseResponse } from "../test/sse";
import type { ChatMessage, Conversation, ReportCard } from "../types";
import { TalkPage } from "../talk/TalkPage";
import { MessageView } from "./MessageView";
import { reportToText } from "./ReportCard";
import { chatReducer, initialChatState, type ChatState } from "./state";

const CARD: ReportCard = {
  version: 1,
  task_id: "11111111-1111-4111-8111-111111111111",
  status: "done",
  incomplete: false,
  incomplete_reason: null,
  summary: "Der Container web wurde wegen Speicher beendet.",
  findings: [
    {
      claim: "web wurde vom OOM-Killer beendet",
      tag: "live",
      evidence: ["E1"],
    },
    {
      claim: "Die Notiz zu Port 80 ist veraltet",
      tag: "live",
      evidence: ["E2"],
      contradicts: ["E3"],
    },
  ],
  assumptions: [
    {
      claim: "Das Limit ist vermutlich zu niedrig",
      confirm_with: "docker.inspect",
      evidence: [],
    },
  ],
  proposal: {
    text: "Speicherlimit erhöhen",
    impact: "web startet einmal neu",
    rollback: "",
    advice_only: true,
  },
  evidence: [
    {
      ref: "E1",
      tag: "live",
      node: "vm-docker",
      capability: "docker.logs",
      input: { container: "web" },
      excerpt: "fatal: out of memory",
      truncated: false,
    },
    {
      ref: "E2",
      tag: "live",
      node: "vm-docker",
      capability: "docker.ps",
      input: {},
      excerpt: "web  0.0.0.0:8080",
      truncated: true,
    },
  ],
  notes: [],
  calls: 2,
};

function msg(
  id: number,
  role: "user" | "assistant",
  content: string,
  extra: Partial<ChatMessage> = {},
): ChatMessage {
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
    created_at: "2026-10-02T10:00:00Z",
    kind: "say",
    ...extra,
  };
}

const REPORT_MSG = msg(7, "assistant", "**Summary:** …", {
  kind: "report",
  report: CARD,
  agent_task_id: CARD.task_id,
});

beforeEach(() => {
  void i18n.changeLanguage("de");
});
afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
});

function card(onApi?: ApiClient) {
  return render(
    <I18nextProvider i18n={i18n}>
      <MessageView message={REPORT_MSG} busy={false} api={onApi} />
    </I18nextProvider>,
  );
}

describe("the report card", () => {
  it("shows the summary, the findings with their marks, the assumptions apart and the proposal", () => {
    card();
    const view = screen.getByTestId("report-card");
    expect(
      within(view).getByText("Der Container web wurde wegen Speicher beendet."),
    ).toBeTruthy();
    expect(
      within(view).getByText(/web wurde vom OOM-Killer beendet/),
    ).toBeTruthy();
    // an assumption is not among the findings, it has its own part with its own hint
    const assumptions = within(view).getByTestId("report-assumptions");
    expect(assumptions.textContent).toContain(
      "Das Limit ist vermutlich zu niedrig",
    );
    expect(assumptions.textContent).toContain("nicht gesehen, nicht bestätigt");
    expect(assumptions.textContent).toContain("docker.inspect");
    expect(view.querySelector(".report-findings")?.textContent).not.toContain(
      "vermutlich",
    );
    // a contradiction with a note is said
    expect(view.textContent).toContain("widerspricht den Notizen E3");
    // the proposal: impact as given, a way back the check did not state is left out, advice only
    const proposal = within(view).getByTestId("report-proposal");
    expect(proposal.textContent).toContain(
      "nur ein Rat — nichts wurde geändert",
    );
    expect(proposal.textContent).toContain("web startet einmal neu");
    expect(proposal.textContent).not.toContain("Rückweg");
  });

  it("says which lookups gave no result, and folds the evidence list away", () => {
    const failing = msg(8, "assistant", "x", {
      kind: "report",
      report: {
        ...CARD,
        failures: [
          {
            capability: "web.fetch",
            node: "tools-a",
            target: "https://example.org/produkt",
            code: "http_404",
            message: "page not found (404)",
          },
        ],
      },
      agent_task_id: CARD.task_id,
    });
    render(
      <I18nextProvider i18n={i18n}>
        <MessageView message={failing} busy={false} />
      </I18nextProvider>,
    );
    const note = screen.getByTestId("report-failure");
    expect(note.textContent).toContain("Nicht abrufbar");
    expect(note.textContent).toContain(
      "https://example.org/produkt: page not found (404)",
    );
    const all = document.querySelector(
      "details.report-evidence-all",
    ) as HTMLDetailsElement;
    expect(all.open).toBe(false);
  });

  it("shows the table of a check order that asked for one, and copies it", () => {
    const rows = [
      { Aussage: "Audiosrv läuft", Beleg: "E2" },
      { Aussage: "Ereignis 225 | audiodg", Beleg: "E3" },
    ];
    const withTable = msg(9, "assistant", "x", {
      kind: "report",
      report: { ...CARD, table: rows },
      agent_task_id: CARD.task_id,
    });
    render(
      <I18nextProvider i18n={i18n}>
        <MessageView message={withTable} busy={false} />
      </I18nextProvider>,
    );
    const table = screen.getByTestId("report-table");
    expect(table.textContent).toContain("Ergebnistabelle");
    const headers = within(table).getAllByRole("columnheader").map((h) => h.textContent);
    expect(headers).toEqual(["Aussage", "Beleg"]);
    expect(within(table).getAllByRole("row")).toHaveLength(3);
    expect(within(table).getByText("Ereignis 225 | audiodg")).toBeTruthy();
    const text = reportToText(i18n.t.bind(i18n), { ...CARD, table: rows });
    expect(text).toContain("Ergebnistabelle:\nAussage | Beleg\nAudiosrv läuft | E2");
    // a card without a table shows none
    cleanup();
    card();
    expect(screen.queryByTestId("report-table")).toBeNull();
  });

  it("shows the quote a finding was checked against, and a quote its evidence does not hold", () => {
    const quoted = msg(10, "assistant", "x", {
      kind: "report",
      report: {
        ...CARD,
        findings: [
          { claim: "Ereignis 225 gefunden", tag: "live", evidence: ["E1"], quote: "Ereignis 225 audiodg" },
          { claim: "Ohne Zitat", tag: "live", evidence: ["E1"], unquoted: true },
        ],
        assumptions: [
          { claim: "Namen stehen in hub.env", confirm_with: "", evidence: [], downgraded: true, misquoted: "names are set in hub.env" },
        ],
      },
      agent_task_id: CARD.task_id,
    });
    render(
      <I18nextProvider i18n={i18n}>
        <MessageView message={quoted} busy={false} />
      </I18nextProvider>,
    );
    expect(screen.getByTestId("report-quote").textContent).toBe("Ereignis 225 audiodg");
    expect(screen.getByText("(ohne Zitat)", { exact: false })).toBeTruthy();
    expect(screen.getByTestId("report-misquoted").textContent).toContain(
      "das Zitat „names are set in hub.env“ steht nicht im Beleg",
    );
  });

  it("opens the excerpt an evidence mark names", () => {
    card();
    const first = screen.getByText("fatal: out of memory", { exact: false });
    expect(first.closest("details")?.open).toBe(false);
    fireEvent.click(screen.getByRole("button", { name: "Beleg E1 zeigen" }));
    expect(first.closest("details")?.open).toBe(true);
    expect(screen.getByText("(gekürzt)", { exact: false })).toBeTruthy(); // E2 is shortened
  });

  it("survives the toggle event a browser fires when the mark opens the excerpt", () => {
    card();
    const first = screen.getByText("fatal: out of memory", { exact: false });
    const details = first.closest("details") as HTMLDetailsElement;
    fireEvent.click(screen.getByRole("button", { name: "Beleg E1 zeigen" }));
    // jsdom fires no toggle itself; a browser does, and the handler used to read a null target
    fireEvent(details, new Event("toggle"));
    expect(details.open).toBe(true);
    expect(screen.getByTestId("report-card")).toBeTruthy();
  });

  it("collapses to the summary and expands again", () => {
    card();
    expect(screen.getByTestId("report-body")).toBeTruthy();
    fireEvent.click(screen.getByTestId("report-toggle"));
    expect(screen.queryByTestId("report-body")).toBeNull();
    expect(
      screen.getByText("Der Container web wurde wegen Speicher beendet."),
    ).toBeTruthy();
    fireEvent.click(screen.getByTestId("report-toggle"));
    expect(screen.getByTestId("report-body")).toBeTruthy();
  });

  it("copies the report as text in the user's language", async () => {
    const write = vi.fn().mockResolvedValue(undefined);
    Object.defineProperty(navigator, "clipboard", {
      value: { writeText: write },
      configurable: true,
    });
    card();
    fireEvent.click(screen.getByTestId("report-copy"));
    await waitFor(() => expect(write).toHaveBeenCalled());
    const text = String(write.mock.calls[0]?.[0]);
    expect(text).toContain(
      "Kurzfazit: Der Container web wurde wegen Speicher beendet.",
    );
    expect(text).toContain("- [live] web wurde vom OOM-Killer beendet [E1]");
    expect(text).toContain("Annahmen (nicht gesehen, nicht bestätigt):");
    expect(text).not.toContain("Rückweg");
    expect(text).toContain("E1 — docker.logs @ vm-docker");
    expect(text).toContain("fatal: out of memory");
  });

  it("says which machines were asked and which were not (E3-07 D4)", () => {
    const covered: ReportCard = {
      ...CARD,
      coverage: { asked: ["vm-docker"], not_asked: ["vm-two", "vm-three"] },
    };
    render(
      <I18nextProvider i18n={i18n}>
        <MessageView
          message={{ ...REPORT_MSG, report: covered }}
          busy={false}
        />
      </I18nextProvider>,
    );
    expect(screen.getByTestId("report-coverage").textContent).toBe(
      "Geprüft auf: vm-docker. Nicht geprüft: vm-two, vm-three — was nicht gefunden wurde, gilt nur für die ersten.",
    );
    expect(reportToText(i18n.t.bind(i18n) as never, covered)).toContain(
      "Nicht geprüft: vm-two",
    );
    // every machine asked: nothing to say
    cleanup();
    card();
    expect(screen.queryByTestId("report-coverage")).toBeNull();
  });

  it("names the reason of an incomplete check in words, and an unknown code as it is", () => {
    const t = i18n.t.bind(i18n) as never;
    const stopped = {
      ...CARD,
      incomplete: true,
      incomplete_reason: "repeated_calls",
    };
    expect(reportToText(t, stopped)).toContain(
      "Unvollständige Prüfung: die Abfragen wiederholten sich",
    );
    const odd = {
      ...CARD,
      incomplete: true,
      incomplete_reason: "something_new",
    };
    expect(reportToText(t, odd)).toContain(
      "Unvollständige Prüfung: something_new",
    );
  });

  it("says when a check is incomplete", () => {
    const incomplete: ReportCard = {
      ...CARD,
      incomplete: true,
      incomplete_reason: "bad_format",
    };
    render(
      <I18nextProvider i18n={i18n}>
        <MessageView
          message={{ ...REPORT_MSG, report: incomplete }}
          busy={false}
        />
      </I18nextProvider>,
    );
    expect(screen.getByTestId("report-incomplete").textContent).toContain(
      "Unvollständige Prüfung: das Modell antwortete nicht in der erwarteten Form",
    );
    expect(reportToText(i18n.t.bind(i18n) as never, incomplete)).toContain(
      "Unvollständige Prüfung",
    );
  });
});

describe("the messages of a finding request", () => {
  const stateWith = (messages: ChatMessage[]): ChatState => ({
    ...initialChatState,
    details: {
      c1: {
        id: "c1",
        title: "t",
        model: null,
        node: null,
        created_at: "",
        updated_at: "",
        message_count: 0,
        messages,
      },
    },
  });

  it("puts the confirmation and the card into the conversation and keeps the progress line out of it", () => {
    let state = chatReducer(stateWith([msg(1, "user", "Lagebericht?")]), {
      type: "stream_start",
      id: "c1",
      userMessage: null,
    });
    const ev = (e: unknown) => {
      state = chatReducer(state, {
        type: "stream_event",
        id: "c1",
        event: e as never,
      });
    };
    ev({ type: "bubble", message: msg(2, "assistant", "Schau ich mir an.") });
    ev({ type: "progress", message_id: 3, text: "docker.ps · vm — look" });
    expect(state.streams.c1?.progress).toBe("docker.ps · vm — look");
    ev({ type: "progress_done", message_id: 3 });
    expect(state.streams.c1?.progress).toBeUndefined();
    ev({ type: "report", message: REPORT_MSG });
    ev({
      type: "done",
      message: msg(8, "assistant", "Fertig: web braucht mehr Speicher."),
    });
    const kinds = state.details.c1?.messages.map((m) => [m.id, m.kind]);
    expect(kinds).toEqual([
      [1, "say"],
      [2, "say"],
      [7, "report"],
      [8, "say"],
    ]);
    expect(state.streams.c1).toBeUndefined();
  });

  it("drops everything the last question got when the answer is replaced", () => {
    const state = chatReducer(
      stateWith([
        msg(1, "user", "q"),
        msg(2, "assistant", "Schau ich mir an."),
        REPORT_MSG,
        msg(8, "assistant", "Fertig."),
      ]),
      { type: "drop_last_answer", id: "c1" },
    );
    expect(state.details.c1?.messages.map((m) => m.id)).toEqual([1]);
  });
});

describe("a finding request in the chat", () => {
  const CONV: Conversation = {
    id: "c1",
    title: "Lage",
    model: null,
    node: null,
    created_at: "2026-10-02T09:00:00Z",
    updated_at: "2026-10-02T09:00:00Z",
    message_count: 0,
  };

  it("shows the confirmation, the progress line, then the card and a short closing bubble", async () => {
    const meta = {
      conversation_id: "c1",
      user_message: msg(1, "user", "Gib mir einen Lagebericht"),
      continues: null,
      memory: [],
      model: "qwen-7b",
      node: "node-amd",
      fallback_from: null,
      fallback_state: null,
      checking: "forced",
      context: { length: 4096, estimated_prompt_tokens: 40, trimmed: 0 },
    };
    const mock = mockFetch({
      "GET /api/conversations": [CONV],
      "GET /api/conversations/c1": { ...CONV, messages: [] },
      "GET /api/chat/models": [],
      "GET /api/chat/settings": {
        user: {
          address: "du",
          memory_enabled: true,
          quality: "balanced",
          internet: true,
        },
        hub: {},
        effective_model: "qwen-7b",
      },
      "GET /api/chat/tools": {
        groups: [],
        nodes: [],
        internet: { node: null },
      },
      "POST /api/conversations/c1/messages": () =>
        sseResponse([
          ["meta", meta],
          ["bubble", { message: msg(2, "assistant", "Schau ich mir an.") }],
          ["tools_start", { task_id: CARD.task_id }],
          ["progress", { message_id: 3, text: "docker.ps · vm-docker — look" }],
          ["progress_done", { message_id: 3 }],
          ["report", { message: REPORT_MSG }],
          ["delta", { text: "Fertig, " }],
          ["delta", { text: "web braucht mehr Speicher." }],
          [
            "done",
            {
              message: msg(
                8,
                "assistant",
                "Fertig, web braucht mehr Speicher.",
                { agent_task_id: CARD.task_id },
              ),
            },
          ],
        ]),
    });
    const api = new ApiClient({ token: "t", fetchImpl: mock.fetch });
    render(
      <I18nextProvider i18n={i18n}>
        <TalkPage api={api} conversationId="c1" />
      </I18nextProvider>,
    );
    // the conversation is open once the loading line is gone
    await waitFor(() => expect(screen.queryByText("Lade …")).toBeNull());
    const box = await screen.findByLabelText("Schreib mir …");
    fireEvent.change(box, { target: { value: "Gib mir einen Lagebericht" } });
    fireEvent.click(screen.getByRole("button", { name: "Senden" }));
    expect(
      await screen.findByText("Fertig, web braucht mehr Speicher."),
    ).toBeTruthy();
    // one block for the reader: no "looking now" bubble, the card follows the answer
    expect(screen.queryByText("Schau ich mir an.")).toBeNull();
    expect(screen.getByTestId("report-card")).toBeTruthy();
    expect(screen.queryByTestId("progress-line")).toBeNull();
    // the card is folded under the answer; the check is shown once, in the card
    const folded = document.querySelector(
      "details.talk-report",
    ) as HTMLDetailsElement;
    expect(folded.open).toBe(false);
    folded.open = true;
    const checks = screen.getAllByTestId("stored-check");
    expect(checks).toHaveLength(1);
    expect(
      within(screen.getByTestId("report-card")).getByTestId("stored-check"),
    ).toBe(checks[0]);
    // the question and the answer; the card sits at the end of the answer's own bubble
    const rows = [...document.querySelectorAll(".talk-msg")];
    expect(
      rows.map((r) =>
        r.className.includes("talk-user") ? "user" : "assistant",
      ),
    ).toEqual(["user", "assistant"]);
    expect(rows[1]?.querySelector("details.talk-report")).toBeTruthy();
  });
});
