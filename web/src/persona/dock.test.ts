import { describe, expect, it } from "vitest";
import type { ChatEvent } from "../api/client";
import type { ActivityItem, ChatMessage } from "../types";
import { applicationFor, dockMood, dockReduce, EMPTY_DOCK, KEEP_BUBBLES, shortText, waitingOf, type DockState } from "./dock";

const msg = (content: string, status: ChatMessage["status"] = "complete") => ({ content, status }) as ChatMessage;
const ev = (event: ChatEvent) => ({ type: "event" as const, event });
const tool = (status: ActivityItem["status"]) =>
  ({ id: 1, kind: "tool", status, title: "web.search", detail: null, data: {}, at: "", ms: null }) as ActivityItem;

describe("the dock's conversation", () => {
  it("turns a question and its streamed answer into two bubbles, Clipper working in between", () => {
    let s: DockState = dockReduce(EMPTY_DOCK, { type: "sent", text: "Wann kam Alien raus?" });
    expect(dockMood(s)).toBe("thinking");
    s = dockReduce(s, ev({ type: "bubble", message: msg("Schau ich mir an.") }));
    s = dockReduce(s, ev({ type: "activity", op: "add", item: tool("running") }));
    expect(dockMood(s)).toBe("working");
    s = dockReduce(s, ev({ type: "activity", op: "update", item: tool("ok") }));
    expect(s.activity).toHaveLength(1);
    s = dockReduce(s, ev({ type: "delta", text: "Am 25. Mai " }));
    s = dockReduce(s, ev({ type: "delta", text: "1979." }));
    expect(s.answer).toBe("Am 25. Mai 1979.");
    s = dockReduce(s, ev({ type: "done", message: msg("Am 25. Mai 1979.") }));
    expect(s.bubbles.map((b) => [b.who, b.text])).toEqual([
      ["me", "Wann kam Alien raus?"],
      ["clipper", "Schau ich mir an."],
      ["clipper", "Am 25. Mai 1979."],
    ]);
    expect(dockMood(s)).toBe("done");
  });

  it("shows a failure as a red bubble and keeps only the last few bubbles", () => {
    let s = dockReduce(EMPTY_DOCK, { type: "sent", text: "x" });
    s = dockReduce(s, ev({ type: "error", code: "no_model", message: "Kein Modell erreichbar." }));
    expect(s.bubbles.at(-1)).toMatchObject({ failed: true, text: "Kein Modell erreichbar." });
    expect(dockMood(s)).toBe("error");
    for (let i = 0; i < KEEP_BUBBLES + 4; i += 1) s = dockReduce(s, { type: "sent", text: `q${i}` });
    expect(s.bubbles).toHaveLength(KEEP_BUBBLES);
  });

  it("goes on with a stored talk: what was said, without the hub's cards; a running answer follows", () => {
    const stored = (id: number, role: "user" | "assistant", content: string, over: Record<string, unknown> = {}) =>
      ({ id, role, content, status: "complete", kind: "say", ...over }) as unknown as ChatMessage;
    const s = dockReduce(EMPTY_DOCK, {
      type: "loaded",
      id: "c1",
      running: true,
      messages: [
        stored(1, "user", "Wie viel Platz?"),
        stored(2, "assistant", "**card**", { kind: "report" }),
        stored(3, "assistant", "Genug."),
        stored(4, "assistant", "", { status: "running" }),
      ],
    });
    expect(s.conversationId).toBe("c1");
    expect(s.running).toBe(true);
    expect(s.bubbles.map((b) => `${b.who}:${b.text}`)).toEqual(["me:Wie viel Platz?", "clipper:Genug."]);
  });

  it("keeps a bubble at the perch short, cut at a word", () => {
    expect(shortText("kurz")).toEqual({ text: "kurz", cut: false });
    const long = shortText("wort ".repeat(100), 40);
    expect(long.cut).toBe(true);
    expect(long.text.length).toBeLessThanOrEqual(42);
    expect(long.text.endsWith("wort …")).toBe(true);
  });

  it("keeps what an answer left waiting for a click: prepared changes and a hand-over", () => {
    let s = dockReduce(EMPTY_DOCK, { type: "sent", text: "Lege eine Aufgabe an" });
    s = dockReduce(s, ev({ type: "report", message: { role: "assistant", kind: "report", content: "", report: { changes_for: "t1" } } as unknown as ChatMessage }));
    s = dockReduce(s, ev({ type: "handover", id: "h1" } as unknown as ChatEvent));
    expect(s.changesFor).toBe("t1");
    expect(s.handoverId).toBe("h1");
    // a new question starts afresh
    s = dockReduce(s, { type: "sent", text: "und jetzt?" });
    expect([s.changesFor, s.handoverId]).toEqual([null, null]);
    // a stored talk: the newest report's changes, the hand-over its answer links to
    const id = "8c2f0e4a-1b7d-4c3e-9a55-0d6b2e9f1a77";
    const stored = [
      { role: "assistant", kind: "report", content: "", report: { changes_for: "t9" } },
      { role: "assistant", kind: "say", content: `[Übergabe ansehen](#/handovers/${id})` },
    ] as unknown as ChatMessage[];
    expect(waitingOf(stored)).toEqual({ changesFor: "t9", handoverId: id, offer: null });
  });

  it("keeps an offered research until it is taken, and follows it once started", () => {
    const offer = (started?: string) =>
      ({ id: 12, role: "assistant", kind: "say", content: "Dafür lohnt sich eine Recherche.", status: "complete",
         report: { research_offer: { question: "Pixel-Animationen?", started } } }) as unknown as ChatMessage;
    let s = dockReduce(EMPTY_DOCK, { type: "sent", text: "Pixel-Animationen?" });
    s = dockReduce(s, ev({ type: "done", message: offer() }));
    expect(s.offer).toEqual({ question: "Pixel-Animationen?", messageId: 12 });
    s = dockReduce(s, { type: "offerTaken" });
    expect(s.offer).toBeNull();
    expect(s.running).toBe(true);
    // a stored talk: an offer that was taken is not offered again
    expect(waitingOf([offer("run-1")]).offer).toBeNull();
    expect(waitingOf([offer()]).offer?.messageId).toBe(12);
  });

  it("belongs to the window's application where it has one, else to Chat", () => {
    expect(applicationFor("research")).toBe("research");
    expect(applicationFor("wiki")).toBe("chat");
  });
});
