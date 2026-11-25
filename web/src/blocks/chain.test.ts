import { describe, expect, it } from "vitest";
import type { BlockRunStep } from "../types";
import { childGroups, describeActivity, liveByPath, topLevelProgress, type Block } from "./chain";

const step = (s: Partial<BlockRunStep> & { event: string; seq: number }): BlockRunStep => ({ ...s }) as BlockRunStep;

describe("liveByPath", () => {
  it("shows the block that runs now, with its node and model and what the agent does", () => {
    const steps: BlockRunStep[] = [
      step({ event: "step", seq: 0, at: 10, path: "stack[0]", b: "agent", state: "start" }),
      step({ event: "activity", seq: 1, at: 11, path: "stack[0]", what: "tool_step", data: { kind: "plan", capability: "web.search", node: "node-nvidia", text: "suche Quellen" } }),
      step({ event: "call", seq: 2, path: "stack[1]", b: "check", model: "qwen", node: "node-laptop" }),
    ];
    const live = liveByPath(steps, false);
    expect(live.get("stack[0]")?.state).toBe("active");
    expect(live.get("stack[0]")?.started).toBe(10);
    expect(live.get("stack[0]")?.activity).toEqual({
      key: "plan",
      values: { capability: "web.search", node: "node-nvidia", reason: "suche Quellen" },
    });
    expect(live.get("stack[1]")).toMatchObject({ state: "waiting", model: "qwen", node: "node-laptop" });
  });

  it("marks a finished block done with its end, and a failed run's stopped block failed", () => {
    const steps: BlockRunStep[] = [
      step({ event: "step", seq: 0, at: 1, path: "stack[0]", state: "start" }),
      step({ event: "step", seq: 1, at: 4, path: "stack[0]", state: "done", out: "draft" }),
      step({ event: "step", seq: 2, at: 5, path: "stack[1]", state: "start" }),
      step({ event: "step", seq: 3, at: 6, path: "stack[1]", state: "end" }),
    ];
    expect(liveByPath(steps, false).get("stack[0]")).toMatchObject({ state: "done", ended: 4 });
    expect(liveByPath(steps, true).get("stack[1]")?.state).toBe("failed");
    expect(liveByPath(steps, false).get("stack[1]")?.state).toBe("done");
  });

  it("keeps the latest round of a loop and the way the loop ended", () => {
    const steps: BlockRunStep[] = [
      step({ event: "step", seq: 0, path: "stack[2].stack[0]", state: "start", at: 1 }),
      step({ event: "step", seq: 1, path: "stack[2].stack[0]", state: "done", at: 2 }),
      step({ event: "loop_end", seq: 2, path: "stack[2]", reason: "done", rounds: 2 }),
    ];
    const live = liveByPath(steps, false);
    expect(live.get("stack[2].stack[0]")?.state).toBe("done");
    expect(live.get("stack[2]")?.loop).toEqual({ reason: "done", rounds: 2 });
  });
});

describe("childGroups", () => {
  it("gives the paths the engine uses for nested blocks", () => {
    const repeat: Block = { b: "repeat_until", stack: [{ b: "revise" }, { b: "check" }] };
    expect(childGroups("stack[2]", repeat)).toEqual([
      { label: "do", base: "stack[2].stack", blocks: repeat.stack },
    ]);
    const branch: Block = { b: "if", then: [{ b: "ask" }], else: [{ b: "stop" }] };
    expect(childGroups("stack[0]", branch).map((g) => g.base)).toEqual([
      "stack[0].then",
      "stack[0].else",
    ]);
    const par: Block = { b: "parallel", lanes: [[{ b: "ask" }], [{ b: "ask" }]] };
    expect(childGroups("stack[1]", par).map((g) => g.base)).toEqual([
      "stack[1].lanes[0]",
      "stack[1].lanes[1]",
    ]);
  });
});

describe("activity and progress", () => {
  it("says what an agent event means in one line", () => {
    expect(describeActivity(step({ event: "activity", seq: 0, what: "tools_wait" }))?.key).toBe("waiting");
    expect(describeActivity(step({ event: "activity", seq: 0, what: "tool_step", data: { kind: "note", text: "x" } }))).toEqual({
      key: "note",
      values: { text: "x" },
    });
    expect(describeActivity(step({ event: "activity", seq: 0, what: "tool_step", data: { kind: "other" } }))).toBeNull();
  });

  it("counts the top-level blocks that are done", () => {
    const stack: Block[] = [{ b: "agent" }, { b: "check" }, { b: "repeat_until" }];
    const live = liveByPath(
      [
        step({ event: "step", seq: 0, path: "stack[0]", state: "done" }),
        step({ event: "step", seq: 1, path: "stack[1]", state: "start" }),
      ],
      false,
    );
    expect(topLevelProgress(stack, live)).toEqual({ done: 1, total: 3 });
  });
});
