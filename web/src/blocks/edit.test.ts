import { describe, expect, it } from "vitest";
import {
  EditError,
  analyze,
  checked,
  duplicateBlock,
  indent,
  insertBlock,
  moveBlock,
  moveWithin,
  newBlock,
  outdent,
  removeBlock,
  setField,
  stackAt,
  variablesAt,
  type Block,
  type Program,
} from "./edit";

const PROGRAM: Program = {
  name: "report",
  inputs: { question: "text" },
  stack: [
    { b: "agent", prompt: "{{question}}", out: "draft" },
    { b: "check", in: "draft", pick: { other_than: ["draft"] }, out: "v" },
    {
      b: "repeat_until",
      cond: "v.ok",
      max: 3,
      stack: [{ b: "revise", draft: "draft", issues: "v", out: "draft" }],
    },
    { b: "file_report", in: "draft", title: "{{question}}" },
  ],
};

const kinds = (stack: Block[] | undefined) => (stack ?? []).map((b) => b.b);

describe("the typing rules", () => {
  it("lets the shipped shape through", () => {
    expect(analyze(PROGRAM)).toEqual([]);
  });

  it("names a variable that is read before it exists, or with the wrong type", () => {
    const early = insertBlock(PROGRAM, [], 0, { b: "check", in: "draft", out: "v0" });
    expect(analyze(early).map((i) => i.message)).toContain("draft is not defined before this block");
    const wrong = setField(PROGRAM, [], 1, "in", "question"); // a text: fine
    expect(analyze(wrong)).toEqual([]);
    const verdictAsText = setField(PROGRAM, [], 3, "in", "v");
    expect(analyze(verdictAsText)[0]?.message).toBe("v is verdict, this block needs text");
  });

  it("wants a verdict or a yes/no to test, in the right form", () => {
    expect(analyze(setField(PROGRAM, [], 2, "cond", "draft.ok"))[0]?.message).toContain(
      "not a bool, verdict or compare",
    );
    expect(analyze(setField(PROGRAM, [], 2, "cond", "v"))[0]?.message).toBe("use v.ok here");
    expect(analyze(setField(PROGRAM, [], 2, "cond", "not v.ok"))).toEqual([]);
  });

  it("wants a loop to write something, and the lanes of a parallel to write different things", () => {
    const empty = setField(PROGRAM, [2, "stack"].slice(0, 0), 2, "stack", []);
    expect(analyze(empty).map((i) => i.message)).toContain("the body of a loop must write a variable");
    const twins: Program = {
      inputs: { q: "text" },
      stack: [
        {
          b: "parallel",
          max_parallel: 1,
          lanes: [
            [{ b: "set", value: "{{q}}", out: "a" }],
            [{ b: "set", value: "{{q}}", out: "a" }],
          ],
        },
      ],
    };
    expect(analyze(twins)[0]?.message).toContain("both write a");
  });

  it("knows what a branch hands on: only what both sides define", () => {
    const both: Program = {
      inputs: { q: "text", flag: "bool" },
      stack: [
        {
          b: "if",
          cond: "flag",
          then: [{ b: "set", value: "{{q}}", out: "x" }],
          else: [{ b: "set", value: "{{q}}", out: "x" }],
        },
        { b: "file_report", in: "x" },
      ],
    };
    expect(analyze(both)).toEqual([]);
    const one = { ...both, stack: [{ ...both.stack[0]!, else: [] }, both.stack[1]!] } as Program;
    expect(analyze(one)[0]?.message).toBe("x is not defined before this block");
  });
});

describe("what exists at a place", () => {
  it("lists the variables before a block, inside loops too", () => {
    expect(variablesAt(PROGRAM, [], 0)).toEqual({ question: "text" });
    expect(variablesAt(PROGRAM, [], 2)).toEqual({ question: "text", draft: "text", v: "verdict" });
    expect(variablesAt(PROGRAM, [2, "stack"], 0)).toMatchObject({ v: "verdict", draft: "text" });
    expect(variablesAt(PROGRAM, [], 4)).toMatchObject({ draft: "text" }); // the end of the stack
  });
});

describe("a new block comes with fields that pass", () => {
  it("fills text and verdict fields from what exists, and says what is missing", () => {
    expect(newBlock(PROGRAM, "check", [], 1)).toMatchObject({ b: "check", in: "draft" });
    expect(newBlock(PROGRAM, "if", [], 2)).toMatchObject({ cond: "v.ok", then: [], else: [] });
    expect(() => newBlock(PROGRAM, "if", [], 0)).toThrow(EditError); // nothing to test yet
    expect(newBlock(PROGRAM, "compare", [], 2)).toMatchObject({ in: ["question", "draft"] });
    expect(() => newBlock(PROGRAM, "for_each", [], 2)).toThrow(/list of texts/);
  });

  it("gives every new block a free name for what it writes and passes the rules", () => {
    for (const kind of ["ask", "agent", "check", "revise", "set", "repeat_until", "parallel", "file_report"]) {
      const block = newBlock(PROGRAM, kind, [], 3);
      const next = insertBlock(PROGRAM, [], 3, block);
      expect(analyze(next), kind).toEqual([]);
    }
    const first = newBlock(PROGRAM, "set", [], 3);
    expect(first.out).toBe("text");
    const second = newBlock(insertBlock(PROGRAM, [], 3, first), "set", [], 4);
    expect(second.out).toBe("text_2");
  });
});

