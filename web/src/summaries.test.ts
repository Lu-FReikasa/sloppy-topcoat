import { describe, expect, it } from "vitest";
import { makeNode } from "./test/fixtures";
import { nodeGlance, oneLine, summarizeTestOutput } from "./summaries";

// the end of a workshop run as the hub keeps it: hub pytest, then the web checks
const PYTEST_RED = [
  "hub/tests/test_x.py ..F.                                            [ 98%]",
  "=================================== FAILURES ===================================",
  "___________________________________ test_x ____________________________________",
  "    def test_x():",
  ">       assert count() == 2",
  "E       assert 3 == 2",
  "E        +  where 3 = count()",
  "=========================== short test summary info ============================",
  "FAILED hub/tests/test_x.py::test_x - assert 3 == 2",
  "FAILED hub/tests/test_y.py::test_y - KeyError: 'family'",
  "============ 2 failed, 3544 passed, 12 skipped, 3 warnings in 121.30s ============",
].join("\n");

const VITEST_RED = [
  "=== web: lint, typecheck, tests",
  " ✓ src/format.test.ts (12 tests) 8ms",
  " FAIL  src/summaries.test.ts > summarizeTestOutput > counts",
  "AssertionError: expected 1 to be 2 // Object.is equality",
  " ❯ src/summaries.test.ts:12:5",
  "",
  " Test Files  1 failed | 140 passed (141)",
  "      Tests  1 failed | 1450 passed | 2 skipped (1453)",
].join("\n");

describe("summarizeTestOutput", () => {
  it("counts a pytest run and names its first failing test with the error", () => {
    const s = summarizeTestOutput(PYTEST_RED);
    expect(s.counts).toEqual({ passed: 3544, failed: 2, errors: 0, skipped: 12 });
    expect(s.firstFailure).toEqual({ test: "hub/tests/test_x.py::test_x", error: "assert 3 == 2" });
  });

  it("adds the web checks' vitest counts to the hub's and keeps the first failure (the hub's)", () => {
    const s = summarizeTestOutput(`${PYTEST_RED}\n${VITEST_RED}`);
    expect(s.counts).toEqual({ passed: 4994, failed: 3, errors: 0, skipped: 14 });
    expect(s.firstFailure?.test).toBe("hub/tests/test_x.py::test_x");
  });

  it("finds a failing vitest test and the error line below it", () => {
    const s = summarizeTestOutput("hub ok\n========== 9 passed in 3.10s ==========\n" + VITEST_RED);
    expect(s.counts).toEqual({ passed: 1459, failed: 1, errors: 0, skipped: 2 });
    expect(s.firstFailure).toEqual({
      test: "src/summaries.test.ts > summarizeTestOutput > counts",
      error: "AssertionError: expected 1 to be 2 // Object.is equality",
    });
  });

  it("takes the first E line when the short summary has no message", () => {
    const s = summarizeTestOutput(
      "E   KeyError: 'family'\nFAILED hub/tests/test_x.py::test_x\n1 failed, 9 passed in 0.50s\n",
    );
    expect(s.counts).toEqual({ passed: 9, failed: 1, errors: 0, skipped: 0 });
    expect(s.firstFailure).toEqual({ test: "hub/tests/test_x.py::test_x", error: "KeyError: 'family'" });
  });

  it("counts collection errors and reads CRLF logs with colour codes", () => {
    const log =
      "\u001b[31mERROR hub/tests/test_z.py - ImportError: no module z\u001b[0m\r\n" +
      "\u001b[31m===== 1 error in 0.40s =====\u001b[0m\r\n";
    const s = summarizeTestOutput(log);
    expect(s.counts).toEqual({ passed: 0, failed: 0, errors: 1, skipped: 0 });
    expect(s.firstFailure).toEqual({ test: "hub/tests/test_z.py", error: "ImportError: no module z" });
  });

  it("says nothing it cannot read: a green run has no failure, a log without a summary no counts", () => {
    expect(summarizeTestOutput("===== 12 passed in 1.00s =====")).toEqual({
      counts: { passed: 12, failed: 0, errors: 0, skipped: 0 },
      firstFailure: null,
    });
    expect(summarizeTestOutput("npm ERR! Cannot find module 'mermaid'")).toEqual({
      counts: null,
      firstFailure: null,
    });
    // a test whose name has "passed" in it is no summary line
    expect(summarizeTestOutput("hub/tests/test_a.py::test_passed PASSED").counts).toBeNull();
  });
});

