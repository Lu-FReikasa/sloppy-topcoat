// The program Sheets (hub: api/sheets.py). A sheet is stored as it sounds; the view the hub returns
// is what one instrument shows (guitar with or without capo shapes, piano without capo).

export type Instrument = "guitar" | "piano";

export interface SheetSummary {
  id: string;
  title: string;
  artist: string;
  instrument: Instrument;
  key: string | null;
  capo: number;
  capo_view: boolean;
  version_note: string;
  sources: number;
  report_id: string | null;
  updated_at: string | null;
  created_at?: string | null;
}

export interface SheetSegment {
  chord: string | null;
  text: string;
}

export interface SheetLine {
  segments?: SheetSegment[];
  comment?: string;
}

export interface SheetSection {
  index: number;
  kind: string;
  label: string;
  repeat_of: number | null;
  lines: SheetLine[];
}

export interface SheetView {
  title: string;
  artist: string;
  instrument: Instrument;
  key: string | null;
  key_estimated: boolean;
  shown_key: string | null;
  capo: number;
  capo_view: boolean;
  transpose: number;
  flow: { index: number; kind: string; label: string; repeat_of: number | null }[];
  sections: SheetSection[];
  chordpro: string;
}

export interface SheetSource {
  url: string | null;
  title: string | null;
  version: string | null;
  note: string | null;
}

export interface SheetDetail extends SheetSummary {
  view: SheetView;
  source_list: SheetSource[];
}

export interface SheetDifference {
  kind: string;
  where: string;
  a: string;
  b: string;
  note: string;
}

export interface SheetComparison {
  against: { source?: number; url?: string | null; sheet?: string };
  key_a: string | null;
  key_b: string | null;
  shift: number;
  capo_a: number;
  capo_b: number;
  lyrics_similarity: number;
  chords_similarity: number;
  differences: SheetDifference[];
  counts: Record<string, number>;
}

export interface SheetResearchJob {
  id: string;
  profile: string;
  song: string;
  artist: string;
  instrument: Instrument;
  wish: string;
  status: "running" | "done" | "failed";
  steps: string[];
  sheet_id: string | null;
  report_id: string | null;
  error: string;
  started: string;
  finished: string | null;
}

export interface SheetViewParams {
  instrument?: Instrument;
  capo_view?: boolean;
  transpose?: number;
}

export interface SheetPatch {
  instrument?: Instrument;
  capo_view?: boolean;
  capo?: number;
  transpose?: number;
  title?: string;
  artist?: string;
}

/** A song of a setlist, named by one of its sheets. It plays the version in use of that song
 *  (#159); `newest` is kept from the time an item could pin a version and is no longer read. */
export interface SetlistItem {
  sheet_id: string;
  newest: boolean;
}

/** The order of songs for one evening (rehearsal or gig). */
export interface Setlist {
  id: string;
  name: string;
  items: SetlistItem[];
  updated_at: string | null;
  created_at: string | null;
}