describe("editing", () => {
  it("changes a copy and leaves the program alone", () => {
    const before = JSON.stringify(PROGRAM);
    removeBlock(PROGRAM, [], 3);
    moveWithin(PROGRAM, [], 0, 1);
    duplicateBlock(PROGRAM, [], 0);
    expect(JSON.stringify(PROGRAM)).toBe(before);
  });

  it("removes, moves and duplicates", () => {
    expect(kinds(removeBlock(PROGRAM, [], 3).stack)).toEqual(["agent", "check", "repeat_until"]);
    expect(kinds(moveWithin(PROGRAM, [], 3, -1).stack)).toEqual(["agent", "check", "file_report", "repeat_until"]);
    expect(() => moveWithin(PROGRAM, [], 0, -1)).toThrow(EditError);
    const dup = duplicateBlock(PROGRAM, [], 0);
    expect(kinds(dup.stack).slice(0, 2)).toEqual(["agent", "agent"]);
    expect(dup.stack[1]?.out).toBe("draft_2"); // a copy does not write the same variable
  });

  it("sets and clears a field", () => {
    const set = setField(PROGRAM, [], 2, "max", 5);
    expect(set.stack[2]?.max).toBe(5);
    expect("max" in (setField(set, [], 2, "max", "").stack[2] as object)).toBe(false);
  });

  it("puts a block into the block before it and takes it out again", () => {
    const into = indent(PROGRAM, [], 3);
    expect(kinds(into.stack)).toEqual(["agent", "check", "repeat_until"]);
    expect(kinds(stackAt(into, [2, "stack"]))).toEqual(["revise", "file_report"]);
    const out = outdent(into, [2, "stack"], 1);
    expect(kinds(out.stack)).toEqual(["agent", "check", "repeat_until", "file_report"]);
    expect(() => outdent(PROGRAM, [], 0)).toThrow(/not inside/);
    expect(() => indent(PROGRAM, [], 0)).toThrow(/before it/);
  });

  it("moves a block between places and counts the indices right", () => {
    // the check before the agent, in the same stack
    expect(kinds(moveBlock(PROGRAM, { path: [], index: 3 }, { path: [], index: 0 }).stack)[0]).toBe("file_report");
    // down: to the end (index 4 is "after the last", counted before the move)
    expect(
      kinds(moveBlock(PROGRAM, { path: [], index: 0 }, { path: [], index: 4 }).stack),
    ).toEqual(["check", "repeat_until", "file_report", "agent"]);
    // into the loop's stack, then the later siblings shift: the path 2 is the loop before and after
    const into = moveBlock(PROGRAM, { path: [], index: 1 }, { path: [2, "stack"], index: 0 });
    expect(kinds(into.stack)).toEqual(["agent", "repeat_until", "file_report"]);
    expect(kinds(stackAt(into, [1, "stack"]))).toEqual(["check", "revise"]); // the loop is now block 1
    expect(() => moveBlock(PROGRAM, { path: [], index: 2 }, { path: [2, "stack"], index: 0 })).toThrow(
      /into itself/,
    );
  });
});

describe("a move that cannot work is refused", () => {
  it("accepts a move that keeps the program valid and refuses one that reads too early", () => {
    const fine = moveWithin(PROGRAM, [], 3, -1); // report before the loop: still fine
    expect(checked(PROGRAM, fine)).toEqual({ ok: true });
    const early = moveWithin(PROGRAM, [], 1, -1); // the check before the agent that writes draft
    const verdict = checked(PROGRAM, early);
    expect(verdict.ok).toBe(false);
    expect(verdict.ok === false && verdict.message).toContain("draft is not defined before this block");
  });

  it("does not blame a move for problems the program already had", () => {
    const broken = setField(PROGRAM, [], 3, "in", "nothing");
    expect(analyze(broken)).toHaveLength(1);
    const moved = moveWithin(broken, [], 0, 1); // moving the agent now breaks the check too
    expect(checked(broken, moved).ok).toBe(false);
    const harmless = duplicateBlock(broken, [], 0);
    expect(checked(broken, harmless).ok).toBe(true);
  });
});

// The same cases are in hub/tests/test_blocks_api.py (RULE_CASES): the editor and the hub's
// validator must give the same verdict, so that a move the editor lets through is one the hub accepts.
describe("agrees with the hub", () => {
  const cases: [Program, string | null][] = [
    [{ inputs: { q: "text" }, stack: [{ b: "set", value: "{{q}}", out: "a" }] }, null],
    [{ inputs: { q: "text" }, stack: [{ b: "check", in: "nothing", out: "v" }] }, "nothing is not defined"],
    [
      {
        inputs: { q: "text" },
        stack: [
          { b: "check", in: "q", out: "v" },
          { b: "file_report", in: "v" },
        ],
      },
      "v is verdict, this block needs text",
    ],
    [
      {
        inputs: { q: "text" },
        stack: [
          { b: "check", in: "q", out: "v" },
          { b: "repeat_until", cond: "v", stack: [{ b: "set", value: "{{q}}", out: "x" }] },
        ],
      },
      "use v.ok here",
    ],
    [
      {
        inputs: { q: "text" },
        stack: [
          { b: "check", in: "q", out: "v" },
          { b: "repeat_until", cond: "v.ok", stack: [] },
        ],
      },
      "must write a variable",
    ],
    [
      {
        inputs: { q: "text" },
        stack: [
          {
            b: "parallel",
            lanes: [
              [{ b: "set", value: "{{q}}", out: "a" }],
              [{ b: "set", value: "{{q}}", out: "a" }],
            ],
          },
        ],
      },
      "both write a",
    ],
  ];
  it.each(cases)("gives the verdict of the hub for case %#", (program, problem) => {
    const messages = analyze(program).map((i) => i.message);
    if (problem === null) expect(messages).toEqual([]);
    else expect(messages.join(" | ")).toContain(problem);
  });
});
