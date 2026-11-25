import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { describe, expect, it } from "vitest";
import { APPLICATIONS, APPLICATION_ACCENT, APPLICATION_HUE } from "./applications";

interface Entry {
  key: string;
  accent: string;
  hue: number;
}
const file = JSON.parse(
  readFileSync(resolve(process.cwd(), "../proto/ui/applications.json"), "utf8"),
) as { applications: Entry[] };

describe("the applications", () => {
  it("are the ones in proto/ui/applications.json, in the same order", () => {
    expect([...APPLICATIONS]).toEqual(file.applications.map((a) => a.key));
  });

  it("have the colours and hues the file names", () => {
    for (const a of file.applications) {
      expect(APPLICATION_ACCENT[a.key], a.key).toBe(a.accent);
      expect(APPLICATION_HUE[a.key], a.key).toBe(a.hue);
    }
  });
});
