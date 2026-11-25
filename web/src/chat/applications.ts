// The chat list of the Chat application can show the conversations of one application or of all
// of them. A conversation from before applications belongs to Chat (the hub's own default).
import type { Conversation } from "../types";

export const DEFAULT_APPLICATION = "chat";

/** The applications in the order the navigation shows them. The same keys, colours and hues are in
 *  `proto/ui/applications.json`, which the hub reads; `applications.shared.test.ts` checks that they agree. */
export const APPLICATIONS = ["chat", "research", "dev", "admin", "project"] as const;
export const APPLICATION_ORDER = APPLICATIONS;

export type ApplicationFilter = "all" | string;

export function applicationOf(c: Conversation): string {
  return c.application ?? DEFAULT_APPLICATION;
}

/** The applications that have conversations, with their counts, in the navigation's order. Other
 *  applications the hub may add follow the known ones, in the order they first appear. */
export function applicationCounts(
  conversations: Conversation[],
): { application: string; count: number }[] {
  const counts = new Map<string, number>();
  for (const c of conversations) {
    const app = applicationOf(c);
    counts.set(app, (counts.get(app) ?? 0) + 1);
  }
  const known = APPLICATION_ORDER.filter((a) => counts.has(a));
  const others = [...counts.keys()].filter((a) => !(APPLICATION_ORDER as readonly string[]).includes(a));
  return [...known, ...others].map((application) => ({
    application,
    count: counts.get(application) ?? 0,
  }));
}

/** The conversations of one application, or all of them for "all". */
export function inApplication(conversations: Conversation[], filter: ApplicationFilter): Conversation[] {
  return filter === "all" ? conversations : conversations.filter((c) => applicationOf(c) === filter);
}

/** The accent of each application: the chat window takes it while that application's tab is open,
 *  so it is clear which application the conversation belongs to. Hues from the desktop palette. */
export const APPLICATION_ACCENT: Record<string, string> = {
  chat: "#6ee27a",
  research: "#7aa7ff",
  dev: "#ffb86b",
  admin: "#e87aff",
  project: "#6fd8d0",
};

/** The hue of each application on the desktop (title bar, taskbar, inbox tag). */
export const APPLICATION_HUE: Record<string, number> = {
  chat: 200,
  research: 275,
  dev: 25,
  admin: 355,
  project: 185,
};

export function applicationAccent(application: string): string {
  return APPLICATION_ACCENT[application] ?? APPLICATION_ACCENT.chat ?? "#6ee27a";
}
