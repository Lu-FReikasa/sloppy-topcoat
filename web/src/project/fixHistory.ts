// An issue's fix attempts as a condensed history (#180): one row per attempt with its state, its
// failure fingerprint (hub failure_print.py) and the reason in one sentence; attempts in a row
// that ended the same way merge into one row ("×3"). Pure, so it is tested on its own like
// sheets/collapse.ts.
import type { IssueFix } from "./types";

export interface FixRow {
  /** The newest attempt the row stands for: its actions and output are the ones shown. */
  fix: IssueFix;
  /** The older attempts merged into this row, oldest first (empty for a single attempt). */
  earlier: IssueFix[];
  /** How many attempts the row stands for. */
  times: number;
}

/** Two attempts are the same when they ended in the same state with the same fingerprint. An
 *  attempt without a fingerprint (a draft, a passed or live fix) never merges. */
function same(a: IssueFix, b: IssueFix): boolean {
  return a.state === b.state && !!a.failure_print && a.failure_print === b.failure_print;
}

/** `fixes` oldest first, as the hub sends them; the rows keep that order. */
export function fixHistory(fixes: readonly IssueFix[]): FixRow[] {
  return fixes.reduce<FixRow[]>((rows, f) => {
    const last = rows[rows.length - 1];
    if (last && same(last.fix, f)) {
      rows[rows.length - 1] = {
        fix: f,
        earlier: [...last.earlier, last.fix],
        times: last.times + 1,
      };
    } else {
      rows.push({ fix: f, earlier: [], times: 1 });
    }
    return rows;
  }, []);
}

// The end of a sentence, a "." "!" or "?" before whitespace: matches the "." in "Tests failed. See
// the log"; finds nothing in "v1.2 broke" or "error: x". https://regex101.com/?regex=%5B.!%3F%5D(%3F%3D%5Cs)&testString=Tests%20failed.%20See%20the%20log
const SENTENCE_END = /[.!?](?=\s)/;
const REASON_MAX = 160;
const PRINT_MAX = 60;
// A line break, Unix or Windows: matches "\n" and "\r\n"; finds nothing in "a b".
// https://regex101.com/?regex=%5Cr%3F%5Cn
const LINE_BREAK = /\r?\n/;
// Runs of whitespace: matches the "  " in "a  b" and the "\n" in "a\nb"; finds nothing in "ab".
// https://regex101.com/?regex=%5Cs%2B&testString=a%20%20b
const SPACES = /\s+/g;

function cut(text: string, max: number): string {
  return text.length > max ? `${text.slice(0, max - 1).trimEnd()}…` : text;
}

/** The first sentence of a text, on one line and at most 160 characters: "Tests failed. See
 *  the log.\nTraceback …" gives "Tests failed."; an empty text gives "". */
export function firstSentence(text: string | null | undefined): string {
  const line = (text ?? "").trim().split(LINE_BREAK)[0]?.trim() ?? "";
  const end = SENTENCE_END.exec(line);
  return cut(end ? line.slice(0, end.index + 1) : line, REASON_MAX);
}

/** A fingerprint short enough for a row; the whole one goes into the title. */
export function shortPrint(print: string | null | undefined): string {
  return cut((print ?? "").replace(SPACES, " ").trim(), PRINT_MAX);
}

/** Why the attempt ended as it did: the error, else the roll-out's message (a live fix). */
export function fixReason(fix: IssueFix): string {
  return firstSentence(fix.error) || firstSentence(fix.rollout?.message);
}
