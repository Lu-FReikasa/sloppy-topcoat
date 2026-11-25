import { useEffect, useMemo, useRef, useState, type KeyboardEvent } from "react";
import { useTranslation } from "react-i18next";

export interface PaletteEntry {
  id: string;
  /** What is shown and searched. */
  label: string;
  /** Where it is (the program of a view), shown small and searched too. */
  hint?: string;
  /** Further words it is found by, not shown (a setting's synonyms in both languages). */
  words?: string;
  /** Listed only once something is typed (the many options of Settings would crowd the rest out). */
  quiet?: boolean;
  run: () => void;
}

/** Every word of the query is in the label or the hint, in any order and case. */
export function matches(entry: PaletteEntry, query: string): boolean {
  const hay = `${entry.label} ${entry.hint ?? ""} ${entry.words ?? ""}`.toLowerCase();
  return query
    .toLowerCase()
    .split(/\s+/)
    .filter(Boolean)
    .every((word) => hay.includes(word));
}

const MAX_SHOWN = 12;

/** The command palette (Ctrl+K, docs/DESKTOP.md): programs, views, settings and actions by name. */
export function CommandPalette({ entries, onClose }: { entries: PaletteEntry[]; onClose: () => void }) {
  const { t } = useTranslation();
  const [query, setQuery] = useState("");
  const [index, setIndex] = useState(0);
  const input = useRef<HTMLInputElement>(null);
  const found = useMemo(
    () => entries.filter((e) => (query.trim() || !e.quiet) && matches(e, query)).slice(0, MAX_SHOWN),
    [entries, query],
  );
  const current = Math.min(index, Math.max(0, found.length - 1));

  useEffect(() => input.current?.focus(), []);

  function run(entry: PaletteEntry | undefined) {
    if (!entry) return;
    onClose();
    entry.run();
  }

  function onKey(e: KeyboardEvent<HTMLInputElement>) {
    if (e.key === "ArrowDown") {
      e.preventDefault();
      setIndex((current + 1) % Math.max(1, found.length));
    } else if (e.key === "ArrowUp") {
      e.preventDefault();
      setIndex((current - 1 + found.length) % Math.max(1, found.length));
    } else if (e.key === "Enter") {
      e.preventDefault();
      run(found[current]);
    } else if (e.key === "Escape") {
      e.preventDefault();
      onClose();
    }
  }

  return (
    <div className="dt-palette-backdrop" onPointerDown={(e) => e.target === e.currentTarget && onClose()}>
      <div className="dt-palette" role="dialog" aria-modal="true" aria-label={t("desktop.palette")}>
        <input
          ref={input}
          type="text"
          value={query}
          role="combobox"
          aria-expanded="true"
          aria-controls="dt-palette-list"
          aria-activedescendant={found[current] ? `dt-pal-${found[current].id}` : undefined}
          aria-label={t("desktop.paletteSearch")}
          placeholder={t("desktop.paletteSearch")}
          onChange={(e) => {
            setQuery(e.target.value);
            setIndex(0);
          }}
          onKeyDown={onKey}
        />
        <ul id="dt-palette-list" role="listbox" aria-label={t("desktop.palette")}>
          {found.map((entry, i) => (
            <li
              key={entry.id}
              id={`dt-pal-${entry.id}`}
              role="option"
              aria-selected={i === current}
              className={i === current ? "dt-pal-current" : undefined}
              onPointerEnter={() => setIndex(i)}
              onClick={() => run(entry)}
            >
              <span>{entry.label}</span>
              {entry.hint && <span className="muted small"> {entry.hint}</span>}
            </li>
          ))}
          {found.length === 0 && <li className="muted">{t("desktop.paletteNothing")}</li>}
        </ul>
      </div>
    </div>
  );
}
