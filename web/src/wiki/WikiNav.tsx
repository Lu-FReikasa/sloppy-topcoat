import { useMemo, useState, type DragEvent } from "react";
import { useTranslation } from "react-i18next";
import { wikiHref } from "../route";
import type { WikiPage, WikiTree } from "../types";
import { buildWikiTree, countPages, pageFilter, type WikiFolder } from "./tree";

const CLOSED_KEY = "topcoat.wiki.closed";
const DRAG_TYPE = "application/x-topcoat-wiki-page";

function loadClosed(): Set<string> {
  try {
    const raw: unknown = JSON.parse(window.localStorage.getItem(CLOSED_KEY) ?? "[]");
    return new Set(Array.isArray(raw) ? raw.filter((x): x is string => typeof x === "string") : []);
  } catch {
    return new Set();
  }
}

function saveClosed(closed: Set<string>): void {
  try {
    window.localStorage.setItem(CLOSED_KEY, JSON.stringify([...closed]));
  } catch {
    /* no storage: the folders open again next time */
  }
}

interface Props {
  tree: WikiTree;
  /** The open page. */
  id?: string;
  /** A page dragged onto a folder. */
  onMove: (page: WikiPage, folder: string) => void;
  /** A folder renamed (or moved, with a path). */
  onRenameFolder: (folder: string, next: string) => void;
}

/** The wiki's pages like the file explorer: folders that open and close, pages to drag onto a
 *  folder, a search and the categories as filters above. */
export function WikiNav({ tree, id, onMove, onRenameFolder }: Props) {
  const { t } = useTranslation();
  const [search, setSearch] = useState("");
  const [chosen, setChosen] = useState<string[]>([]);
  const [closed, setClosed] = useState<Set<string>>(loadClosed);
  const [renaming, setRenaming] = useState<string | null>(null);
  const [over, setOver] = useState<string | null>(null);
  const filtering = search.trim() !== "" || chosen.length > 0;
  const root = useMemo(() => buildWikiTree(tree, pageFilter(search, chosen)), [tree, search, chosen]);
  const byId = useMemo(() => new Map(tree.sections.flatMap((s) => s.pages).map((p) => [p.id, p])), [tree]);
  const categories = tree.categories ?? [];

  const toggle = (path: string) =>
    setClosed((c) => {
      const next = new Set(c);
      if (!next.delete(path)) next.add(path);
      saveClosed(next);
      return next;
    });

  const drop = (folder: string) => (e: DragEvent) => {
    e.preventDefault();
    setOver(null);
    const page = byId.get(e.dataTransfer.getData(DRAG_TYPE));
    if (page && (page.folder ?? page.section) !== folder) onMove(page, folder);
  };

  const folderRow = (f: WikiFolder, depth: number) => {
    // a search or a filter shows every folder that holds a match
    const open = filtering || !closed.has(f.path);
    return (
      <li key={f.path} role="treeitem" aria-expanded={open} aria-label={f.name}>
        <div
          className={`wiki-folder${over === f.path ? " wiki-drop" : ""}`}
          style={{ paddingLeft: `${depth * 0.9}rem` }}
          onDragOver={(e) => {
            if (!e.dataTransfer.types.includes(DRAG_TYPE)) return;
            e.preventDefault();
            setOver(f.path);
          }}
          onDragLeave={() => setOver((o) => (o === f.path ? null : o))}
          onDrop={drop(f.path)}
        >
          {renaming === f.path ? (
            <form
              className="wiki-rename"
              onSubmit={(e) => {
                e.preventDefault();
                const next = String(new FormData(e.currentTarget).get("to") ?? "").trim();
                setRenaming(null);
                if (next && next !== f.path) onRenameFolder(f.path, next);
              }}
            >
              <input
                name="to"
                defaultValue={f.path}
                aria-label={t("wiki.renameTo")}
                // Esc ends the renaming here, not the overlay (desktop/client.ts)
                data-esc-first=""
                autoFocus
                onKeyDown={(e) => e.key === "Escape" && setRenaming(null)}
                onBlur={() => setRenaming(null)}
              />
            </form>
          ) : (
            <>
              <button
                type="button"
                className="wiki-folder-toggle"
                onClick={() => toggle(f.path)}
                aria-label={t(open ? "wiki.collapse" : "wiki.expand", { folder: f.name })}
              >
                <span aria-hidden="true">{open ? "▾" : "▸"}</span> <span aria-hidden="true">📁</span> {f.name}
                <span className="wiki-count muted">{countPages(f)}</span>
              </button>
              <button
                type="button"
                className="wiki-folder-edit"
                onClick={() => setRenaming(f.path)}
                aria-label={t("wiki.rename", { folder: f.path })}
                title={t("wiki.rename", { folder: f.path })}
              >
                ✎
              </button>
            </>
          )}
        </div>
        {open && (
          <ul role="group">
            {f.folders.map((c) => folderRow(c, depth + 1))}
            {f.pages.map((p) => (
              <li key={p.id} role="treeitem" aria-label={p.title}>
                <a
                  href={wikiHref(p.id)}
                  aria-current={p.id === id ? "page" : undefined}
                  draggable
                  onDragStart={(e) => {
                    e.dataTransfer.setData(DRAG_TYPE, p.id);
                    e.dataTransfer.effectAllowed = "move";
                  }}
                  style={{ paddingLeft: `${(depth + 1) * 0.9 + 0.5}rem` }}
                >
                  <span aria-hidden="true">📄</span> {p.title}
                </a>
              </li>
            ))}
          </ul>
        )}
      </li>
    );
  };

  return (
    <>
      <input
        type="search"
        className="wiki-search"
        value={search}
        onChange={(e) => setSearch(e.target.value)}
        placeholder={t("wiki.search")}
        aria-label={t("wiki.search")}
      />
      {categories.length > 0 && (
        <div className="wiki-cats" role="group" aria-label={t("wiki.categories")}>
          {categories.map((c) => {
            const on = chosen.includes(c);
            return (
              <button
                key={c}
                type="button"
                className="wiki-cat"
                aria-pressed={on}
                onClick={() => setChosen((all) => (on ? all.filter((x) => x !== c) : [...all, c]))}
              >
                {c}
              </button>
            );
          })}
        </div>
      )}
      {root.folders.length === 0 && filtering ? (
        <p className="muted small">{t("wiki.noMatch")}</p>
      ) : (
        <ul className="wiki-tree" role="tree" aria-label={t("wiki.folders")}>
          {root.folders.map((f) => folderRow(f, 0))}
        </ul>
      )}
      {root.folders.length > 0 && <p className="muted small wiki-hint">{t("wiki.moveHint")}</p>}
    </>
  );
}
