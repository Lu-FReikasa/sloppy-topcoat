import { describe, expect, it } from "vitest";
import { dayIn, parseQuickAdd, zonedIso } from "./quickAdd";

// Thursday 2026-10-29, 10:00 in Berlin (CET, +01:00 since the clocks went back on the 25th)
const NOW = new Date("2026-10-29T09:00:00Z");
const ZONE = "Europe/Berlin";
const parse = (text: string) => parseQuickAdd(text, NOW, ZONE);

describe("the clock helpers", () => {
  it("find the day in a zone, not in UTC", () => {
    expect(dayIn(new Date("2026-10-29T23:30:00Z"), "Europe/Berlin")).toEqual({ y: 2026, m: 10, d: 30 });
    expect(dayIn(new Date("2026-10-29T23:30:00Z"), "UTC")).toEqual({ y: 2026, m: 10, d: 29 });
  });

  it("give the offset that holds on that day, summer time included", () => {
    expect(zonedIso({ y: 2026, m: 7, d: 1 }, 9, 0, "Europe/Berlin")).toBe("2026-07-01T09:00:00+02:00");
    expect(zonedIso({ y: 2026, m: 12, d: 1 }, 9, 0, "Europe/Berlin")).toBe("2026-12-01T09:00:00+01:00");
    expect(zonedIso({ y: 2026, m: 10, d: 24 }, 23, 59, "Europe/Berlin")).toBe("2026-10-24T23:59:00+02:00");
    expect(zonedIso({ y: 2026, m: 10, d: 25 }, 9, 0, "Europe/Berlin")).toBe("2026-10-25T09:00:00+01:00");
    expect(zonedIso({ y: 2026, m: 1, d: 1 }, 8, 30, "Asia/Kolkata")).toBe("2026-01-01T08:30:00+05:30");
  });
});

describe("parseQuickAdd", () => {
  it("takes labels, a priority, a day, a time and a repeat out of one line", () => {
    expect(parse("Pay the invoice *money !3 tomorrow 17:00 every week")).toEqual({
      title: "Pay the invoice",
      labels: ["money"],
      priority: 3,
      due_at: "2026-10-30T17:00:00+01:00",
      repeat_rule: { every: 1, unit: "week", mode: "after", tz: ZONE },
    });
  });

  it("understands the German words", () => {
    expect(parse("Steuer machen morgen um 17 Uhr jeden Dienstag")).toMatchObject({
      title: "Steuer machen",
      due_at: "2026-10-30T17:00:00+01:00",
      repeat_rule: { every: 1, unit: "week" },
    });
    expect(parse("Rechnung in 3 Tagen").due_at).toBe("2026-11-01T23:59:00+01:00");
    expect(parse("Bericht nächsten Montag").due_at).toBe("2026-11-02T23:59:00+01:00");
    expect(parse("Wäsche übermorgen").due_at).toBe("2026-10-31T23:59:00+01:00");
    expect(parse("Zahnarzt 3.11. 14.30 Uhr").due_at).toBe("2026-11-03T14:30:00+01:00");
  });

  it("means the end of the day when there is a date and no time", () => {
    expect(parse("Report today").due_at).toBe("2026-10-29T23:59:00+01:00");
  });

  it("takes the coming weekday, never today", () => {
    expect(parse("Review thursday").due_at).toBe("2026-11-05T23:59:00+01:00");
    expect(parse("Review next friday").due_at).toBe("2026-10-30T23:59:00+01:00");
  });

  it("starts a weekly repeat on the named day when there is no date", () => {
    const r = parse("Standup every monday 9:30");
    expect(r.repeat_rule).toMatchObject({ every: 1, unit: "week" });
    expect(r.due_at).toBe("2026-11-02T09:30:00+01:00");
  });

  it("reads repeat words and counts", () => {
    expect(parse("Water plants every 3 days").repeat_rule).toMatchObject({ every: 3, unit: "day" });
    expect(parse("Gießen alle 2 Wochen").repeat_rule).toMatchObject({ every: 2, unit: "week" });
    expect(parse("Rent monthly").repeat_rule).toMatchObject({ every: 1, unit: "month", mode: "monthly" });
    expect(parse("Backup täglich").repeat_rule).toMatchObject({ every: 1, unit: "day", mode: "after" });
  });

  it("reads am and pm and a date written out", () => {
    expect(parse("Call at 5pm tomorrow").due_at).toBe("2026-10-30T17:00:00+01:00");
    expect(parse("Launch 2026-12-24").due_at).toBe("2026-12-24T23:59:00+01:00");
  });

  it("takes a date in the past year as the next one", () => {
    expect(parse("Birthday 3.2.").due_at).toBe("2027-02-03T23:59:00+01:00");
  });

  it("keeps a quoted label with a space and several labels", () => {
    const r = parse('Buy milk *"shopping list" *home');
    expect(r.labels).toEqual(["shopping list", "home"]);
    expect(r.title).toBe("Buy milk");
  });

  it("leaves a plain title alone, and words that only look like dates", () => {
    expect(parse("Write the report")).toEqual({
      title: "Write the report",
      labels: [],
      priority: null,
      due_at: null,
      repeat_rule: null,
    });
    expect(parse("So do it").due_at).toBeNull(); // "so" and "do" are no weekdays
    expect(parse("Sun protection").due_at).toBeNull();
    expect(parse("Fix bug #3.1").due_at).toBeNull();
    expect(parse("Version 2.5 release").due_at).toBeNull();
  });

  it("leaves an empty title when the line is only markers", () => {
    expect(parse("*home !2 tomorrow").title).toBe("");
  });
});
