import type { TFunction, i18n as I18n } from "i18next";
import type { ActionError } from "./types";

/**
 * Human-readable text for an error code from the hub or a node.
 * Known codes use the catalogue; unknown ones show the raw message.
 *
 * `busy` has two senders. The hub refuses with an HTTP status (429 when all places for programs
 * are taken): that keeps the catalogue text. A node refuses for its own reason ("node is busy
 * gaming; …", a benchmark still running) and names it in the message: that message is shown.
 * Telling them apart relies on the hub's `trigger_action` (hub/src/topcoat_hub/api/gui.py)
 * passing node refusals through in a 200 body (`ok: false`), so they carry no `status`.
 */
export function describeError(err: ActionError, t: TFunction, i18n: I18n): string {
  if (err.code === "busy" && err.status === undefined && err.message) {
    return t("errors.generic", { message: err.message });
  }
  const key = `errors.${err.code}`;
  if (i18n.exists(key)) return t(key);
  return t("errors.generic", { message: err.message || err.code });
}

/** An error thrown by an API call as an `ActionError`. The hub's HTTP status is kept, so a refusal
 *  of the hub itself is not mistaken for a node's (see `describeError`). */
export function toActionError(err: unknown): ActionError {
  if (err && typeof err === "object" && "code" in err && "message" in err) {
    const e = err as { code: unknown; message: unknown; status?: unknown };
    const out: ActionError = { code: String(e.code), message: String(e.message) };
    if (typeof e.status === "number") out.status = e.status;
    return out;
  }
  return { code: "unknown", message: err instanceof Error ? err.message : String(err) };
}

/** Whether an error means the hub did not answer at all: no network, or a proxy in front of it
 *  that could not reach it (502, 503 without the hub's own body, 504). The pages then say the
 *  hub cannot be reached instead of showing the HTTP status text. */
export function hubUnreachable(err: unknown): boolean {
  if (!err || typeof err !== "object" || !("status" in err)) return err instanceof TypeError;
  const e = err as { status: unknown; code?: unknown };
  if (e.status === 0) return e.code === "network";
  // the hub's own 503 (a busy model) carries its code; a proxy's has only the status
  return e.status === 502 || e.status === 504 || (e.status === 503 && e.code === "http_503");
}

/** The nodes named in a `wake_timeout` error ("node-a, node-b did not wake within 240 s …", the
 *  hub's gateway), or null when the message does not name them. */
export function wakeTimeoutNodes(message: string): string | null {
  const m = /^(.+?) did not wake within\b/.exec(message.trim());
  return m ? (m[1] as string) : null;
}
