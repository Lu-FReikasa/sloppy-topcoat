// Every capability a node can offer (proto/capabilities.json, issue #206) has its text in both
// catalogues; a German GUI would otherwise show the node's English description.
import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { describe, expect, it } from "vitest";
import de from "./de.json";
import en from "./en.json";

const file = JSON.parse(readFileSync(resolve(process.cwd(), "../proto/capabilities.json"), "utf8")) as {
  capabilities: { name: string }[];
};
const names = file.capabilities.map((c) => c.name);

describe("capability texts", () => {
  it("reads the capabilities from the file", () => {
    expect(names.length).toBeGreaterThan(80);
    expect(names).toContain("windows.system");
  });

  it.each([
    ["en", en],
    ["de", de],
  ])("has a text for every capability in %s.json", (_lang, catalogue) => {
    const texts = (catalogue as { tools: { cap: Record<string, Record<string, string>> } }).tools.cap;
    const missing = names.filter((name) => {
      const [group, action] = name.split(".");
      return !texts[group!]?.[action!];
    });
    expect(missing).toEqual([]);
  });

  it("has no text for a capability that is not in the file", () => {
    const texts = (en as { tools: { cap: Record<string, Record<string, string>> } }).tools.cap;
    const listed = new Set(names);
    const extra = Object.entries(texts).flatMap(([group, actions]) =>
      Object.keys(actions)
        .map((action) => `${group}.${action}`)
        .filter((name) => !listed.has(name)),
    );
    expect(extra).toEqual([]);
  });
});
