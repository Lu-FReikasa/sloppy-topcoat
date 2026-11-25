import { describe, expect, it } from "vitest";
import { clockText } from "./Taskbar";

describe("clockText", () => {
  it("shows the person's zone, Berlin when the zone is unknown, never universal time", () => {
    const now = new Date("2026-11-13T00:30:05Z");
    expect(clockText(now, "Europe/Berlin")).toBe("01:30:05");
    expect(clockText(now, "America/New_York")).toBe("19:30:05");
    expect(clockText(now, "Not/AZone")).toBe("01:30:05");
  });
});
