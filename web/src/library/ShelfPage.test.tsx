import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { ApiClient } from "../api/client";
import { INTENT_EVENT, takeIntent } from "../layout/intent";
import type { Artifact, Conversation, Report } from "../types";
import { ShelfPage } from "./ShelfPage";
import { appCounts, matches, shelfItems } from "./shelfModel";

afterEach(cleanup);

const report = (over: Partial<Report> = {}): Report => ({
  id: "r1",
  application: "research",
  title: "Pixel-Animationen lokal",
  question: "q",
  language: "de",
  depth: "thorough",
  status: "draft",
  current_version: 2,
  released_version: null,
  task_id: "t1",
  conversation_id: null,
  unread: true,
  created_at: "2026-11-13T10:00:00Z",
  updated_at: "2026-11-13T10:00:00Z",
  released_at: null,
  auto: false,
  ...over,
});

const artifact = (over: Partial<Artifact> = {}): Artifact => ({
  id: "a1",
  kind: "report",
  title: "Warum startet web neu?",
  task_id: "t2",
  conversation_id: null,
  unread: false,
  created_at: "2026-11-12T08:00:00Z",
  updated_at: "2026-11-12T08:00:00Z",
  application: "dev",
  auto: false,
  ...over,
});

const chat = (over: Partial<Conversation> = {}): Conversation =>
  ({
    id: "c1",
    title: "Proxmox backup",
    model: null,
    node: null,
    created_at: "2026-11-11T08:00:00Z",
    updated_at: "2026-11-11T08:00:00Z",
    application: "research",
    ...over,
  }) as Conversation;

describe("the list model", () => {
  it("lists a task once: its research report, not the library copy it also filed", () => {
    const items = shelfItems([report()], [artifact({ id: "dup", task_id: "t1" }), artifact()], [chat()]);
    expect(items.map((i) => i.key)).toEqual(["r:r1", "a:a1", "c:c1"]);
  });

  it("filters by application, kind, words, and hides the hub's own work unless asked", () => {
    const items = shelfItems([report(), report({ id: "r2", auto: true, title: "Ziel: x" })], [artifact()], [chat()]);
    const base = { app: "all", kind: "all" as const, auto: false, query: "" };
    expect(items.filter((i) => matches(i, base)).map((i) => i.id)).toEqual(["r1", "a1", "c1"]);
    expect(items.filter((i) => matches(i, { ...base, auto: true }))).toHaveLength(4);
    expect(items.filter((i) => matches(i, { ...base, app: "research" })).map((i) => i.id)).toEqual(["r1", "c1"]);
    expect(items.filter((i) => matches(i, { ...base, kind: "documents" })).map((i) => i.id)).toEqual(["r1", "a1"]);
    expect(items.filter((i) => matches(i, { ...base, kind: "chats" })).map((i) => i.id)).toEqual(["c1"]);
    expect(items.filter((i) => matches(i, { ...base, query: "PIXEL" })).map((i) => i.id)).toEqual(["r1"]);
    expect(appCounts(items, ["chat", "research", "dev"])).toEqual([
      { application: "research", count: 3 },
      { application: "dev", count: 1 },
    ]);
  });
});

function api(over: Record<string, unknown> = {}) {
  return {
    reports: vi.fn().mockResolvedValue({ reports: [report(), report({ id: "r2", auto: true, title: "Ziel: x" })] }),
    artifacts: vi.fn().mockResolvedValue({ artifacts: [artifact()], unread: 0 }),
    listConversations: vi.fn().mockResolvedValue([chat()]),
    ...over,
  } as unknown as ApiClient;
}

describe("the Ablage", () => {
  it("of an application holds what it made, and a document opens a window of its own", async () => {
    const a = api();
    render(<ShelfPage api={a} application="research" />);
    const link = await screen.findByRole("link", { name: "Pixel-Animationen lokal" });
    // the development report is not here, nor what a flow made by itself, nor any conversation
    expect(screen.queryByText("Warum startet web neu?")).toBeNull();
    expect(screen.queryByText("Ziel: x")).toBeNull();
    expect(screen.queryByText("Proxmox backup")).toBeNull();
    expect(screen.queryByRole("group", { name: "Anwendung" })).toBeNull();
    expect((a as unknown as { listConversations: ReturnType<typeof vi.fn> }).listConversations).not.toHaveBeenCalled();
    const seen = vi.fn();
    window.addEventListener(INTENT_EVENT, seen);
    fireEvent.click(link);
    window.removeEventListener(INTENT_EVENT, seen);
    expect(seen).toHaveBeenCalled();
    expect(takeIntent()).toEqual({
      module: "page.document",
      props: { kind: "report", id: "r1", title: "Pixel-Animationen lokal" },
    });
  });

  it("of Start holds the documents of every application, and on request what flows made", async () => {
    render(<ShelfPage api={api()} application="chat" />);
    await screen.findByRole("link", { name: "Pixel-Animationen lokal" });
    expect(screen.getByRole("link", { name: "Warum startet web neu?" })).toBeTruthy();
    const apps = screen.getByRole("group", { name: "Anwendung" });
    fireEvent.click(within(apps).getByRole("button", { name: /Entwicklung/ }));
    await waitFor(() => expect(screen.queryByRole("link", { name: "Pixel-Animationen lokal" })).toBeNull());
    fireEvent.click(within(apps).getByRole("button", { name: /Alle/ }));
    fireEvent.click(screen.getByRole("checkbox"));
    expect(screen.getByRole("link", { name: "Ziel: x" })).toBeTruthy();
  });
});
