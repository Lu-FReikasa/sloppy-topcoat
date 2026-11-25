// A wiki page's Markdown (written by the doc program, hub project_programs.render_document) taken
// apart for reading: its title and the note on how it was made go into the page's header, the
// body keeps its sections, the sources ("Belege") and the removed statements ("Nicht belegt,
// entfernt") are shown apart. A page in another shape keeps its whole text as the body.

export interface Source {
  ref: string;
  text: string;
}

export interface WikiDoc {
  title: string | null;
  /** The italic note "Erzeugt aus dem Code des Hub-Builds ..." without its underscores. */
  made: string | null;
  body: string;
  /** The h2 headings of the body, in order, with the ids the page gives them. */
  headings: { id: string; text: string }[];
  sources: Source[];
  removed: string[];
}

const SOURCES = /^(belege|quellen|sources)$/i;
const REMOVED = /^(nicht belegt.*|removed.*)$/i;

/** The id of a heading: lower case letters, digits and hyphens. */
export function slug(text: string): string {
  return (
    text
      .toLowerCase()
      .replace(/ä/g, "ae")
      .replace(/ö/g, "oe")
      .replace(/ü/g, "ue")
      .replace(/ß/g, "ss")
      .replace(/[^a-z0-9]+/g, "-")
      .replace(/^-+|-+$/g, "") || "section"
  );
}

/** `[E3]` becomes a link to its source (`#cite-E3`); the page draws it as a small mark. */
export function linkCitations(text: string): string {
  return text.replace(/\[(E\d+)\](?!\()/g, "[$1](#cite-$1)");
}

export function parseDoc(markdown: string): WikiDoc {
  const lines = markdown.replace(/\r\n/g, "\n").split("\n");
  let title: string | null = null;
  let made: string | null = null;
  let i = 0;
  while (i < lines.length && lines[i]?.trim() === "") i++;
  const first = lines[i] ?? "";
  if (/^#\s+/.test(first)) {
    title = first.replace(/^#\s+/, "").trim();
    i++;
    while (i < lines.length && lines[i]?.trim() === "") i++;
    const note = (lines[i] ?? "").trim();
    if (/^_.*_$/.test(note)) {
      made = note.slice(1, -1).trim();
      i++;
    }
  }
  const body: string[] = [];
  const sources: Source[] = [];
  const removed: string[] = [];
  let part: "body" | "sources" | "removed" = "body";
  for (const line of lines.slice(i)) {
    const h2 = /^##\s+(.+?)\s*$/.exec(line);
    if (h2?.[1]) {
      if (SOURCES.test(h2[1])) {
        part = "sources";
        continue;
      }
      if (REMOVED.test(h2[1])) {
        part = "removed";
        continue;
      }
      part = "body";
    }
    if (part === "body") {
      body.push(line);
    } else if (/^\s*-\s+/.test(line)) {
      const item = line.replace(/^\s*-\s+/, "").trim();
      if (part === "removed") {
        removed.push(item);
      } else {
        const m = /^\*\*(E\d+)\*\*\s*(.*)$/.exec(item);
        if (m?.[1]) sources.push({ ref: m[1], text: m[2] ?? "" });
        else sources.push({ ref: "", text: item });
      }
    }
  }
  const text = body.join("\n").trim();
  const headings = [...text.matchAll(/^##\s+(.+?)\s*$/gm)].map((m) => ({
    id: slug(m[1] ?? ""),
    text: m[1] ?? "",
  }));
  return { title, made, body: text, headings, sources, removed };
}
