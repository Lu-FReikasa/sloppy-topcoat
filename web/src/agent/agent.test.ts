// @vitest-environment node
import { describe, expect, it } from "vitest";
import { canContinue, evidenceId, said } from "./agent";
import type { AgentStep } from "../types";

const step = (content: string): AgentStep => ({
  seq: 1,
  kind: "assistant",
  data: { content },
  created_at: null,
});

describe("agent helpers", () => {
  it("reads a tool request out of the model's answer", () => {
    const s = said(
      step(
        'Sure:\n```json\n{"action":"tool","node":"vm","capability":"docker.ps","input":{"all":true},"reason":"look"}\n```',
      ),
    );
    expect(s).toEqual({
      kind: "tool",
      node: "vm",
      capability: "docker.ps",
      input: { all: true },
      reason: "look",
    });
  });

  it("recognises the diagnosis and keeps anything else as text", () => {
    expect(said(step('{"action":"finish","summary":"x"}')).kind).toBe("finish");
    expect(said(step("I think it is fine."))).toEqual({
      kind: "raw",
      text: "I think it is fine.",
    });
    expect(said(step("{broken"))).toEqual({ kind: "raw", text: "{broken" });
    expect(said(step('{"action":"run"}')).kind).toBe("raw");
  });

  it("offers Continue for stopped, failed and cut-off tasks only", () => {
    expect(canContinue("canceled", "canceled")).toBe(true);
    expect(canContinue("failed", "bad_format")).toBe(true);
    expect(canContinue("done", "max_turns")).toBe(true);
    expect(canContinue("done", null)).toBe(false);
    expect(canContinue("running", null)).toBe(false);
  });

  it("names the anchor of an evidence item", () => {
    expect(evidenceId("E3")).toBe("evidence-E3");
  });
});
