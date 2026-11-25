import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { I18nextProvider } from "react-i18next";
import { afterEach, describe, expect, it, vi } from "vitest";
import i18n from "../i18n";
import type { ChatMessage, ConversationDetail } from "../types";
import { ExportButtons } from "./ExportButtons";
import { conversationToMarkdown, exportFileName, type ExportLabels } from "./exportMarkdown";

afterEach(cleanup);

const L: ExportLabels = {
  you: "You",
  assistant: "Assistant",
  report: "Server check",
  assumptions: "Assumptions",
  proposal: "Proposal",
  impact: "Effect",
  rollback: "Way back",
  incomplete: "Incomplete",
};

function msg(o: Partial<ChatMessage> & { id: number }): ChatMessage {
  return {
    role: "assistant",
    content: "",
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
    created_at: "2026-10-03T10:00:00Z",
    ...o,
  };
}

const CONV: ConversationDetail = {
  id: "c1",
  title: "Plex Neustart",
  model: null,
  node: null,
  created_at: "2026-10-03T10:00:00Z",
  updated_at: "2026-10-03T10:00:00Z",
  messages: [
    msg({ id: 1, role: "user", content: "Läuft Plex?" }),
    msg({ id: 2, kind: "progress", content: "docker.ps · node-a" }),
    msg({
      id: 3,
      kind: "report",
      content: "",
      report: {
        version: 1,
        task_id: "t1",
        status: "done",
        incomplete: false,
        incomplete_reason: null,
        summary: "Plex läuft.",
        findings: [{ claim: "Container plex ist up", tag: "live", evidence: ["E1"] }],
        assumptions: [{ claim: "Port ist offen", confirm_with: "curl", evidence: [] }],
        proposal: { text: "Nichts tun.", impact: "keine", rollback: "-", advice_only: true },
        evidence: [],
        notes: [],
        calls: 1,
      },
    }),
    msg({ id: 4, content: "Ja, Plex läuft." }),
  ],
};

describe("conversationToMarkdown", () => {
  const md = conversationToMarkdown(CONV, L, "en", "Untitled", "UTC");

  it("has the title, then each message under who wrote it", () => {
    expect(md.startsWith("# Plex Neustart\n")).toBe(true);
    expect(md).toContain("## You · ");
    expect(md).toContain("Läuft Plex?");
    expect(md).toContain("## Assistant · ");
    expect(md).toContain("Ja, Plex läuft.");
  });

  it("leaves transient progress lines out", () => {
    expect(md).not.toContain("docker.ps");
  });

  it("writes a report card as summary, findings with evidence marks, assumptions and proposal", () => {
    expect(md).toContain("Plex läuft.");
    expect(md).toContain("- [live] Container plex ist up [E1]");
    expect(md).toContain("- Port ist offen (curl)");
    expect(md).toContain("Effect: keine");
  });

  it("uses the fallback title for an unnamed conversation", () => {
    expect(conversationToMarkdown({ ...CONV, title: "" }, L, "en", "Untitled", "UTC")).toContain(
      "# Untitled",
    );
  });
});

describe("exportFileName", () => {
  it("makes a plain file name from title and day", () => {
    expect(exportFileName("Plex: Neustart nötig?", new Date("2026-10-03T10:00:00Z"))).toBe(
      "plex-neustart-notig-2026-10-03.md",
    );
    expect(exportFileName("???", new Date("2026-10-03T10:00:00Z"))).toBe("chat-2026-10-03.md");
  });
});

describe("the export buttons", () => {
  function setup(conversation: ConversationDetail) {
    render(
      <I18nextProvider i18n={i18n}>
        <ExportButtons conversation={conversation} />
      </I18nextProvider>,
    );
  }

  it("copy the Markdown to the clipboard", async () => {
    const writeText = vi.fn().mockResolvedValue(undefined);
    Object.defineProperty(navigator, "clipboard", { value: { writeText }, configurable: true });
    setup(CONV);
    fireEvent.click(screen.getByText(i18n.t("export.copy")));
    await waitFor(() => expect(writeText).toHaveBeenCalledTimes(1));
    expect(String(writeText.mock.calls[0]?.[0])).toContain("# Plex Neustart");
    expect(await screen.findByText(i18n.t("export.copied"))).toBeTruthy();
  });

  it("say so when the clipboard is not available", async () => {
    Object.defineProperty(navigator, "clipboard", { value: undefined, configurable: true });
    setup(CONV);
    fireEvent.click(screen.getByText(i18n.t("export.copy")));
    expect(await screen.findByText(i18n.t("export.failed"))).toBeTruthy();
  });

  it("are disabled for an empty conversation", () => {
    setup({ ...CONV, messages: [] });
    expect((screen.getByText(i18n.t("export.copy")) as HTMLButtonElement).disabled).toBe(true);
    expect((screen.getByText(i18n.t("export.download")) as HTMLButtonElement).disabled).toBe(true);
  });
});
