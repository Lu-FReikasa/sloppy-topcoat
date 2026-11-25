/** Clipper at a window (the desktop plan, phase 5a): the conversation as speech bubbles. This file
 *  is the arithmetic: which event of the hub's answer stream makes which bubble, and Clipper's mood. */
import type { ChatEvent } from "../api/client";
import type { ActivityItem, ChatMessage } from "../types";
import { APPLICATIONS, DEFAULT_APPLICATION } from "../chat/applications";
import type { Mood } from "./Clipper";

export interface Bubble {
  id: string;
  who: "me" | "clipper";
  text: string;
  failed?: boolean;
}

export interface DockState {
  conversationId: string | null;
  bubbles: Bubble[];
  running: boolean;
  /** The answer as it streams in. */
  answer: string;
  activity: ActivityItem[];
  last: "done" | "error" | null;
  /** The check of the current answer (its place in the queue comes by its id). */
  taskId: string | null;
  /** What the answer left waiting for the person's click (2026-11-13: the talk at a window showed
   *  only text, so prepared changes and hand-overs were there but nobody could see them): the
   *  check whose changes are prepared, and the hand-over that waits to be sent. */
  changesFor: string | null;
  handoverId: string | null;
  /** The newest answer offered a research that is not started yet: its question and message. */
  offer: { question: string; messageId: number } | null;
}

export const EMPTY_DOCK: DockState = {
  conversationId: null,
  bubbles: [],
  running: false,
  answer: "",
  activity: [],
  last: null,
  taskId: null,
  changesFor: null,
  handoverId: null,
  offer: null,
};

/** The research an answer offers and nobody started yet. */
function offerOf(m: ChatMessage): DockState["offer"] {
  const o = m.report?.research_offer;
  return o && !o.started ? { question: o.question, messageId: m.id } : null;
}

/** The hand-over a stored answer links to (`#/handovers/<id>`, api/chat.py). */
const HANDOVER_LINK = /#\/handovers\/([0-9a-f-]{36})/i;

/** What a stored conversation left waiting: the newest report's prepared changes, the newest
 *  answer's hand-over. */
export function waitingOf(
  messages: readonly ChatMessage[],
): Pick<DockState, "changesFor" | "handoverId" | "offer"> {
  let changesFor: string | null = null;
  let handoverId: string | null = null;
  let offer: DockState["offer"] = null;
  for (const m of messages) {
    if (m.role !== "assistant") continue;
    if (m.kind === "report" && m.report?.changes_for) changesFor = m.report.changes_for;
    const link = HANDOVER_LINK.exec(m.content);
    if (link?.[1]) handoverId = link[1];
    offer = offerOf(m) ?? (m.kind === "say" || !m.kind ? null : offer);
  }
  return { changesFor, handoverId, offer };
}

/** How many bubbles the talk keeps: the column shows them, the perch only the newest; the whole
 *  talk is in Chat. */
export const KEEP_BUBBLES = 40;

/** A bubble at the perch stays a readable block (2026-11-13): longer text is cut at a word, the
 *  rest is in the column. */
export const SHORT_BUBBLE = 280;

export function shortText(text: string, max = SHORT_BUBBLE): { text: string; cut: boolean } {
  const t = text.trim();
  if (t.length <= max) return { text: t, cut: false };
  const at = t.lastIndexOf(" ", max);
  return { text: `${t.slice(0, at > max * 0.6 ? at : max).trimEnd()} …`, cut: true };
}

/** The bubbles of a stored conversation (a window pinned, restored or reloaded takes its talk
 *  along, 2026-11-13): what was said, not the hub's cards and progress lines. */
export function bubblesOf(messages: readonly ChatMessage[]): Bubble[] {
  return keep(
    messages
      .filter((m) => (m.kind ?? "say") === "say" && (m.content.trim() || m.role === "user") && m.status !== "running")
      .map((m) => ({
        id: `m${m.id}`,
        who: m.role === "user" ? ("me" as const) : ("clipper" as const),
        text: m.content,
        failed: m.status === "error",
      })),
  );
}

