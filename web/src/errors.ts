import type { TFunction, i18n as I18n } from "i18next";
import type { ActionError } from "./types";

/**
 * Human-readable (German) text for an error code from the hub or a node.
 * Known codes use the catalogue; unknown ones show the raw message.
 */
export function describeError(err: ActionError, t: TFunction, i18n: I18n): string {
  const key = `errors.${err.code}`;
  if (i18n.exists(key)) return t(key);
  return t("errors.generic", { message: err.message || err.code });
}

export function toActionError(err: unknown): ActionError {
  if (err && typeof err === "object" && "code" in err && "message" in err) {
    const e = err as { code: unknown; message: unknown };
    return { code: String(e.code), message: String(e.message) };
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
