import { afterAll, describe, expect, it } from "vitest";
import i18n from ".";
import { blockProgramTitle, capabilityDescription } from "./shipped";

const t = i18n.t.bind(i18n) as (key: string, o?: Record<string, unknown>) => string;
const before = i18n.language;
afterAll(() => void i18n.changeLanguage(before));

describe("names the hub and the nodes send (issue #11)", () => {
  it("translates a program that ships by its name", async () => {
    const p = { name: "research_report", title: "Research report with revision", builtin: true };
    await i18n.changeLanguage("de");
    expect(blockProgramTitle(p, t)).toBe("Recherchebericht mit Überarbeitung");
    await i18n.changeLanguage("en");
    expect(blockProgramTitle(p, t)).toBe("Research report with revision");
  });

  it("keeps the title of a program that is unknown or a person's own", async () => {
    await i18n.changeLanguage("de");
    const newer = { name: "brand_new", title: "A program of a newer hub", builtin: true };
    expect(blockProgramTitle(newer, t)).toBe("A program of a newer hub");
    // a saved program's title is the person's own text, even under the name of a shipped one
    const mine = { name: "research_report", title: "My report", builtin: false };
    expect(blockProgramTitle(mine, t)).toBe("My report");
  });

  it("translates a known capability by its name", async () => {
    const c = { name: "system.info", description: "Host name, OS, kernel (/proc)." };
    await i18n.changeLanguage("de");
    expect(capabilityDescription(c, t)).toMatch(/^Hostname, Betriebssystem/);
    await i18n.changeLanguage("en");
    expect(capabilityDescription(c, t)).toMatch(/^Host name, OS, kernel/);
    expect(capabilityDescription(c, t)).not.toBe(c.description);
  });

  it("shows the node's own text for an unknown capability", async () => {
    await i18n.changeLanguage("de");
    for (const name of [
      "acme.widgets", // a group the GUI does not know
      "system.secrets", // a known group, an unknown action
      "system", // a group alone is not a capability
      "system.info.extra",
      "tools.cap", // never a key outside tools.cap
      "constructor.name", // not a lookup on Object.prototype
      "docker.__proto__",
      "Docker.PS",
    ]) {
      expect(capabilityDescription({ name, description: "sent text" }, t), name).toBe("sent text");
    }
  });
});
