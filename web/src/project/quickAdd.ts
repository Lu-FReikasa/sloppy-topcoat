// Quick add (PPH-04, 04e): one line becomes a task. `Pay the invoice *money !3 tomorrow 17:00
// every week` is the title "Pay the invoice", the label "money", priority 3, due tomorrow at 17:00
// and repeating every week. English and German words work in one line (`morgen`, `nächsten
// Montag`, `in 3 Tagen`, `jeden Dienstag`). Pure: the clock and the time zone come in as arguments,
// so the result is the same everywhere.
//
// A date without a time means the end of that day (23:59), so a task due today is not "overdue" at
// 09:01. A label is `*word` or `*"two words"`; a priority is `!0` to `!5`. Whatever is left over is
// the title. Words that look like dates are taken out of it: if a title must contain "tomorrow",
// change the title after the task was made.
import type { ProjectRepeatRule } from "./types";

export interface QuickAdd {
  title: string;
  labels: string[];
  priority: number | null;
  /** ISO 8601 with the offset of `zone` at that moment. */
  due_at: string | null;
  repeat_rule: ProjectRepeatRule | null;
}

export interface Day {
  y: number;
  m: number;
  d: number;
}

const WEEKDAYS: Record<string, number> = {
  // 0 = Monday … 6 = Sunday. Full names only: "so", "do", "sat" and "sun" are words of their own.
  monday: 0, montag: 0,
  tuesday: 1, dienstag: 1,
  wednesday: 2, mittwoch: 2,
  thursday: 3, donnerstag: 3,
  friday: 4, freitag: 4,
  saturday: 5, samstag: 5, sonnabend: 5,
  sunday: 6, sonntag: 6,
}; // prettier-ignore

const UNITS: Record<string, ProjectRepeatRule["unit"]> = {
  minute: "minute", minutes: "minute", minuten: "minute",
  hour: "hour", hours: "hour", stunde: "hour", stunden: "hour",
  day: "day", days: "day", tag: "day", tage: "day", tagen: "day",
  week: "week", weeks: "week", woche: "week", wochen: "week",
  month: "month", months: "month", monat: "month", monate: "month", monaten: "month",
  year: "year", years: "year", jahr: "year", jahre: "year", jahren: "year",
}; // prettier-ignore

const W = "(?<![\\p{L}\\p{N}_])"; // a word starts here
const E = "(?![\\p{L}\\p{N}_])"; // and ends here
const re = (source: string) => new RegExp(`${W}(?:${source})${E}`, "iu");

const DAY_WORDS = Object.keys(WEEKDAYS).sort((a, b) => b.length - a.length).join("|");
const UNIT_WORDS = Object.keys(UNITS).sort((a, b) => b.length - a.length).join("|");

/** The calendar day of `now` in `zone`. */
export function dayIn(now: Date, zone: string): Day {
  const parts = new Intl.DateTimeFormat("en-CA", {
    timeZone: zone,
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
  }).formatToParts(now);
  const get = (type: string) => Number(parts.find((p) => p.type === type)?.value);
  return { y: get("year"), m: get("month"), d: get("day") };
}

function addDays(day: Day, n: number): Day {
  const t = new Date(Date.UTC(day.y, day.m - 1, day.d + n));
  return { y: t.getUTCFullYear(), m: t.getUTCMonth() + 1, d: t.getUTCDate() };
}

function addMonths(day: Day, n: number): Day {
  const t = new Date(Date.UTC(day.y, day.m - 1 + n, 1));
  const last = new Date(Date.UTC(t.getUTCFullYear(), t.getUTCMonth() + 1, 0)).getUTCDate();
  return { y: t.getUTCFullYear(), m: t.getUTCMonth() + 1, d: Math.min(day.d, last) };
}

/** Weekday of a day, 0 = Monday. */
function weekdayOf(day: Day): number {
  return (new Date(Date.UTC(day.y, day.m - 1, day.d)).getUTCDay() + 6) % 7;
}

const pad = (n: number) => String(n).padStart(2, "0");

/** The offset of `zone` at the wall-clock time, as `+01:00`. Settles the offset by trying the one
 *  just before and the one just after, so a time inside a change of summer time gets one of them. */
