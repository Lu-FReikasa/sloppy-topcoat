// "Put this text into the chat of that program": a message line hands an answer over to the
// development chat. The text is kept until that chat takes it, so it also works when the chat
// had to be opened first.
import { requestOpen } from "../layout/intent";

/** Fired when a text is waiting. */
export const COMPOSE_EVENT = "topcoat:compose";

const pending = new Map<string, string[]>();

export function requestCompose(application: string, text: string): void {
  pending.set(application, [...(pending.get(application) ?? []), text]);
  requestOpen({ module: "page.chat", props: {}, program: application });
  window.dispatchEvent(new Event(COMPOSE_EVENT));
}

/** The waiting texts of this program, joined by a new line, once. */
export function takeCompose(application: string): string | null {
  const texts = pending.get(application);
  pending.delete(application);
  return texts && texts.length > 0 ? texts.join("\n") : null;
}
