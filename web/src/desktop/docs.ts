// A document of the Ablage in a window of its own (2026-11-13): a report or an artifact opens as a
// window like a program's, so it can be moved, pinned as a native window, have Clipper's talk at
// its side and stand next to other documents. The desktop knows it as a program with a key of its
// own (`r-<id>` for a research report, `a-<id>` for an artifact) that exists only while it is open.
import type { Workspace } from "../layout/types";
import type { DesktopState, ProgramView } from "./state";

/** The view a document window shows. */
export const DOC_MODULE = "page.document";

export type DocKind = "report" | "artifact";

const PREFIX: Record<DocKind, string> = { report: "r-", artifact: "a-" };
const ID = /^[0-9a-f-]{8,36}$/;

/** The key of the window that shows a document, or null for an id that cannot be one. */
export function docKey(kind: DocKind, id: string): string | null {
  return ID.test(id) ? `${PREFIX[kind]}${id}` : null;
}

/** The document a window key stands for. */
export function docOf(key: string): { kind: DocKind; id: string } | null {
  for (const kind of ["report", "artifact"] as const) {
    const p = PREFIX[kind];
    if (key.startsWith(p) && ID.test(key.slice(p.length))) return { kind, id: key.slice(p.length) };
  }
  return null;
}

export const isDocKey = (key: string) => docOf(key) !== null;

/** The view of a document window: which document, and the title it is listed by. */
export function docView(kind: DocKind, id: string, title?: string): ProgramView {
  return { module: DOC_MODULE, props: title ? { kind, id, title } : { kind, id } };
}

/** The address of the Ablage that names a document (`#/library/report:<id>`, `#/library/<id>`),
 *  as the document; anything else is null. */
export function docOfLibraryId(id: string): { kind: DocKind; id: string } | null {
  if (id.startsWith("report:")) {
    const rid = id.slice("report:".length);
    return ID.test(rid) ? { kind: "report", id: rid } : null;
  }
  return ID.test(id) ? { kind: "artifact", id } : null;
}

/** The program of an open document window: one view, its title from the view (the title is
 *  the one the list showed; the window shows the document's own heading). */
export function docProgram(key: string, view: ProgramView | undefined, fallback: string): Workspace {
  const doc = docOf(key);
  const title = typeof view?.props.title === "string" && view.props.title.trim() ? view.props.title : fallback;
  return {
    key,
    title,
    title_key: null,
    icon: doc?.kind === "report" ? "📑" : "📄",
    position: 1000,
    modules: [DOC_MODULE],
    open: [DOC_MODULE],
    agent: { tool_scope: { nodes: null, groups: null, internet: null }, persona: null, model_role: null },
    requires_role: null,
    builtin: false,
    rev: 0,
  };
}

/** The document windows a desktop state holds, with their views. */
export function docPrograms(
  states: (Pick<DesktopState, "open" | "views"> | null | undefined)[],
  extra: (string | null)[],
  fallback: string,
): Workspace[] {
  const keys: string[] = [];
  const views: Record<string, ProgramView> = {};
  for (const s of states) {
    if (!s) continue;
    for (const k of s.open) if (isDocKey(k) && !keys.includes(k)) keys.push(k);
    for (const [k, v] of Object.entries(s.views)) if (isDocKey(k) && !views[k]) views[k] = v;
  }
  for (const k of extra) if (k && isDocKey(k) && !keys.includes(k)) keys.push(k);
  return keys.map((k) => docProgram(k, views[k], fallback));
}
