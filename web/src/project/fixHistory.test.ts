import { describe, expect, it } from "vitest";
import { firstSentence, fixHistory, fixReason, shortPrint } from "./fixHistory";
import type { IssueFix } from "./types";

function fix(id: string, state: IssueFix["state"], extra: Partial<IssueFix> = {}): IssueFix {
  return {
    id,
    issue_id: "t1",
    summary: id,
    state,
    edits: [],
    files: [],
    branch: null,
    base_commit: null,
    commit: null,
    diff: "",
    tests: null,
    rollout: null,
    error: null,
    created_at: null,
    updated_at: null,
    ...extra,
  };
}

const A = "AssertionError | test_x.py::test_x | assert # == #";
const K = "KeyError | test_x.py::test_x | 'family'";

describe("fixHistory", () => {
  it("merges attempts in a row that failed with the same fingerprint, the newest one in front", () => {
    const rows = fixHistory([
      fix("f1", "failed", { failure_print: A }),
      fix("f2", "failed", { failure_print: A }),
      fix("f3", "failed", { failure_print: K }),
      fix("f4", "failed", { failure_print: K }),
      fix("f5", "failed", { failure_print: K }),
      fix("f6", "draft"),
    ]);
    expect(rows.map((r) => [r.fix.id, r.times, r.earlier.map((e) => e.id)])).toEqual([
      ["f2", 2, ["f1"]],
      ["f5", 3, ["f3", "f4"]],
      ["f6", 1, []],
    ]);
  });

  it("does not merge equal prints that are not in a row, nor attempts without a print", () => {
    const rows = fixHistory([
      fix("f1", "failed", { failure_print: A }),
      fix("f2", "failed", { failure_print: K }),
      fix("f3", "failed", { failure_print: A }),
      fix("f4", "draft"),
      fix("f5", "draft"),
      fix("f6", "failed"),
      fix("f7", "failed"),
    ]);
    expect(rows.map((r) => r.times)).toEqual([1, 1, 1, 1, 1, 1, 1]);
  });

  it("does not merge the same print under another state", () => {
    const rows = fixHistory([fix("f1", "failed", { failure_print: A }), fix("f2", "refused", { failure_print: A })]);
    expect(rows).toHaveLength(2);
  });

  it("leaves its input alone", () => {
    const fixes = [fix("f1", "failed", { failure_print: A }), fix("f2", "failed", { failure_print: A })];
    const first = fixHistory(fixes);
    expect(fixHistory(fixes)).toEqual(first);
    expect(fixes.map((f) => f.id)).toEqual(["f1", "f2"]);
  });
});

describe("the reason in one sentence", () => {
  it("takes the first sentence of the first line, on Unix and Windows line ends", () => {
    expect(firstSentence("Tests failed. See the log.\nTraceback …")).toBe("Tests failed.");
    expect(firstSentence("Tests failed\r\nTraceback …")).toBe("Tests failed");
    expect(firstSentence("v1.2 broke the build: x.py")).toBe("v1.2 broke the build: x.py");
    expect(firstSentence(null)).toBe("");
  });

  it("cuts a long sentence to 160 characters", () => {
    const out = firstSentence("x".repeat(400));
    expect(out).toHaveLength(160);
    expect(out.endsWith("…")).toBe(true);
  });

  it("is the error, else the roll-out's message", () => {
    expect(fixReason(fix("f", "failed", { error: "Tests red. More." }))).toBe("Tests red.");
    expect(fixReason(fix("f", "live", { rollout: { message: "The hub runs it. Build 3." } }))).toBe("The hub runs it.");
    expect(fixReason(fix("f", "draft"))).toBe("");
  });

  it("shortens a long fingerprint to one row", () => {
    expect(shortPrint(A)).toBe(A);
    expect(shortPrint(`${A} ${"y".repeat(80)}`)).toHaveLength(60);
    expect(shortPrint("a\n  b")).toBe("a b");
  });
});
