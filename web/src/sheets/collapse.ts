// Collapsing repeated parts (#141): a part the song plays again is written out the first time
// only; later it is one line ("Chorus ×2"). Pure, so the sheet view and the stage share it.
import type { SheetLine, SheetSection } from "./types";

/** One block of the body: a part written out, or a line that points back to one above. */
export interface BodyItem {
  section: SheetSection;
  /** true: the part was written out above; only its name is shown. */
  again: boolean;
  /** How often it is played here in a row (back-to-back repeats merge into one block). */
  times: number;
  /** The indices of all parts this block stands for (anchors for the flow). */
  indices: number[];
}

/** Every part as written, nothing collapsed. */
export function asWritten(sections: SheetSection[]): BodyItem[] {
  return sections.map((s) => ({ section: s, again: false, times: 1, indices: [s.index] }));
}

// Runs of whitespace: matches the "  " in "a  b" and the "\t" in "a\tb"; finds nothing in "ab".
const SPACES = /\s+/g;

function lineKey(ln: SheetLine): unknown {
  if (ln.comment !== undefined) return ["#", ln.comment.trim()];
  return (ln.segments ?? []).map((g) => [g.chord ?? "", g.text.replace(SPACES, " ")]);
}

function labelKey(s: SheetSection): string {
  return `${s.kind}|${s.label.trim().replace(SPACES, " ").toLowerCase()}`;
}

/** The identity of a part: its label plus its content. A `{chorus}` reference (`repeat_of`) is
 *  the part it points to; one whose target is missing (hidden as tab) has none. */
function identity(s: SheetSection, byIndex: Map<number, SheetSection>): string | null {
  const src = s.repeat_of === null ? s : byIndex.get(s.repeat_of);
  if (!src || src.repeat_of !== null) return null;
  if (src.lines.length === 0) return null; // a bare heading carries nothing to save
  return JSON.stringify([labelKey(src), src.lines.map(lineKey)]);
}

/** The body with repeats collapsed: a part with the same label and the same content as one
 *  written out above (or a `{chorus}` reference to it) becomes a single line; back-to-back
 *  plays of one part merge into one block with a count. Different labels never merge, even
 *  with equal content ("Chorus 1" / "Chorus 2" stay as the source named them). */
export function collapseRepeats(sections: SheetSection[]): BodyItem[] {
  const byIndex = new Map(sections.map((s) => [s.index, s]));
  const seen = new Set<string>();
  const out = sections.reduce<(BodyItem & { id: string | null })[]>((acc, s) => {
    const id = identity(s, byIndex);
    const last = acc[acc.length - 1];
    if (id !== null && last && last.id === id) {
      last.times += 1;
      last.indices.push(s.index);
      return acc;
    }
    const again = id !== null && seen.has(id);
    if (id !== null && s.repeat_of === null) seen.add(id);
    acc.push({ section: s, again, times: 1, indices: [s.index], id });
    return acc;
  }, []);
  return out.map(({ id: _id, ...item }) => item);
}
