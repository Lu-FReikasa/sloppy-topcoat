import type { ChatMessage, ConversationDetail, ReportCard } from "../types";
import { formatFull } from "./time";

/** The words the export needs, from the catalogue (so the file is in the user's language). */
export interface ExportLabels {
  you: string;
  assistant: string;
  report: string;
  assumptions: string;
  proposal: string;
  impact: string;
  rollback: string;
  incomplete: string;
}

function reportText(card: ReportCard, l: ExportLabels): string {
  const out: string[] = [`**${l.report}**`, "", card.summary.trim()];
  if (card.incomplete) out.push("", `_${l.incomplete}${card.incomplete_reason ? `: ${card.incomplete_reason}` : ""}_`);
  if (card.findings.length > 0) {
    out.push("");
    for (const f of card.findings) {
      const marks = f.evidence.length > 0 ? ` [${f.evidence.join(", ")}]` : "";
      out.push(`- [${f.tag}] ${f.claim}${marks}`);
    }
  }
  if (card.assumptions.length > 0) {
    out.push("", `**${l.assumptions}**`, "");
    for (const a of card.assumptions) out.push(`- ${a.claim} (${a.confirm_with})`);
  }
  if (card.proposal) {
    out.push("", `**${l.proposal}**`, "", card.proposal.text.trim());
    if (card.proposal.impact) out.push("", `${l.impact}: ${card.proposal.impact}`);
    if (card.proposal.rollback) out.push("", `${l.rollback}: ${card.proposal.rollback}`);
  }
  return out.join("\n");
}

function body(m: ChatMessage, l: ExportLabels): string | null {
  if (m.kind === "progress") return null; // a transient line, not part of the conversation
  if (m.kind === "report" && m.report) return reportText(m.report, l);
  const text = m.content.trim();
  return text === "" ? null : text;
}

/** The conversation as Markdown: a title, then every message under who wrote it and when. Only
 *  what is on screen is exported (no prompts, no memory, no tool input); progress lines are left
 *  out. ``timeZone`` is only passed by tests. */
export function conversationToMarkdown(
  conversation: ConversationDetail,
  l: ExportLabels,
  locale: string,
  fallbackTitle: string,
  timeZone?: string,
): string {
  const out: string[] = [`# ${conversation.title || fallbackTitle}`];
  for (const m of conversation.messages) {
    const text = body(m, l);
    if (text === null) continue;
    const who = m.role === "user" ? l.you : l.assistant;
    const at = formatFull(m.created_at, locale, timeZone);
    out.push("", `## ${who}${at ? ` · ${at}` : ""}`, "", text);
  }
  return out.join("\n") + "\n";
}

/** A file name from the title and the day: "plex-restart-2026-10-03.md". */
export function exportFileName(title: string, now: Date = new Date()): string {
  const slug = title
    .normalize("NFKD")
    .replace(/[̀-ͯ]/g, "")
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, "-")
    .replace(/^-+|-+$/g, "")
    .slice(0, 50);
  return `${slug || "chat"}-${now.toISOString().slice(0, 10)}.md`;
}
