/** The wiki's tree like the file explorer's (files/tree.ts): folders from the pages' folder paths
 *  ("B Hub/Gateway"), pages as leaves, sorted by name; filtered by a search and by categories. */
import type { WikiPage, WikiTree } from "../types";

export interface WikiFolder {
  name: string;
  /** The whole path ("B Hub/Gateway"). */
  path: string;
  folders: WikiFolder[];
  pages: WikiPage[];
}

const byName = (a: string, b: string) => a.localeCompare(b, undefined, { numeric: true, sensitivity: "base" });

export const folderOf = (p: WikiPage): string => p.folder ?? p.section;

/** The tree of the pages that pass `keep`; folders without such a page are left out. */
export function buildWikiTree(tree: WikiTree, keep: (p: WikiPage) => boolean = () => true): WikiFolder {
  const root: WikiFolder = { name: "", path: "", folders: [], pages: [] };
  const at = (path: string): WikiFolder => {
    let node = root;
    for (const name of path.split("/").filter(Boolean)) {
      const full = node.path ? `${node.path}/${name}` : name;
      let next = node.folders.find((f) => f.name === name);
      if (!next) {
        next = { name, path: full, folders: [], pages: [] };
        node.folders.push(next);
      }
      node = next;
    }
    return node;
  };
  for (const s of tree.sections) for (const p of s.pages) if (keep(p)) at(folderOf(p)).pages.push(p);
  const sort = (f: WikiFolder) => {
    f.folders.sort((a, b) => byName(a.name, b.name));
    f.pages.sort((a, b) => byName(a.title, b.title));
    f.folders.forEach(sort);
  };
  sort(root);
  return root;
}

/** A page passes when its title or folder holds every word of the search and it carries every
 *  chosen category. */
export function pageFilter(search: string, categories: readonly string[]): (p: WikiPage) => boolean {
  const words = search.toLowerCase().split(/\s+/).filter(Boolean);
  const wanted = categories.map((c) => c.toLowerCase());
  return (p) => {
    const text = `${p.title} ${folderOf(p)}`.toLowerCase();
    const has = new Set((p.categories ?? []).map((c) => c.toLowerCase()));
    return words.every((w) => text.includes(w)) && wanted.every((c) => has.has(c));
  };
}

/** Every folder path, the parents too ("B Hub", "B Hub/Gateway"): what a page can be moved to. */
export function allFolders(tree: WikiTree): string[] {
  const out = new Set<string>();
  for (const s of tree.sections) {
    const parts = s.name.split("/").filter(Boolean);
    parts.forEach((_, i) => out.add(parts.slice(0, i + 1).join("/")));
  }
  return [...out].sort(byName);
}

/** How many pages lie in a folder and below it. */
export function countPages(f: WikiFolder): number {
  return f.pages.length + f.folders.reduce((n, c) => n + countPages(c), 0);
}
