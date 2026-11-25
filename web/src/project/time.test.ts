import { describe, expect, it } from "vitest";
import { formatDue, fromInputValue, isOverdue, joinOffset, splitOffset, toInputValue } from "./time";

const BERLIN = "Europe/Berlin";

describe("the wall clock of a zone", () => {
  it("shows an instant as the value of a datetime-local input, and takes it back", () => {
    expect(toInputValue("2026-10-30T16:00:00Z", BERLIN)).toBe("2026-10-30T17:00");
    expect(fromInputValue("2026-10-30T17:00", BERLIN)).toBe("2026-10-30T17:00:00+01:00");
    expect(fromInputValue("2026-07-01T09:00", BERLIN)).toBe("2026-07-01T09:00:00+02:00");
    expect(toInputValue(fromInputValue("2026-03-29T09:00", BERLIN), BERLIN)).toBe("2026-03-29T09:00");
  });

  it("says nothing for an empty or broken value", () => {
    expect(toInputValue(null, BERLIN)).toBe("");
    expect(toInputValue("nonsense", BERLIN)).toBe("");
    expect(fromInputValue("", BERLIN)).toBeNull();
    expect(fromInputValue("tomorrow", BERLIN)).toBeNull();
  });

  it("shows the end of a day without a time", () => {
    expect(formatDue("2026-10-30T22:59:00Z", "en-GB", BERLIN)).toContain("30");
    expect(formatDue("2026-10-30T22:59:00Z", "en-GB", BERLIN)).not.toMatch(/\d:\d\d/);
    expect(formatDue("2026-10-30T16:00:00Z", "en-GB", BERLIN)).toMatch(/17:00/);
    expect(formatDue(null, "en-GB", BERLIN)).toBe("");
  });

  it("knows what is overdue", () => {
    const now = new Date("2026-10-29T09:00:00Z");
    expect(isOverdue("2026-10-29T08:59:00Z", now)).toBe(true);
    expect(isOverdue("2026-10-29T09:01:00Z", now)).toBe(false);
    expect(isOverdue(null, now)).toBe(false);
  });

  it("splits a reminder offset into a count and a unit, and joins it again", () => {
    expect(splitOffset(-86400)).toEqual({ amount: -1, unit: "days" });
    expect(splitOffset(-7200)).toEqual({ amount: -2, unit: "hours" });
    expect(splitOffset(-900)).toEqual({ amount: -15, unit: "minutes" });
    expect(splitOffset(0)).toEqual({ amount: 0, unit: "minutes" });
    expect(joinOffset(-2, "hours")).toBe(-7200);
    expect(joinOffset(3, "days")).toBe(259200);
  });
});
