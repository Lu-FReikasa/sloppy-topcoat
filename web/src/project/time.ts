// Times of the Projects page. The hub sends and takes ISO 8601 with an offset; the page shows and
// edits the wall clock of one zone (the person's own, here the browser's). Pure helpers, the zone is
// always an argument so the tests do not depend on where they run.
import { dayIn, zonedIso } from "./quickAdd";

export const browserZone = (): string => Intl.DateTimeFormat().resolvedOptions().timeZone;

const pad = (n: number) => String(n).padStart(2, "0");

/** `2026-10-30T17:00` for a `datetime-local` input, in `zone`; "" when there is no time. */
export function toInputValue(iso: string | null | undefined, zone: string): string {
  if (!iso) return "";
  const at = new Date(iso);
  if (Number.isNaN(at.getTime())) return "";
  const day = dayIn(at, zone);
  const p = new Intl.DateTimeFormat("en-GB", {
    timeZone: zone,
    hourCycle: "h23",
    hour: "2-digit",
    minute: "2-digit",
  }).formatToParts(at);
  const get = (type: string) => p.find((x) => x.type === type)?.value ?? "00";
  return `${day.y}-${pad(day.m)}-${pad(day.d)}T${get("hour")}:${get("minute")}`;
}

/** ISO 8601 with the offset of `zone` for the value of a `datetime-local` input; null when the
 *  field is empty or not a time. */
export function fromInputValue(value: string, zone: string): string | null {
  const m = /^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2})$/.exec(value.trim());
  if (!m) return null;
  return zonedIso({ y: Number(m[1]), m: Number(m[2]), d: Number(m[3]) }, Number(m[4]), Number(m[5]), zone);
}

/** "Fri 30 Oct, 17:00" (or the German form), short, in `zone`. A time of 23:59 is the end of a
 *  day (quick add): it shows without a time. */
export function formatDue(iso: string | null | undefined, locale: string, zone: string): string {
  if (!iso) return "";
  const at = new Date(iso);
  if (Number.isNaN(at.getTime())) return "";
  const wall = toInputValue(iso, zone);
  const endOfDay = wall.endsWith("T23:59");
  return new Intl.DateTimeFormat(locale, {
    timeZone: zone,
    weekday: "short",
    day: "numeric",
    month: "short",
    ...(endOfDay ? {} : { hour: "2-digit", minute: "2-digit" }),
  }).format(at);
}

/** Whether an open task's due time has passed. */
export function isOverdue(iso: string | null | undefined, now: Date): boolean {
  if (!iso) return false;
  const at = new Date(iso);
  return !Number.isNaN(at.getTime()) && at.getTime() < now.getTime();
}

/** Seconds as the reminder form shows them: a signed count of minutes, hours or days. */
export function splitOffset(seconds: number): { amount: number; unit: "minutes" | "hours" | "days" } {
  const abs = Math.abs(seconds);
  if (abs !== 0 && abs % 86400 === 0) return { amount: seconds / 86400, unit: "days" };
  if (abs !== 0 && abs % 3600 === 0) return { amount: seconds / 3600, unit: "hours" };
  return { amount: Math.round(seconds / 60), unit: "minutes" };
}

export function joinOffset(amount: number, unit: "minutes" | "hours" | "days"): number {
  return amount * (unit === "days" ? 86400 : unit === "hours" ? 3600 : 60);
}