export type DockAction =
  | { type: "loaded"; id: string; messages: ChatMessage[]; running: boolean }
  | { type: "offerTaken" }
  | { type: "sent"; text: string }
  | { type: "conversation"; id: string }
  | { type: "event"; event: ChatEvent }
  | { type: "failed"; text: string }
  | { type: "ended" };

let seq = 0;
const nextId = () => `b${(seq += 1)}`;

function keep(bubbles: Bubble[]): Bubble[] {
  return bubbles.slice(-KEEP_BUBBLES);
}

export function dockReduce(s: DockState, a: DockAction): DockState {
  switch (a.type) {
    case "offerTaken":
      return { ...s, offer: null, running: true, last: null };
    case "loaded":
      return {
        ...EMPTY_DOCK,
        conversationId: a.id,
        bubbles: bubblesOf(a.messages),
        running: a.running,
        ...waitingOf(a.messages),
      };
    case "sent":
      return {
        ...s,
        bubbles: keep([...s.bubbles, { id: nextId(), who: "me", text: a.text }]),
        running: true,
        answer: "",
        activity: [],
        last: null,
        taskId: null,
        changesFor: null,
        handoverId: null,
        offer: null,
      };
    case "conversation":
      return { ...s, conversationId: a.id };
    case "failed":
      return {
        ...s,
        running: false,
        answer: "",
        last: "error",
        bubbles: keep([...s.bubbles, { id: nextId(), who: "clipper", text: a.text, failed: true }]),
      };
    case "ended":
      return s.running ? { ...s, running: false, last: s.last ?? "done" } : s;
    case "event": {
      const ev = a.event;
      switch (ev.type) {
        case "delta":
          return { ...s, answer: s.answer + ev.text };
        case "tools_start":
          return { ...s, taskId: ev.task_id };
        case "report":
          // the hub's card of the check: what it prepared waits for a click
          return ev.message.report?.changes_for ? { ...s, changesFor: ev.message.report.changes_for } : s;
        case "handover":
          return { ...s, handoverId: ev.id };
        case "discard":
          return { ...s, answer: "" };
        case "activity": {
          const at = s.activity.findIndex((i) => i.id === ev.item.id);
          const activity = at >= 0 ? s.activity.map((i, n) => (n === at ? ev.item : i)) : [...s.activity, ev.item];
          return { ...s, activity };
        }
        case "bubble":
          // the hub's own line before a check ("Schau ich mir an.")
          return { ...s, bubbles: keep([...s.bubbles, { id: nextId(), who: "clipper", text: ev.message.content }]) };
        case "done":
          return {
            ...s,
            offer: offerOf(ev.message),
            running: false,
            answer: "",
            last: ev.message.status === "error" ? "error" : "done",
            bubbles: keep([
              ...s.bubbles,
              { id: nextId(), who: "clipper", text: ev.message.content || s.answer, failed: ev.message.status === "error" },
            ]),
          };
        case "stopped":
          return {
            ...s,
            running: false,
            answer: "",
            last: "done",
            bubbles: s.answer || ev.message?.content
              ? keep([...s.bubbles, { id: nextId(), who: "clipper", text: ev.message?.content || s.answer }])
              : s.bubbles,
          };
        case "error":
          return {
            ...s,
            running: false,
            answer: "",
            last: "error",
            bubbles: keep([...s.bubbles, { id: nextId(), who: "clipper", text: ev.message, failed: true }]),
          };
        default:
          return s;
      }
    }
  }
}

/** Clipper's mood at the window: work beats everything, then the last outcome. */
export function dockMood(s: DockState): Mood {
  if (s.running) return s.activity.some((i) => i.status === "running" && i.kind === "tool") ? "working" : "thinking";
  if (s.last === "error") return "error";
  if (s.last === "done") return "done";
  return "idle";
}

/** The application a window's conversation belongs to: the program's own when it has one. */
export function applicationFor(programKey: string): string {
  return (APPLICATIONS as readonly string[]).includes(programKey) ? programKey : DEFAULT_APPLICATION;
}
