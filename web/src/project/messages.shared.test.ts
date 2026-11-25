import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { describe, expect, it } from "vitest";
import { MESSAGE_STATES, MESSAGE_TYPES, messageLabel, messageType } from "./messages";

interface Entry {
  type: string;
  from: string[];
  to: string[];
  trust: string;
  needs_review: boolean;
  effect: string;
  label: { en: string; de: string };
}
const file = JSON.parse(
  readFileSync(resolve(process.cwd(), "../proto/messages/registry.json"), "utf8"),
) as { types: Entry[]; states: string[] };

describe("the message registry", () => {
  it("has the types of proto/messages/registry.json, in the same order", () => {
    expect(MESSAGE_TYPES.map((t) => t.type)).toEqual(file.types.map((t) => t.type));
  });

  it("agrees on the edges, the trust, the review, the effect and the names", () => {
    for (const entry of file.types) {
      const mine = messageType(entry.type)!;
      expect(mine.from, entry.type).toEqual(entry.from);
      expect(mine.to, entry.type).toEqual(entry.to);
      expect(mine.trust, entry.type).toBe(entry.trust);
      expect(mine.needsReview, entry.type).toBe(entry.needs_review);
      expect(mine.effect, entry.type).toBe(entry.effect);
      expect(mine.label, entry.type).toEqual(entry.label);
    }
  });

  it("agrees on the states", () => {
    expect([...MESSAGE_STATES]).toEqual(file.states);
  });

  it("names a type in the language of the person, and an unknown one as it is", () => {
    expect(messageLabel("project.request", "en")).toBe("Request");
    expect(messageLabel("project.request", "de-DE")).toBe("Anfrage");
    expect(messageLabel("made.up", "en")).toBe("made.up");
  });
});
