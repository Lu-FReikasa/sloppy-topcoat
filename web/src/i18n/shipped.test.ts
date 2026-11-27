import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { afterAll, describe, expect, it } from "vitest";
import i18n from ".";
import de from "./de.json";
import en from "./en.json";
import { SHIPPED_TITLE_KEY, blockProgramTitle, capabilityDescription, programTitle } from "./shipped";

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

  it("translates a shipped program without a title_key by its key (issue #181)", async () => {
    const w = { key: "research", title: "Research", title_key: null, builtin: true };
    await i18n.changeLanguage("de");
    expect(programTitle(w, t)).toBe("Recherche");
    // a title_key the catalogue lacks (an older or newer hub) falls back to the key, too
    expect(programTitle({ ...w, title_key: "workspace.gone" }, t)).toBe("Recherche");
    expect(programTitle({ ...w, key: "chat", title: "Start", title_key: "workspace.start" }, t)).toBe("Start");
    // the program `chat` is called Start (its seed's own title_key), not the application name "Chat"
    for (const title_key of [null, "workspace.gone"]) {
      expect(programTitle({ ...w, key: "chat", title: "Start", title_key }, t)).toBe("Start");
    }
    await i18n.changeLanguage("en");
    expect(programTitle(w, t)).toBe("Research");
  });

  it("keeps the hub's title of an unknown program or one a person made", async () => {
    await i18n.changeLanguage("de");
    const shipped = { title: "Sent title", title_key: null, builtin: true };
    for (const key of ["brand-new", "constructor", "report:abc", "Research", ""]) {
      expect(programTitle({ ...shipped, key }, t), key).toBe("Sent title");
    }
    // an admin's own program keeps its title, even under the key of a shipped one
    expect(programTitle({ key: "research", title: "Kids' research", title_key: null, builtin: false }, t)).toBe(
      "Kids' research",
    );
  });

  it("has a title in both catalogues for every program the hub ships", () => {
    const seeds = readFileSync(resolve(process.cwd(), "../hub/src/topcoat_hub/ui/seeds.py"), "utf8");
    const keys = [...seeds.matchAll(/_program\(\s*(?:#[^\n]*\s*)*"([a-z0-9-]+)"/g)].map((m) => m[1] ?? "");
    const explicit = [...seeds.matchAll(/title_key="workspace\.([a-z0-9-]+)"/g)].map((m) => m[1] ?? "");
    // a seed with its own title_key is in SHIPPED_TITLE_KEY, so a row without one reads the same
    for (const call of seeds.split("_program(").slice(1)) {
      const key = /^\s*(?:#[^\n]*\s*)*"([a-z0-9-]+)"/.exec(call)?.[1];
      const own = /title_key="([^"]+)"/.exec(call.split(/\n\]/)[0] ?? "")?.[1];
      if (key && own) expect(SHIPPED_TITLE_KEY[key], key).toBe(own);
    }
    expect(keys.length).toBeGreaterThanOrEqual(6); // the pattern still finds the programs
    for (const key of [...keys, ...explicit]) {
      for (const [lang, cat] of [["en", en], ["de", de]] as const) {
        const ws = cat.workspace as Record<string, string>;
        expect(ws[key], `${lang} workspace.${key}`).toBeTruthy();
      }
    }
  });
});
