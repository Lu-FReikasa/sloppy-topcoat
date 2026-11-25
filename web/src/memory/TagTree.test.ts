import { describe, expect, it } from "vitest";
import i18n from "../i18n";
import { tagLabel } from "./TagTree";

describe("tagLabel", () => {
  it("translates the fixed roots and the seeded sensitive tags, and keeps every other tag's own word", async () => {
    const before = i18n.language;
    await i18n.changeLanguage("de");
    const t = i18n.t.bind(i18n) as (k: string, o?: Record<string, unknown>) => string;
    expect(tagLabel({ name: "project", root: true }, t)).toBe("Projekte");
    expect(tagLabel({ name: "health", root: false }, t)).toBe("Gesundheit");
    expect(tagLabel({ name: "finance", root: false }, t)).toBe("Finanzen");
    expect(tagLabel({ name: "garden", root: false }, t)).toBe("garden");
    await i18n.changeLanguage(before);
  });
});
