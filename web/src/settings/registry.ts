// Every option of Settings, as data (the desktop plan, phase 4): the section it is in, its name and
// the words it is found by, in German and English. The search of Settings and the command palette
// (Ctrl+K) both read this list, so "temperatur" finds the same place in either.

export const SECTIONS = ["general", "client", "assistant", "apps", "memory", "household"] as const;
export type SectionKey = (typeof SECTIONS)[number];

export function isSection(v: unknown): v is SectionKey {
  return typeof v === "string" && (SECTIONS as readonly string[]).includes(v);
}

export interface SettingEntry {
  /** Stable: it is the `data-setting` anchor on the page and the `focus` prop of a jump. */
  id: string;
  section: SectionKey;
  /** Further words it is found by (both languages, lower case); the label is searched too. */
  words: string;
}

export const SETTINGS: readonly SettingEntry[] = [
  { id: "language", section: "general", words: "sprache language deutsch english englisch" },
  { id: "look", section: "general", words: "optik theme aussehen look design farben colours colors" },
  { id: "hue", section: "general", words: "farbton hue farbe colour color akzent accent" },
  { id: "shell", section: "general", words: "oberfläche navigation fenster desktop windows browser ansicht layout" },
  { id: "chatlist", section: "general", words: "chatliste liste unterhaltungen chat list kompakt karten compact cards" },
  { id: "client-keys", section: "client", words: "taste tasten hotkey tastenkürzel key shortcut altgr clipper overlay" },
  { id: "client-look", section: "client", words: "blur unschärfe abgedunkelt bildschirm bildschirme screen screens backdrop" },
  { id: "client-tile", section: "client", words: "kacheln tiling snapping fenster window windows" },
  { id: "assistant-address", section: "assistant", words: "anrede du sie address formal informal" },
  { id: "assistant-quality", section: "assistant", words: "qualität schnell ausgewogen gründlich quality fast balanced thorough tempo" },
  { id: "assistant-internet", section: "assistant", words: "internet web suche search online" },
  { id: "assistant-proposals", section: "assistant", words: "vorschläge proposals wünsche" },
  { id: "assistant-persona", section: "assistant", words: "persona stimme voice butler clipper ton tone fragebogen" },
  { id: "assistant-rules", section: "assistant", words: "regeln stile rules styles style" },
  { id: "assistant-advanced", section: "assistant", words: "erweitert advanced system-prompt systemprompt prompt temperatur temperature" },
  { id: "assistant-core", section: "assistant", words: "kern core vorschau preview zuletzt besprochen recent so sieht die ki" },
  { id: "apps", section: "apps", words: "anwendungen applications rahmen frame werkzeuge tools heikel sensitive gedächtnis memory" },
  { id: "memory-switch", section: "memory", words: "gedächtnis memory an aus on off notizen notes" },
  { id: "memory-notes", section: "memory", words: "gedächtnis memory notizen notes kern core profil profile tags archiv archive rückfragen" },
  { id: "household", section: "household", words: "haushalt household grundlagen basics feste regeln fixed rules" },
];

/** The entries every word of the query is in (label, section name or words): those whose name
 *  holds the words first, then the others, each in their order. */
export function searchSettings(
  query: string,
  t: (key: string) => string,
  entries: readonly SettingEntry[] = SETTINGS,
): SettingEntry[] {
  const words = query.toLowerCase().split(/\s+/).filter(Boolean);
  if (words.length === 0) return [];
  const label = (e: SettingEntry) => t(`settingsApp.item.${e.id}`).toLowerCase();
  const hits = entries.filter((e) => {
    const hay = `${label(e)} ${t(`settingsApp.section.${e.section}`)} ${e.words}`.toLowerCase();
    return words.every((w) => hay.includes(w));
  });
  const byName = (e: SettingEntry) => words.every((w) => label(e).includes(w));
  return [...hits.filter(byName), ...hits.filter((e) => !byName(e))];
}
