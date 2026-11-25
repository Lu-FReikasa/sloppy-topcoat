/** The herald (the desktop plan, 5b): which ends of tasks Clipper tells the person about on the
 *  desktop, and the events the app hands on from its stream (`topcoat:task`). */

export const TASK_EVENT = "topcoat:task";

/** One `agent_task` or `agent_progress` event of the stream, handed on by the app. */
export interface TaskSignal {
  event: "agent_task" | "agent_progress";
  data: Record<string, unknown>;
}

/** Hand a stream event on to whoever listens on this page (the herald, Clipper at a window). */
export function signalTask(event: string, data: unknown): void {
  if ((event !== "agent_task" && event !== "agent_progress") || !data || typeof data !== "object") return;
  window.dispatchEvent(new CustomEvent<TaskSignal>(TASK_EVENT, { detail: { event, data: data as Record<string, unknown> } }));
}

export interface Notice {
  id: string;
  title: string;
  failed: boolean;
  application: string | null;
}

/** A chat check the person waits for in the chat is no news; one that ran a while may have been
 *  left behind. */
export const CHAT_NEWS_SECONDS = 45;

/** Is this event the end of a task worth telling about? Only top-level tasks the person started
 *  (the Tasks page, a batch, a hand-over, a long chat check), never the steps of a program. */
export function noticeOf(s: TaskSignal): Notice | null {
  if (s.event !== "agent_task") return null;
  const d = s.data;
  const status = d.status;
  if (status !== "done" && status !== "failed") return null;
  if (d.parent_id || d.origin === "child") return null;
  if (d.origin === "chat" && (typeof d.seconds !== "number" || d.seconds < CHAT_NEWS_SECONDS)) return null;
  if (typeof d.id !== "string") return null;
  return {
    id: d.id,
    title: typeof d.title === "string" && d.title.trim() ? d.title.trim() : "",
    failed: status === "failed",
    application: typeof d.application === "string" ? d.application : null,
  };
}

/** Is this the herald's page (`?herald=1`, the desktop client's corner window)? */
export function heraldOf(search: string): boolean {
  return new URLSearchParams(search).get("herald") === "1";
}
