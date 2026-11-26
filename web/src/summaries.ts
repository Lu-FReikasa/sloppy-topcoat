// Short summaries of raw data for three views (issue #22): the output of a fix's test run, a
// capability's description and a node's state. The views show the summary first and keep the
// raw data one click away; nothing is removed, only folded.
import type { NodeSummary } from "./types";

// ---------------------------------------------------------------------------------------------
// Test output (IssuePanel): the log of the workshop's test run, hub pytest then the web checks
// (deploy/workshop/pytest/run-tests.sh). The hub keeps only its last 6000 characters, so the
// summaries at the end are there and the start may be cut.

export interface TestCounts {
  passed: number;
  failed: number;
  /** pytest's errors (setup, collection) */
  errors: number;
  skipped: number;
}

export interface TestSummary {
  /** the counts of every summary line found (pytest and vitest added up), null when none was */
  counts: TestCounts | null;
  /** the first failing test and its first error line, when the output names one */
  firstFailure: { test: string; error: string | null } | null;
}

// The colour codes of a terminal: "\u001b[31m" matches, "[31m" without the escape does not.
// eslint-disable-next-line no-control-regex -- the colour codes of a terminal are control characters
const ANSI = /\u001b\[[0-9;]*m/g;
// pytest's summary: "==== 1 failed, 3544 passed, 12 skipped in 121.30s ====" and "1 failed, 9 passed
// in 3.2s" match; "test_x.py::test_passed PASSED" does not.
const PYTEST_LINE = /\b\d+ (?:passed|failed|errors?|skipped)\b.*\bin \d+(?:\.\d+)?s\b|^\s*=+ .*\b\d+ (?:passed|failed|errors?)\b/;
// vitest's count of tests: "      Tests  1 failed | 1450 passed (1451)" matches; "Test Files  2 passed" does not.
const VITEST_LINE = /^\s*Tests\s+\d+ (?:passed|failed|skipped)/;
// pytest's short test summary: "FAILED hub/tests/test_x.py::test_x - AssertionError: assert 3 == 2"
// gives the test and the message; "test_x.py::test_x FAILED" (the verbose line) does not match.
const PYTEST_FAILED = /^(?:FAILED|ERROR) (\S+)(?: - (.*))?$/;
// vitest: " FAIL  src/a.test.ts > group > does x" gives "src/a.test.ts > group > does x";
// " FAIL  src/a.test.ts [ src/a.test.ts ]" (a file that did not load) does not match.
const VITEST_FAILED = /^\s*(?:FAIL|×|✗)\s+(\S+\.(?:test|spec)\.[cm]?[jt]sx? > .+?)(?:\s+\d+ms)?$/;
// the error below a failing vitest test: "AssertionError: expected 1 to be 2" and "TypeError: x is
// undefined" match; "  ❯ src/a.test.ts:12:5" does not.
const ERROR_LINE = /^\s*([A-Za-z]*(?:Error|Exception)\b.*)$/;
// pytest's lines of an error: "E   KeyError: 'family'" matches; "Error: x" does not.
const E_LINE = /^E {2,}(\S.*)$/;
// how far below a failing vitest test its error line is looked for
const ERROR_LOOKAHEAD = 30;

// each count of a summary line: "3 passed", "1 failed", "2 errors" (or "1 error"), "4 skipped"
const COUNT: Record<keyof TestCounts, RegExp> = {
  passed: /\b(\d+) passed\b/,
  failed: /\b(\d+) failed\b/,
  errors: /\b(\d+) errors?\b/,
  skipped: /\b(\d+) skipped\b/,
};

function countsOf(line: string): TestCounts {
  const n = (k: keyof TestCounts) => Number(line.match(COUNT[k])?.[1] ?? 0);
  return { passed: n("passed"), failed: n("failed"), errors: n("errors"), skipped: n("skipped") };
}

function add(a: TestCounts | null, b: TestCounts): TestCounts {
  if (!a) return b;
  return {
    passed: a.passed + b.passed,
    failed: a.failed + b.failed,
    errors: a.errors + b.errors,
    skipped: a.skipped + b.skipped,
  };
}

/** The failing test a line names, with the first error line that goes with it, or null. */
function failureAt(lines: readonly string[], i: number): TestSummary["firstFailure"] {
  const py = lines[i]!.match(PYTEST_FAILED);
  if (py) return { test: py[1]!, error: py[2]?.trim() || null };
  const vi = lines[i]!.match(VITEST_FAILED);
  if (!vi) return null;
  const below = lines.slice(i + 1, i + 1 + ERROR_LOOKAHEAD).find((l) => ERROR_LINE.test(l));
  return { test: vi[1]!.trim(), error: below?.match(ERROR_LINE)?.[1]?.trim() ?? null };
}

/** Passed/failed counts and the first failing test with its first error line, from the raw log of
 *  a test run. What the log does not say stays null; the raw log is the reference. */
export function summarizeTestOutput(output: string): TestSummary {
  const lines = output.replace(ANSI, "").split(/\r?\n/);
  // the last pytest summary counts (a log holds one per run); every vitest "Tests" line adds up
  const pytestLine = lines.filter((l) => PYTEST_LINE.test(l) && !VITEST_LINE.test(l)).at(-1);
  const pytest = pytestLine === undefined ? null : countsOf(pytestLine);
  const vitest = lines
    .filter((l) => VITEST_LINE.test(l))
    .map(countsOf)
    .reduce<TestCounts | null>(add, null);
  const counts = pytest && vitest ? add(pytest, vitest) : (pytest ?? vitest);

  const at = lines.findIndex((l) => PYTEST_FAILED.test(l) || VITEST_FAILED.test(l));
  const failure = at < 0 ? null : failureAt(lines, at);
  // a pytest failure without a message in the short summary: its first "E" line says what failed
  const firstE = lines.find((l) => E_LINE.test(l))?.match(E_LINE)?.[1]?.trim() ?? null;
  const firstFailure = failure && { ...failure, error: failure.error ?? firstE };
  return { counts, firstFailure };
}

// ---------------------------------------------------------------------------------------------
// Capability descriptions (ToolsSection): one line, the full text behind the toggle.

export const ONE_LINE_MAX = 90;
// a head before ":" or ";" shorter than this is a label ("Note:"), not the gist
const MIN_HEAD = 12;
// a comma in the second half of the room is a better place to cut than the last space
const COMMA_FROM = 0.5;
// the end of a sentence: "." "!" "?" after a word of three letters or more, so "Containers. Never"
// ends one and "z. B." or "e.g." does not
const SENTENCE_END = /(?<=[^\s.]{3})[.!?](?=\s|$)/;
// a clause mark followed by a space: "Status details of one container: state" cuts at ":";
// "http://x" does not
const CLAUSE_MARK = /[:;](?=\s)/;
// runs of white space, line breaks included: "a \n b" becomes "a b"
const SPACES = /\s+/g;

/** The first sentence of a description, cut at its first ":" or ";" and then at a word when it is
 *  still longer than `max`. `folded` says whether the line leaves anything out. */
export function oneLine(text: string, max = ONE_LINE_MAX): { line: string; folded: boolean } {
  const full = text.replace(SPACES, " ").trim();
  const end = full.search(SENTENCE_END);
  let line = end >= 0 ? full.slice(0, end + 1) : full;
  if (line.length > max || line.length < full.length) {
    const head = line.search(CLAUSE_MARK);
    if (head >= MIN_HEAD) line = line.slice(0, head);
  }
  if (line.length > max) line = `${cutAtWord(line.slice(0, max - 1))}…`;
  return { line, folded: line !== full };
}

/** `room` up to its last comma (when that keeps half of it), else up to its last space; never
 *  inside a bracket that the cut would leave open ("… bringen (nur…" becomes "… bringen…"). */
function cutAtWord(room: string): string {
  const comma = room.lastIndexOf(", ");
  const space = room.lastIndexOf(" ");
  const cut = comma >= room.length * COMMA_FROM ? comma : space;
  const head = (cut > 0 ? room.slice(0, cut) : room).trimEnd();
  const open = head.lastIndexOf(" (");
  return open > 0 && !head.includes(")", open) ? head.slice(0, open) : head;
}

// ---------------------------------------------------------------------------------------------
// Node at a glance (NodeDetail): what matters first (state, loaded models, memory, GPU); the
// inventory (OS, CPU, network cards, versions, runner) is folded.

export interface NodeGlance {
  /** the figures are live; an offline node's are the last it reported and are left out */
  live: boolean;
  /** the models loaded now, with their VRAM when known */
  loaded: { name: string; vramMb: number | null }[];
  ram: { usedMb: number | null; totalMb: number } | null;
  /** VRAM of each GPU; an integrated one also with the system memory it may use */
  gpus: {
    name: string;
    usedMb: number | null;
    totalMb: number;
    shared?: { usedMb: number | null; totalMb: number };
  }[];
}

export function nodeGlance(node: NodeSummary): NodeGlance {
  const live = node.connected && node.state !== "offline";
  if (!live) return { live, loaded: [], ram: null, gpus: [] };
  const running = node.loaded.map((m) => ({ name: m.name, vramMb: m.vram_used_mb ?? null }));
  // a model the list marks loaded that the runner's list misses (an older node) still counts
  const marked = node.models
    .filter((m) => m.loaded && !running.some((l) => l.name === m.name))
    .map((m) => ({ name: m.name, vramMb: m.vram_used_mb }));
  return {
    live,
    loaded: [...running, ...marked],
    ram: node.ram_total_mb === null ? null : { usedMb: node.ram_used_mb, totalMb: node.ram_total_mb },
    gpus: node.gpus
      .filter((g) => g.vram_total_mb > 0)
      .map((g) => ({
        name: g.name,
        usedMb: g.vram_used_mb,
        totalMb: g.vram_total_mb,
        ...(g.integrated && g.shared_total_mb !== undefined
          ? { shared: { usedMb: g.shared_used_mb ?? null, totalMb: g.shared_total_mb } }
          : {}),
      })),
  };
}
