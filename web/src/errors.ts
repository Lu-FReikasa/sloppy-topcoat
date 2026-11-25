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
