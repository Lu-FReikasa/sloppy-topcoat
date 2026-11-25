import { describe, expect, it } from "vitest";
import { dayKey, dayLabel, formatClock, formatFull } from "./time";

// 2026-10-01 22:30 UTC is 2026-10-02 00:30 in Berlin (CEST) and 2026-10-01 18:30 in New York.
const LATE = "2026-10-01T22:30:00Z";

describe("times in the chat", () => {
  it("shows the clock in the time zone of the viewer", () => {
    expect(formatClock(LATE, "de", "Europe/Berlin")).toBe("00:30");
    expect(formatClock(LATE, "en-GB", "America/New_York")).toBe("18:30");
    expect(formatClock(LATE, "de", "UTC")).toBe("22:30");
  });

  it("says the full date, time and zone as the hover text", () => {
    const full = formatFull(LATE, "de", "Europe/Berlin") ?? "";
    expect(full).toContain("Freitag");
    expect(full).toContain("2. Oktober 2026");
    expect(full).toContain("00:30:00");
    expect(full).toMatch(/MESZ|GMT\+2/);
    expect(formatFull(LATE, "en", "UTC")).toContain("October 1, 2026");
  });

  it("puts a message after midnight on the next day only in a zone where it is", () => {
    expect(dayKey(LATE, "Europe/Berlin")).toBe("2026-10-02");
    expect(dayKey(LATE, "America/New_York")).toBe("2026-10-01");
  });

  it("names today and yesterday in the language of the user, other days in full", () => {
    const now = new Date("2026-10-02T09:00:00Z");
    expect(dayLabel("2026-10-02T06:00:00Z", "de", now, "UTC")).toBe("Heute");
    expect(dayLabel("2026-10-01T06:00:00Z", "de", now, "UTC")).toBe("Gestern");
    expect(dayLabel("2026-10-02T06:00:00Z", "en", now, "UTC")).toBe("Today");
    expect(dayLabel("2026-10-01T06:00:00Z", "en", now, "UTC")).toBe("Yesterday");
    expect(dayLabel("2026-09-20T06:00:00Z", "de", now, "UTC")).toBe("Sonntag, 20. September 2026");
  });

  it("copes with a missing or broken time", () => {
    expect(formatClock(null, "de")).toBeNull();
    expect(formatClock("not a time", "de")).toBeNull();
    expect(dayKey(undefined)).toBeNull();
  });
});