function offsetAt(day: Day, hour: number, minute: number, zone: string): number {
  const wall = Date.UTC(day.y, day.m - 1, day.d, hour, minute);
  const offsetOf = (instant: number) => {
    const p = new Intl.DateTimeFormat("en-US", {
      timeZone: zone,
      hourCycle: "h23",
      year: "numeric",
      month: "2-digit",
      day: "2-digit",
      hour: "2-digit",
      minute: "2-digit",
      second: "2-digit",
    }).formatToParts(new Date(instant));
    const g = (type: string) => Number(p.find((x) => x.type === type)?.value);
    const asUtc = Date.UTC(g("year"), g("month") - 1, g("day"), g("hour"), g("minute"), g("second"));
    return Math.round((asUtc - instant) / 60000);
  };
  const first = offsetOf(wall - offsetOf(wall) * 60000);
  return offsetOf(wall - first * 60000);
}

/** ISO 8601 with an offset for a wall-clock time in a zone. */
export function zonedIso(day: Day, hour: number, minute: number, zone: string): string {
  const minutes = offsetAt(day, hour, minute, zone);
  const sign = minutes < 0 ? "-" : "+";
  const abs = Math.abs(minutes);
  return (
    `${day.y}-${pad(day.m)}-${pad(day.d)}T${pad(hour)}:${pad(minute)}:00` +
    `${sign}${pad(Math.floor(abs / 60))}:${pad(abs % 60)}`
  );
}

function take(text: string, pattern: RegExp): [RegExpMatchArray | null, string] {
  const m = text.match(pattern);
  if (!m || m.index === undefined) return [null, text];
  return [m, `${text.slice(0, m.index)} ${text.slice(m.index + m[0].length)}`];
}

