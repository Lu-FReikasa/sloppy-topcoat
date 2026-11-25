// The first message of a new conversation (a report asked about in the library) waits here until the new conversation is open and
// the chat page sends it (sessionStorage: it survives the hash change, not the tab).
const KEY = "topcoat.pendingPrompt";

export function setPendingPrompt(conversationId: string, text: string): void {
  try {
    window.sessionStorage.setItem(
      KEY,
      JSON.stringify({ id: conversationId, text }),
    );
  } catch {
    /* storage blocked: the conversation opens empty and the user types again */
  }
}

/** The waiting message for this conversation, removed on read; null if there is none. */
export function takePendingPrompt(conversationId: string): string | null {
  try {
    const raw = window.sessionStorage.getItem(KEY);
    if (!raw) return null;
    const v = JSON.parse(raw) as { id?: unknown; text?: unknown };
    if (v.id !== conversationId || typeof v.text !== "string") return null;
    window.sessionStorage.removeItem(KEY);
    return v.text;
  } catch {
    return null;
  }
}
