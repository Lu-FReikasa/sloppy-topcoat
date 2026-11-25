// How the list of conversations looks (chat layout study): three variants of the same list.
//   liste  — grouped by day, two lines per entry (title, time and size); the default
//   dicht  — grouped, one line per entry with the time on the right (most entries per screen)
//   karten — like liste, but without an open conversation the list is a grid of cards across the page
// Kept per browser; it only changes layout, never data.
export const CHAT_LISTS = ["liste", "dicht", "karten"] as const;
export type ChatListVariant = (typeof CHAT_LISTS)[number];

export const DEFAULT_CHAT_LIST: ChatListVariant = "liste";

const KEY = "topcoat.chatlist";

/** Fired on `window` whenever a variant is applied, so open switches follow. */
export const CHAT_LIST_EVENT = "topcoat:chatlist";

export function isChatList(v: unknown): v is ChatListVariant {
  return typeof v === "string" && (CHAT_LISTS as readonly string[]).includes(v);
}

export function loadChatList(): ChatListVariant {
  try {
    const v = window.localStorage.getItem(KEY);
    if (isChatList(v)) return v;
  } catch {
    /* storage blocked: fall through to the default */
  }
  return DEFAULT_CHAT_LIST;
}

export function applyChatList(variant: ChatListVariant): void {
  document.documentElement.dataset.chatlist = variant;
  window.dispatchEvent(new Event(CHAT_LIST_EVENT));
  try {
    window.localStorage.setItem(KEY, variant);
  } catch {
    /* the variant still applies for this visit */
  }
}

export type DayGroup = "today" | "yesterday" | "week" | "older";

/** Which group an entry belongs to, by its calendar day in the viewer's time zone. */
export function dayGroup(iso: string | null | undefined, now: Date = new Date()): DayGroup {
  if (!iso) return "older";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return "older";
  const startOf = (x: Date) => new Date(x.getFullYear(), x.getMonth(), x.getDate()).getTime();
  const days = Math.round((startOf(now) - startOf(d)) / 86_400_000);
  if (days <= 0) return "today";
  if (days === 1) return "yesterday";
  if (days < 7) return "week";
  return "older";
}