export function parseQuickAdd(
  input: string,
  now: Date = new Date(),
  zone: string = Intl.DateTimeFormat().resolvedOptions().timeZone,
): QuickAdd {
  let text = ` ${input} `;
  const labels: string[] = [];

  // labels: *word or *"two words"
  for (;;) {
    const [m, rest] = take(text, /(?<=\s)\*(?:"([^"]+)"|(\S+))/u);
    if (!m) break;
    labels.push((m[1] ?? m[2] ?? "").trim());
    text = rest;
  }

  // priority: !3
  let priority: number | null = null;
  {
    const [m, rest] = take(text, /(?<=\s)!([0-5])(?![\p{L}\p{N}])/u);
    if (m) {
      priority = Number(m[1]);
      text = rest;
    }
  }

  // repeat: every day / every 2 weeks / every tuesday / daily / jeden Tag / alle 3 Tage / jeden Dienstag
  let repeat: ProjectRepeatRule | null = null;
  let repeatDay: number | null = null;
  const everyN = re(`(?:every|alle|jede[rsmn]?)\\s+(\\d{1,3})\\s*(${UNIT_WORDS})`);
  const everyWord = re(`(?:every|jede[rsmn]?)\\s+(${UNIT_WORDS}|${DAY_WORDS})`);
  const adverb = re("daily|weekly|monthly|yearly|annually|täglich|woechentlich|wöchentlich|monatlich|jährlich");
  {
    let m: RegExpMatchArray | null;
    [m, text] = take(text, everyN);
    if (m) {
      repeat = { every: Number(m[1]), unit: UNITS[m[2]!.toLowerCase()]! };
    } else {
      [m, text] = take(text, everyWord);
      if (m) {
        const word = m[1]!.toLowerCase();
        if (word in UNITS) repeat = { every: 1, unit: UNITS[word]! };
        else {
          repeat = { every: 1, unit: "week" };
          repeatDay = WEEKDAYS[word]!;
        }
      } else {
        [m, text] = take(text, adverb);
        if (m) {
          const w = m[0].toLowerCase();
          const unit = /^(daily|täglich)$/.test(w)
            ? "day"
            : /^(weekly|w(ö|oe)chentlich)$/.test(w)
              ? "week"
              : /^(monthly|monatlich)$/.test(w)
                ? "month"
                : "year";
          repeat = { every: 1, unit };
        }
      }
    }
  }

  // time of day: 17:00, 17.30 uhr, um 17 uhr, at 5pm, 5 pm
  let time: [number, number] | null = null;
  {
    let m: RegExpMatchArray | null;
    // 17:00 always; 17.30 only with "Uhr" after it, because 3.11. is a date
    [m, text] = take(
      text,
      /(?<![\p{L}\p{N}_:.])(?:um\s+|at\s+)?(?:([01]?\d|2[0-3]):([0-5]\d)(?:\s*uhr)?|([01]?\d|2[0-3])\.([0-5]\d)(?=\s*uhr)\s*uhr)(?![\p{L}\p{N}_:])/iu,
    );
    if (m) time = [Number(m[1] ?? m[3]), Number(m[2] ?? m[4])];
    if (!time) {
      [m, text] = take(text, re("(?:um|at)\\s+(\\d{1,2})\\s*(am|pm|uhr)?"));
      if (m && Number(m[1]) <= 23) {
        let h = Number(m[1]);
        const mark = m[2]?.toLowerCase();
        if (mark === "pm" && h < 12) h += 12;
        if (mark === "am" && h === 12) h = 0;
        time = [h, 0];
      }
    }
    if (!time) {
      [m, text] = take(text, re("(\\d{1,2})\\s*(am|pm)"));
      if (m && Number(m[1]) >= 1 && Number(m[1]) <= 12) {
        let h = Number(m[1]);
        const mark = m[2]!.toLowerCase();
        if (mark === "pm" && h < 12) h += 12;
        if (mark === "am" && h === 12) h = 0;
        time = [h, 0];
      }
    }
  }

  // the day
  const today = dayIn(now, zone);
  let due: Day | null = null;
  {
    let m: RegExpMatchArray | null;
    [m, text] = take(text, re("(?:in|binnen)\\s+(\\d{1,3})\\s*(day|days|tag|tage|tagen|week|weeks|woche|wochen|month|months|monat|monate|monaten)"));
    if (m) {
      const n = Number(m[1]);
      const unit = UNITS[m[2]!.toLowerCase()]!;
      due = unit === "day" ? addDays(today, n) : unit === "week" ? addDays(today, 7 * n) : addMonths(today, n);
    }
    if (!due) {
      [m, text] = take(text, re("übermorgen|uebermorgen|day after tomorrow"));
      if (m) due = addDays(today, 2);
    }
    if (!due) {
      [m, text] = take(text, re("tomorrow|morgen"));
      if (m) due = addDays(today, 1);
    }
    if (!due) {
      [m, text] = take(text, re("today|heute|tonight|heute abend"));
      if (m) due = today;
    }
    if (!due) {
      [m, text] = take(text, re(`(?:(?:next|nächste[rnms]?|naechste[rnms]?|kommende[rnms]?)\\s+)?(${DAY_WORDS})`));
      if (m) {
        const target = WEEKDAYS[m[1]!.toLowerCase()]!;
        const ahead = (target - weekdayOf(today) + 7) % 7 || 7; // never today: "monday" is the coming one
        due = addDays(today, ahead);
      }
    }
    if (!due) {
      [m, text] = take(text, /(?<![\p{L}\p{N}_.])(\d{4})-(\d{2})-(\d{2})(?![\p{L}\p{N}_])/u);
      if (m) due = { y: Number(m[1]), m: Number(m[2]), d: Number(m[3]) };
    }
    if (!due) {
      [m, text] = take(text, /(?<![\p{L}\p{N}_.])(\d{1,2})\.(\d{1,2})\.(\d{4})?(?![\p{L}\p{N}_])/u);
      if (m) {
        let y = m[3] ? Number(m[3]) : today.y;
        const day: Day = { y, m: Number(m[2]), d: Number(m[1]) };
        if (!m[3] && (day.m < today.m || (day.m === today.m && day.d < today.d))) y += 1; // the next one
        due = { ...day, y };
      }
    }
  }
  if (due && (due.m < 1 || due.m > 12 || due.d < 1 || due.d > 31)) due = null;
  // "every tuesday" with no date of its own: the first one is the coming tuesday
  if (!due && repeatDay !== null) {
    due = addDays(today, (repeatDay - weekdayOf(today) + 7) % 7 || 7);
  }
  if (repeat && (repeat.unit === "month" || repeat.unit === "year")) repeat = { ...repeat, mode: "monthly", tz: zone };
  else if (repeat) repeat = { ...repeat, mode: "after", tz: zone };

  const [hour, minute] = time ?? [23, 59];
  const due_at = due || time ? zonedIso(due ?? today, hour, minute, zone) : null;
  return {
    title: text.replace(/\s+/g, " ").trim(),
    labels,
    priority,
    due_at,
    repeat_rule: repeat,
  };
}
