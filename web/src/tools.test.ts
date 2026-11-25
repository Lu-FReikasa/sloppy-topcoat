// @vitest-environment node
import { describe, expect, it } from "vitest";
import { capabilityStatus, inputTemplate, outputText, parseInput, toggleGrant } from "./tools";
import type { ToolCapability } from "./types";

const logs: ToolCapability = {
  name: "docker.logs",
  description: "x",
  read_only: true,
  input_schema: {
    required: ["container", "lines"],
    properties: { container: { type: "string" }, lines: { type: "integer" } },
  },
};

describe("tools helpers", () => {
  it("builds an input template from the required fields", () => {
    expect(JSON.parse(inputTemplate(logs))).toEqual({ container: "", lines: 0 });
    expect(inputTemplate(undefined)).toBe("{}");
  });

  it("accepts only a JSON object as input", () => {
    expect(parseInput("")).toEqual({});
    expect(parseInput(' {"container":"web"} ')).toEqual({ container: "web" });
    expect(parseInput("[1]")).toBeNull();
    expect(parseInput("null")).toBeNull();
    expect(parseInput("{oops")).toBeNull();
  });

  it("says where a capability stands", () => {
    expect(capabilityStatus(logs, ["docker.logs"], ["docker.logs"])).toBe("usable");
    expect(capabilityStatus(logs, ["docker.logs"], [])).toBe("not_granted");
    expect(capabilityStatus(logs, [], [])).toBe("off");
    const restart = { ...logs, name: "docker.restart", read_only: false };
    expect(capabilityStatus(restart, ["docker.restart"], ["docker.restart"])).toBe(
      "needs_confirmation",
    );
  });

  it("shows logs and files as text and the rest as JSON", () => {
    expect(outputText({ container: "web", lines: 2, log: "a\nb" })).toBe(
      "container: web  ·  lines: 2\n\na\nb",
    );
    expect(outputText({ path: "/x/.env", content: "A=1" })).toBe("path: /x/.env\n\nA=1");
    expect(outputText({ containers: [{ name: "web" }] })).toContain('"name": "web"');
  });

  it("toggles grants; all replaces the single names", () => {
    expect(toggleGrant([], "docker.ps")).toEqual(["docker.ps"]);
    expect(toggleGrant(["docker.ps"], "docker.logs")).toEqual(["docker.logs", "docker.ps"]);
    expect(toggleGrant(["docker.ps", "docker.logs"], "docker.ps")).toEqual(["docker.logs"]);
    expect(toggleGrant(["docker.ps"], "*")).toEqual(["*"]);
    expect(toggleGrant(["*"], "*")).toEqual([]);
    expect(toggleGrant(["*"], "docker.ps")).toEqual(["docker.ps"]);
  });
});