describe("oneLine", () => {
  it("keeps a short description whole and folds nothing", () => {
    const text = "Host name, OS, kernel, uptime, load, CPUs, RAM and swap of this machine.";
    expect(oneLine(text)).toEqual({ line: text, folded: false });
  });

  it("cuts at the colon that ends the gist", () => {
    expect(
      oneLine(
        "Status details of one container: state, exit code, restarts, health checks, mounts, networks. Never its environment variables.",
      ),
    ).toEqual({ line: "Status details of one container", folded: true });
    expect(
      oneLine(
        "Wofür der RAM verbraucht wird: Programme, Cache, Kernel, Swap, ZFS-ARC und die Prozesse, die am meisten belegen.",
      ),
    ).toEqual({ line: "Wofür der RAM verbraucht wird", folded: true });
  });

  it("keeps the first sentence when the rest follows in a second one", () => {
    expect(oneLine("Restart one container. Its service is down for a few seconds.")).toEqual({
      line: "Restart one container.",
      folded: true,
    });
    // an abbreviation does not end the sentence
    expect(oneLine("Zeigt z. B. den Zustand.").line).toBe("Zeigt z. B. den Zustand.");
  });

  it("does not cut at a short label before a colon", () => {
    const text = "Note: the node lists this one itself and the GUI has no text for it yet, so it is shown as sent.";
    const { line, folded } = oneLine(text);
    expect(line.startsWith("Note: the node")).toBe(true);
    expect(line.endsWith("…")).toBe(true);
    expect(line.length).toBeLessThanOrEqual(90);
    expect(folded).toBe(true);
  });

  it("cuts a long sentence at a comma or a word, never inside a word", () => {
    const { line } = oneLine(
      "Roll the hub out to an image already on this host, with digest check, dump, health check and a way back to the old one",
    );
    expect(line).toBe("Roll the hub out to an image already on this host, with digest check, dump…");
    // a bracket the cut would leave open goes with the rest
    expect(
      oneLine(
        "Einen Branch eines lokalen Repositorys auf den Stand seines Remotes bringen (nur vorwärts, ohne Merge-Commit).",
      ).line,
    ).toBe("Einen Branch eines lokalen Repositorys auf den Stand seines Remotes bringen…");
    const words = oneLine("word ".repeat(40), 30).line;
    expect(words).toMatch(/^(word )*word…$/);
    expect(words.length).toBeLessThanOrEqual(30);
  });

  it("puts a description that the node sent with line breaks on one line", () => {
    expect(oneLine("Widgets of\n  the ACME controller")).toEqual({
      line: "Widgets of the ACME controller",
      folded: false,
    });
  });
});

describe("nodeGlance", () => {
  it("gives the loaded models, RAM and each GPU of a node that is online", () => {
    expect(nodeGlance(makeNode())).toEqual({
      live: true,
      loaded: [{ name: "llama-8b", vramMb: 6000 }],
      ram: { usedMb: 8192, totalMb: 32768 },
      gpus: [{ name: "Fake GPU", usedMb: 8192, totalMb: 24576 }],
    });
  });

  it("counts a model marked loaded that the runner's list misses, once", () => {
    const node = makeNode({
      models: [
        { name: "llama-8b", loaded: true, vram_used_mb: 6000 },
        { name: "qwen-7b", loaded: true, vram_used_mb: null },
      ],
    });
    expect(nodeGlance(node).loaded).toEqual([
      { name: "llama-8b", vramMb: 6000 },
      { name: "qwen-7b", vramMb: null },
    ]);
  });

  it("leaves out the last figures of an offline node and GPUs without VRAM", () => {
    expect(nodeGlance(makeNode({ state: "offline", connected: false }))).toEqual({
      live: false,
      loaded: [],
      ram: null,
      gpus: [],
    });
    const node = makeNode({
      ram_total_mb: null,
      gpus: [{ index: 0, name: "Display", vendor: "other", vram_total_mb: 0, vram_used_mb: null }],
    });
    expect(nodeGlance(node)).toMatchObject({ ram: null, gpus: [] });
  });

  it("adds the shared system memory of an integrated GPU", () => {
    const node = makeNode({
      gpus: [
        {
          index: 0,
          name: "iGPU",
          vendor: "intel",
          vram_total_mb: 128,
          vram_used_mb: 90,
          integrated: true,
          shared_total_mb: 8192,
          shared_used_mb: 800,
        },
      ],
    });
    expect(nodeGlance(node).gpus).toEqual([
      { name: "iGPU", usedMb: 90, totalMb: 128, shared: { usedMb: 800, totalMb: 8192 } },
    ]);
  });
});
