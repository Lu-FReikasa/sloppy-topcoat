/** Times in the chat (E3-07 D2): the hub stores and sends UTC; here they are shown in the time
 *  zone of the browser (``timeZone`` is only passed by tests). */

function parse(iso: string | null | undefined): Date | null {
  if (!iso) return null;
  const d = new Date(iso);
  return Number.isNaN(d.getTime()) ? null : d;
}

/** "14:32" (the locale decides about 12/24 hours) — what stands on every message. */
export function formatClock(
  iso: string | null | undefined,
  locale: string,
  timeZone?: string,
): string | null {
  const d = parse(iso);
  return d ? new Intl.DateTimeFormat(locale, { timeStyle: "short", timeZone }).format(d) : null;
}

/** "Donnerstag, 2. Oktober 2026 um 14:32:05 MESZ" — the hover text of the time. */
export function formatFull(
  iso: string | null | undefined,
  locale: string,
  timeZone?: string,
): string | null {
  const d = parse(iso);
  if (!d) return null;
  return new Intl.DateTimeFormat(locale, {
    dateStyle: "full",
    timeStyle: "long",
    timeZone,
  }).format(d);
}

/** The calendar day in the viewer's time zone ("2026-10-02"): the key of the date separators. */
export function dayKey(iso: string | null | undefined, timeZone?: string): string | null {
  const d = parse(iso);
  if (!d) return null;
  const parts = new Intl.DateTimeFormat("en-CA", {
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
    timeZone,
  }).formatToParts(d);
  const get = (type: string) => parts.find((p) => p.type === type)?.value ?? "";
  return `${get("year")}-${get("month")}-${get("day")}`;
}

/** "today", "yesterday" (in the language of the user), else "Thursday, 2 October 2026". */
export function dayLabel(
  iso: string,
  locale: string,
  now: Date = new Date(),
  timeZone?: string,
): string | null {
  const d = parse(iso);
  const key = dayKey(iso, timeZone);
  if (!d || !key) return null;
  const today = dayKey(now.toISOString(), timeZone);
  const yesterday = dayKey(new Date(now.getTime() - 24 * 3600 * 1000).toISOString(), timeZone);
  const relative = new Intl.RelativeTimeFormat(locale, { numeric: "auto" });
  if (key === today) return capitalise(relative.format(0, "day"), locale);
  if (key === yesterday) return capitalise(relative.format(-1, "day"), locale);
  return new Intl.DateTimeFormat(locale, { dateStyle: "full", timeZone }).format(d);
}

function capitalise(text: string, locale: string): string {
  return text.charAt(0).toLocaleUpperCase(locale) + text.slice(1);
}
