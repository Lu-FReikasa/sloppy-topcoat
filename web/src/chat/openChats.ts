// The open conversations Start gathers (2026-11-13): pinned ones, and those active within the last
// day and not marked done; the hub says which (`open`), a running one is open too. Grouped by the
// application they belong to, in the navigation's order.
import type { Conversation } from "../types";
import { APPLICATION_ORDER, applicationOf } from "./applications";

/** Fired when a conversation was made, pinned or marked done: Start's list looks again. */
export const CONVERSATIONS_EVENT = "topcoat:conversations";

export function announceConversations(): void {
  window.dispatchEvent(new Event(CONVERSATIONS_EVENT));
}

export function isOpenChat(c: Conversation): boolean {
  return c.open === true || c.running === true;
}

export interface OpenGroup {
  application: string;
  chats: Conversation[];
}

/** The open conversations by application; pinned first within a group, then the newest. */
export function openGroups(conversations: Conversation[]): OpenGroup[] {
  const by = new Map<string, Conversation[]>();
  for (const c of conversations) {
    if (!isOpenChat(c)) continue;
    const app = applicationOf(c);
    by.set(app, [...(by.get(app) ?? []), c]);
  }
  const order = [
    ...APPLICATION_ORDER.filter((a) => by.has(a)),
    ...[...by.keys()].filter((a) => !(APPLICATION_ORDER as readonly string[]).includes(a)).sort(),
  ];
  return order.map((application) => ({
    application,
    chats: (by.get(application) ?? []).sort(
      (a, b) =>
        Number(b.pinned === true) - Number(a.pinned === true) ||
        (b.updated_at ?? "").localeCompare(a.updated_at ?? ""),
    ),
  }));
}
