import type { AgentStatus, AgentStep } from "../types";

export const ACTIVE: AgentStatus[] = ["queued", "running"];

/** What an assistant step says, for the timeline: a tool request, the diagnosis, or plain text. */
export type Said =
  | {
      kind: "tool";
      node: string;
      capability: string;
      input: unknown;
      reason: string;
    }
  | { kind: "finish" }
  | { kind: "raw"; text: string };

export function said(step: AgentStep): Said {
  const text = step.data.content ?? "";
  const start = text.indexOf("{");
  const end = text.lastIndexOf("}");
  if (start >= 0 && end > start) {
    try {
      const o: unknown = JSON.parse(text.slice(start, end + 1));
      if (o && typeof o === "object") {
        const a = o as Record<string, unknown>;
        if (
          a.action === "tool" &&
          typeof a.node === "string" &&
          typeof a.capability === "string"
        ) {
          return {
            kind: "tool",
            node: a.node,
            capability: a.capability,
            input: a.input ?? {},
            reason: typeof a.reason === "string" ? a.reason : "",
          };
        }
        if (a.action === "finish") return { kind: "finish" };
      }
    } catch {
      // not JSON: shown as it is
    }
  }
  return { kind: "raw", text };
}

/** Can "Continue" be offered? A stopped or failed task, or one cut short by a limit. */
export function canContinue(
  status: AgentStatus,
  stopReason: string | null,
): boolean {
  return (
    status === "canceled" ||
    status === "failed" ||
    (status === "done" && !!stopReason)
  );
}

/** The anchor of an evidence item on the task page. */
export function evidenceId(ref: string): string {
  return `evidence-${ref}`;
}
