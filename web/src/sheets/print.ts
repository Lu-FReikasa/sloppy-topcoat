// Printing sheets, one song per page, laid out like the stage mode (columns, the largest font that
// fits). The browser's print dialog saves it as a PDF, which a rehearsal room without Wi-Fi can
// open without the hub (issue #142). Nothing is stored in the browser: the PDF is a file the
// person saves on purpose.

export type Paper = "a4" | "letter";
export type Orientation = "portrait" | "landscape";
export const PAPERS: Paper[] = ["a4", "letter"];
export const ORIENTATIONS: Orientation[] = ["portrait", "landscape"];

const A4_MM: [number, number] = [210, 297];
const LETTER_MM: [number, number] = [215.9, 279.4];

/** Paper sizes in millimetres, portrait. */
const PAPER_MM: Record<Paper, [number, number]> = { a4: A4_MM, letter: LETTER_MM };

/** The page margin in millimetres (the @page rule and the content box agree on it). */
export const MARGIN_MM = 10;

const CSS_PX_PER_INCH = 96;
const MM_PER_INCH = 25.4;
const PX_PER_MM = CSS_PX_PER_INCH / MM_PER_INCH;
/** A margin on both sides of the page. */
const SIDES = 2;
/** The width a printed column wants, and the most columns on a page. */
const COLUMN_PX = 340;
const MOST_COLUMNS = 4;

/** The content box of a page in CSS pixels (96 per inch, the print scale of 100 %). */
export function pageBox(paper: Paper, orientation: Orientation): { width: number; height: number } {
  const [w, h] = PAPER_MM[paper];
  const [pw, ph] = orientation === "portrait" ? [w, h] : [h, w];
  return {
    width: Math.floor((pw - SIDES * MARGIN_MM) * PX_PER_MM),
    height: Math.floor((ph - SIDES * MARGIN_MM) * PX_PER_MM),
  };
}

/** The @page rule for the chosen paper. */
export function pageRule(paper: Paper, orientation: Orientation): string {
  const size = paper === "a4" ? "A4" : "letter";
  return `@page { size: ${size} ${orientation}; margin: ${MARGIN_MM}mm; }`;
}

/** How many columns a page of this width may use. Paper is read closer than a screen, so a column
 *  wants about 340 px (A4 portrait → 2, landscape → 3), at least 1, at most 4. */
export function printColumns(width: number): number {
  return Math.max(1, Math.min(MOST_COLUMNS, Math.floor(width / COLUMN_PX)));
}

/** Moves the item at `index` by `step` places; out of range leaves the list as it is. */
export function move<T>(list: T[], index: number, step: number): T[] {
  const to = index + step;
  if (index < 0 || index >= list.length || to < 0 || to >= list.length) return list;
  const out = [...list];
  out.splice(to, 0, ...out.splice(index, 1));
  return out;
}
