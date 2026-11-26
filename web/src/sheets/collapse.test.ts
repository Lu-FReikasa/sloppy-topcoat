import { describe, expect, it } from "vitest";
import { asWritten, collapseRepeats } from "./collapse";
import type { SheetLine, SheetSection } from "./types";

// synthetic sheets only: made-up words, no real lyrics
const line = (chord: string, text: string): SheetLine => ({ segments: [{ chord, text }] });
const LA = [line("C", "la la la"), line("G", "lo lo")];
const LB = [line("Am", "na na"), line("F", "ne ne ne")];

function part(kind: string, label: string, lines: SheetLine[], repeat_of: number | null = null): Omit<SheetSection, "index"> {
  return { kind, label, repeat_of, lines };
}
/** A part index no sheet here has (a part hidden as tab). */
const GONE = 99;
function sheet(...parts: Omit<SheetSection, "index">[]): SheetSection[] {
  return parts.map((p, index) => ({ ...p, index }));
}

/** The body as text: "Chorus" written out, "Chorus ×2 again" for a collapsed line. */
const shape = (sections: SheetSection[]) =>
  collapseRepeats(sections).map((b) => `${b.section.label}${b.times > 1 ? ` ×${b.times}` : ""}${b.again ? " again" : ""}`);

describe("collapseRepeats", () => {
  it("writes a chorus written out in full again only the first time", () => {
    const s = sheet(part("verse", "Verse 1", LB), part("chorus", "Chorus", LA), part("verse", "Verse 2", LB), part("chorus", "Chorus", LA));
    expect(shape(s)).toEqual(["Verse 1", "Chorus", "Verse 2", "Chorus again"]);
    expect(collapseRepeats(s).at(-1)?.indices).toEqual([s.at(-1)?.index]);
  });

  it("follows a ChordPro {chorus} reference to the part it repeats", () => {
    const s = sheet(part("chorus", "Chorus", LA), part("verse", "Verse", LB), part("chorus", "Chorus", [], 0));
    expect(shape(s)).toEqual(["Chorus", "Verse", "Chorus again"]);
  });

  it("merges back-to-back plays into one block with a count", () => {
    const s = sheet(
      part("verse", "Verse", LB),
      part("chorus", "Chorus", LA),
      part("bridge", "Bridge", LB),
      part("chorus", "Chorus", LA),
      part("chorus", "Chorus", [], 1),
    );
    const out = collapseRepeats(s);
    expect(shape(s)).toEqual(["Verse", "Chorus", "Bridge", "Chorus ×2 again"]);
    expect(out.at(-1)?.indices).toEqual(s.slice(-2).map((x) => x.index));
  });

  it("counts a repeat right after the first play on the written part", () => {
    const s = sheet(part("chorus", "Chorus", LA), part("chorus", "Chorus", [], 0), part("outro", "Outro", LB));
    expect(shape(s)).toEqual(["Chorus ×2", "Outro"]);
  });

  it("needs the same label and the same content", () => {
    const changed = [line("C", "la la la"), line("G", "lo lo lo")];
    const s = sheet(
      part("chorus", "Chorus", LA),
      part("chorus", "Chorus", changed), // other words: written out
      part("chorus", "Chorus 2", LA), // other label: written out
      part("verse", "Chorus", LA), // other kind: written out
      part("chorus", " chorus ", [line("C", "la  la la"), line("G", "lo lo")]), // spacing and case only
    );
    expect(shape(s)).toEqual(["Chorus", "Chorus", "Chorus 2", "Chorus", " chorus  again"]);
  });

  it("keeps a reference whose part is gone and bare headings as they are", () => {
    // the referenced part was a tab part and is hidden: nothing to point back to
    const s = sheet(part("solo", "Solo", []), part("chorus", "Chorus", [], GONE), part("solo", "Solo", []));
    expect(shape(s)).toEqual(["Solo", "Chorus", "Solo"]);
  });

  it("leaves everything as written when off", () => {
    const s = sheet(part("chorus", "Chorus", LA), part("chorus", "Chorus", LA));
    expect(asWritten(s).map((b) => [b.section.index, b.again, b.times])).toEqual([
      [0, false, 1],
      [1, false, 1],
    ]);
  });
});
