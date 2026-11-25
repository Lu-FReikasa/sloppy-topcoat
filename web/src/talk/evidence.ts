import type { ChatMessage, MemoryRef } from "../types";

/** The sources behind an answer and who answered, from what the hub already stores: the notes
 *  the model saw, and the node when it is not the one the person expected (never hidden). The
 *  verdict on the answer is not made here: it is the traffic light of the second look. */
export interface Evidence {
  refs: MemoryRef[];
  /** What the older per-answer check found wrong, when there is one. */
  issues: string[];
  via: string | null;
  /** Why the expected node was passed over (its state), when the hub says. */
  viaFrom: string | null;
}

/** The traffic light of an answer, from its second look: waiting (yellow, the look is queued or
 *  running), ok (green), hint (orange, small flaws), error (red, a backed finding). */
export type Light = "waiting" | "ok" | "hint" | "error";

export function evidenceOf(message: ChatMessage): Evidence {
  const refs = (message.memory_refs ?? []).filter(
    (r) => r.status !== "deleted",
  );
  const verification = message.verification ?? null;
  const issues = verification && !verification.ok ? verification.issues : [];
  const via = message.fallback_from && message.node ? message.node : null;
  return { refs, issues, via, viaFrom: via ? message.fallback_from : null };
}

/** Whether the small mark under an answer is shown at all. */
export function hasMark(e: Evidence, light: Light | null): boolean {
  return light !== null || e.via !== null;
}
