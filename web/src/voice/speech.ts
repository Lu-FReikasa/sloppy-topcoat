/**
 * Which part of an answer can be read out yet: the answer is cut into sentences while the chat
 * still writes it, so the first sentence is spoken before the last one exists (D127, #60).
 * The hub takes the Markdown marks out of each sentence; here only code blocks are held back,
 * because a sentence cut inside one would be read out as code.
 */

const FENCE = "```";
/** A sentence ends with one of these and a space or line break after it, or at a line break:
 *  matches the "." in "Ja. Nein" and the "\n" in "Liste\nPunkt"; not the "." in "3.5". */
const SENTENCE_END = /[.!?…:;](?=\s)|\n/g;
/** A piece worth saying has a letter or a digit: matches "Ja" and "42"; rejects "—" and "**". */
const WORDY = /\p{L}|\p{N}/u;
/** Pieces shorter than this go together with the next one ("1." of a list, "Ok."). */
const MIN_PIECE = 12;

/** The answer without its code blocks: the parts between fences are prose (even places), the
 *  ones inside are code (odd places), a block still being written included. */
export function readable(text: string): string {
  return text
    .split(FENCE)
    .filter((_, i) => i % 2 === 0)
    .join("\n");
}

/** Cuts a growing answer into the sentences that are complete; each one is handed out once. */
export class SentenceCutter {
  private taken = 0;

  /** The new complete sentences of `text` (the whole answer so far); with `final` the rest too. */
  take(text: string, final = false): string[] {
    const body = readable(text);
    if (body.length < this.taken) return []; // the answer was replaced: nothing is said twice
    const fresh = body.slice(this.taken);
    const ends = [...fresh.matchAll(SENTENCE_END)].map((m) => (m.index ?? 0) + m[0].length);
    const cut = final ? fresh.length : (ends.filter((e) => e >= MIN_PIECE).at(-1) ?? 0);
    if (cut === 0) return [];
    this.taken += cut;
    const ready = fresh.slice(0, cut);
    const said = [0, ...ends.filter((e) => e < cut), cut]
      .slice(0, -1)
      .map((start, i, all) => ready.slice(start, all[i + 1] ?? cut))
      .map((s) => s.trim())
      .filter((s) => WORDY.test(s));
    return said.reduce<string[]>((acc, s) => {
      const last = acc.at(-1);
      return last !== undefined && last.length < MIN_PIECE
        ? [...acc.slice(0, -1), `${last} ${s}`]
        : [...acc, s];
    }, []);
  }
}
