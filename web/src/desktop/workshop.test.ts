import { describe, expect, it } from "vitest";
import { asHex, readVars, setVar } from "./workshop";

describe("the workshop's variables", () => {
  const css = "--bg: #f0f0f0;\n--text: #333;\n.dt-program { --bg: #fff; border: 1px solid; }";

  it("reads the bare variables only, not those inside a rule", () => {
    expect(readVars(css)).toEqual([
      ["--bg", "#f0f0f0"],
      ["--text", "#333"],
    ]);
  });

  it("sets a variable in place and adds a missing one at the top", () => {
    const dark = setVar(css, "--bg", "#000000");
    expect(dark.startsWith("--bg: #000000;")).toBe(true);
    expect(dark).toContain(".dt-program { --bg: #fff;"); // the rule keeps its own
    expect(setVar(css, "--accent", "#0ff").startsWith("--accent: #0ff;\n--bg: #f0f0f0;")).toBe(true);
    expect(setVar(undefined, "--bg", "#111")).toBe("--bg: #111;");
  });

  it("gives the colour input a six-digit hex", () => {
    expect(asHex("#333")).toBe("#333333");
    expect(asHex("#0A0b0C")).toBe("#0a0b0c");
    expect(asHex("rgb(0,0,0)")).toBeNull();
  });
});
