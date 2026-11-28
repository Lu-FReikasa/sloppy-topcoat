import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useTranslation } from "react-i18next";
import type { ApiClient } from "../api/client";
import { sheetsHref } from "../route";
import { canReorder, moveItem, resolveSetlist, SETLIST_VIEWS, viewOrder, type SetlistView } from "./setlist";
import type { Setlist, SetlistItem, SheetSummary } from "./types";

const SETLIST_KEY = "topcoat.sheets.setlist";
// the view chosen per setlist (setlist order, by title, by artist): { [setlist id]: view }
const VIEW_KEY = "topcoat.sheets.setlistView";

function views(): Record<string, SetlistView> {
  try {
    const v: unknown = JSON.parse(window.localStorage.getItem(VIEW_KEY) ?? "{}");
    return v && typeof v === "object" ? (v as Record<string, SetlistView>) : {};
  } catch {
    return {};
  }
}

function rememberedView(id: string): SetlistView {
  const v = views()[id];
  return v && SETLIST_VIEWS.includes(v) ? v : "setlist";
}

function rememberView(id: string, view: SetlistView) {
  try {
    const all = views();
    if (view === "setlist") delete all[id];
    else all[id] = view;
    window.localStorage.setItem(VIEW_KEY, JSON.stringify(all));
  } catch {
    /* not remembered; the view still holds for this page */
  }
}

function rememberedSetlist(): string {
  try {
    return window.localStorage.getItem(SETLIST_KEY) ?? "";
  } catch {
    return "";
  }
}

function rememberSetlist(id: string) {
  try {
    window.localStorage.setItem(SETLIST_KEY, id);
  } catch {
    /* not remembered; the choice still holds for this page */
  }
}

interface Props {
  api: ApiClient;
  list: SheetSummary[];
  /** The open sheet, which "add" puts into the setlist. */
  openId?: string;
  /** A song of the setlist is opened in the normal view (the setlist then stays beside it). */
  onOpen: () => void;
  /** Play the setlist on the stage, from the item at this index. */
  onPlay: (setlist: Setlist, from: number) => void;
  /** Print the setlist or save it as a PDF, in its order. */
  onPrint: (setlist: Setlist) => void;
  onError: (e: unknown) => void;
}

/** The person's setlists: pick one, create, rename, delete; add the open song (each item plays
 *  the version in use of its song, #159), reorder by drag or Alt+↑/↓, open a song (the setlist then stays
 *  beside it), play it on the stage from the start or from a song, print it. Every change
 *  is saved at once; a refused one reloads the setlists from the hub. A setlist can also be looked
 *  at sorted by title or artist and searched (#149): only the view changes, each row keeps its
 *  position number, reordering works only in setlist order, and the stage and print go in setlist order. */
export function Setlists({ api, list, openId, onOpen, onPlay, onPrint, onError }: Props) {
  const { t } = useTranslation();
  const [setlists, setSetlists] = useState<Setlist[] | null>(null);
  const [selected, setSelectedState] = useState(rememberedSetlist);
  const [creating, setCreating] = useState(false);
  const [loads, setLoads] = useState(0);
  const reload = useCallback(() => setLoads((n) => n + 1), []);
  const setSelected = (id: string) => {
    setSelectedState(id);
    rememberSetlist(id);
  };

  useEffect(() => {
    const load = async () => {
      try {
        setSetlists((await api.setlists()).setlists);
      } catch {
        setSetlists([]);
      }
    };
    void load();
  }, [api, loads]);

  const current = setlists?.find((s) => s.id === selected) ?? null;
  const replace = (s: Setlist) => setSetlists((all) => (all ?? []).map((x) => (x.id === s.id ? s : x)));
  const failed = (e: unknown) => {
    onError(e);
    reload();
  };

  // changes go to the hub one after the other: each one sends the whole order, so two quick moves
  // must not overtake each other on the way
  const sending = useRef<Promise<void>>(Promise.resolve());
  const change = (s: Setlist, patch: { name?: string; items?: SetlistItem[] }) => {
    replace({ ...s, ...patch });
    const before = sending.current;
    const send = async (): Promise<void> => {
      await before;
      try {
        await api.setlistUpdate(s.id, patch);
      } catch (e) {
        onError(e);
        void reloadWhenSent();
      }
    };
    sending.current = send();
  };
  // after a refused change, reload once the changes queued behind it have landed, or the hub's
  // answer would show an order that a later change is still about to replace
  const reloadWhenSent = async (): Promise<void> => {
    const last = sending.current;
    await last;
    if (last === sending.current) reload();
    else await reloadWhenSent();
  };

  const remove = async (s: Setlist) => {
    if (!window.confirm(t("sheets.setlists.deleteConfirm", { name: s.name }))) return;
    try {
      await api.setlistDelete(s.id);
      setSetlists((all) => (all ?? []).filter((x) => x.id !== s.id));
      setSelected("");
    } catch (e) {
      failed(e);
    }
  };

  const create = async (name: string) => {
    try {
      const s = await api.setlistCreate({ name });
      setSetlists((all) => [s, ...(all ?? [])]);
      setSelected(s.id);
      setCreating(false);
    } catch (e) {
      onError(e);
    }
  };

  return (
    <section className="sheets-setlists" aria-label={t("sheets.setlists.title")} data-testid="setlists">
      <h2>{t("sheets.setlists.title")}</h2>
      <div className="sheets-row">
        <select value={current ? current.id : ""} onChange={(e) => setSelected(e.target.value)} aria-label={t("sheets.setlists.choose")}>
          <option value="">{t("sheets.setlists.choose")}</option>
          {(setlists ?? []).map((s) => (
            <option key={s.id} value={s.id}>
              {s.name}
            </option>
          ))}
        </select>
        {!creating && (
          <button type="button" onClick={() => setCreating(true)}>
            {t("sheets.setlists.new")}
          </button>
        )}
      </div>
      {creating && (
        <NewSetlist
          onCancel={() => setCreating(false)}
          onCreate={(name) => void create(name)}
        />
      )}
      {current && (
        <SetlistEditor
          key={current.id}
          setlist={current}
          list={list}
          openId={openId}
          onChange={(patch) => change(current, patch)}
          onDelete={() => void remove(current)}
          onOpen={onOpen}
          onPlay={(from) => onPlay(current, from)}
          onPrint={() => onPrint(current)}
        />
      )}
    </section>
  );
}

function NewSetlist({ onCreate, onCancel }: { onCreate: (name: string) => void; onCancel: () => void }) {
  const { t } = useTranslation();
  const [name, setName] = useState("");
  const submit = (e: React.FormEvent) => {
    e.preventDefault();
    if (name.trim()) onCreate(name.trim());
  };
  return (
    <form className="sheets-form" onSubmit={submit} aria-label={t("sheets.setlists.new")}>
      <input value={name} onChange={(e) => setName(e.target.value)} placeholder={t("sheets.setlists.name")} aria-label={t("sheets.setlists.name")} maxLength={200} autoFocus />
      <div className="sheets-row">
        <button type="submit" disabled={!name.trim()}>
          {t("sheets.setlists.create")}
        </button>
        <button type="button" className="link-button" onClick={onCancel}>
          {t("sheets.cancel")}
        </button>
      </div>
    </form>
  );
}

function SetlistEditor({
  setlist,
  list,
  openId,
  onChange,
  onDelete,
  onOpen,
  onPlay,
  onPrint,
}: {
  setlist: Setlist;
  list: SheetSummary[];
  openId?: string;
  onChange: (patch: { name?: string; items?: SetlistItem[] }) => void;
  onDelete: () => void;
  onOpen: () => void;
  onPlay: (from: number) => void;
  onPrint: () => void;
}) {
  const { t } = useTranslation();
  const [name, setName] = useState(setlist.name);
  const [dragFrom, setDragFrom] = useState<number | null>(null);
  const focusAt = useRef<number | null>(null);
  const rows = useRef<(HTMLLIElement | null)[]>([]);
  const entries = useMemo(() => resolveSetlist(setlist.items, list), [setlist.items, list]);
  const items = setlist.items;
  const [view, setViewState] = useState<SetlistView>(() => rememberedView(setlist.id));
  const setView = (v: SetlistView) => {
    setViewState(v);
    rememberView(setlist.id, v);
  };
  const [query, setQuery] = useState("");
  const shown = useMemo(() => viewOrder(entries, view, query), [entries, view, query]);
  const reorder = canReorder(view, query);

  // a row moved by keys keeps the focus, so it can be moved again
  useEffect(() => {
    if (focusAt.current === null) return;
    rows.current[focusAt.current]?.focus();
    focusAt.current = null;
  }, [items]);

  const rename = () => {
    const n = name.trim();
    if (n && n !== setlist.name) onChange({ name: n });
    else setName(setlist.name);
  };
  const move = (from: number, to: number, keepFocus = false) => {
    if (!reorder) return;
    const next = moveItem(items, from, to);
    if (next === items) return;
    onChange({ items: next });
    if (keepFocus) focusAt.current = Math.max(0, Math.min(items.length - 1, to));
  };
  const drop = (i: number) => items.filter((_, k) => k !== i);

  const open = openId ? list.find((s) => s.id === openId) : undefined;
  const playable = entries.some((e) => e.sheet);

  return (
    <div className="setlist">
      <div className="sheets-row">
        <input
          value={name}
          onChange={(e) => setName(e.target.value)}
          onBlur={rename}
          onKeyDown={(e) => {
            if (e.key === "Enter") rename();
          }}
          aria-label={t("sheets.setlists.name")}
          maxLength={200}
        />
      </div>
      <div className="sheets-row">
        <button type="button" onClick={() => onPlay(0)} disabled={!playable} data-testid="setlist-play">
          {t("sheets.setlists.play")}
        </button>
        <button type="button" onClick={onPrint} disabled={!playable} data-testid="setlist-print">
          {t("sheets.setlists.print")}
        </button>
        <button type="button" className="link-button small" onClick={onDelete}>
          {t("sheets.setlists.delete")}
        </button>
      </div>
      {items.length === 0 ? (
        <p className="muted small">{t("sheets.setlists.empty")}</p>
      ) : (
        <>
          <div className="sheets-row setlist-view">
            <input
              type="search"
              value={query}
              onChange={(e) => setQuery(e.target.value)}
              placeholder={t("sheets.setlists.search")}
              aria-label={t("sheets.setlists.search")}
              data-testid="setlist-search"
            />
            <select value={view} onChange={(e) => setView(e.target.value as SetlistView)} aria-label={t("sheets.setlists.view")} data-testid="setlist-view">
              {SETLIST_VIEWS.map((v) => (
                <option key={v} value={v}>
                  {t(`sheets.setlists.views.${v}`)}
                </option>
              ))}
            </select>
          </div>
          <p className="muted small">{reorder ? t("sheets.setlists.keys") : t("sheets.setlists.sortedHint")}</p>
          {shown.length === 0 && <p className="muted small">{t("sheets.setlists.noMatch")}</p>}
          <ol className="setlist-items" data-testid="setlist-items">
            {shown.map((i) => {
              const e = entries[i]!;
              return (
              <li
                key={`${i}-${e.item.sheet_id}`}
                ref={(el) => {
                  rows.current[i] = el;
                }}
                className={`setlist-item${dragFrom === i ? " is-dragged" : ""}${reorder ? "" : " is-fixed"}`}
                tabIndex={0}
                draggable={reorder}
                aria-label={e.sheet ? e.sheet.title : t("sheets.setlists.missing")}
                onDragStart={(ev) => {
                  if (!reorder) return;
                  setDragFrom(i);
                  ev.dataTransfer.setData("text/plain", String(i));
                }}
                onDragOver={(ev) => ev.preventDefault()}
                onDrop={(ev) => {
                  ev.preventDefault();
                  const from = dragFrom ?? Number(ev.dataTransfer.getData("text/plain"));
                  setDragFrom(null);
                  if (Number.isInteger(from)) move(from, i);
                }}
                onDragEnd={() => setDragFrom(null)}
                onKeyDown={(ev) => {
                  if (ev.target !== ev.currentTarget || !ev.altKey) return;
                  if (ev.key === "ArrowUp") move(i, i - 1, true);
                  else if (ev.key === "ArrowDown") move(i, i + 1, true);
                  else return;
                  ev.preventDefault();
                }}
              >
                <span className="setlist-n">{i + 1}</span>
                <span className="setlist-song">
                  {e.sheet ? (
                    <a className="setlist-title" href={sheetsHref(e.sheet.id)} onClick={onOpen} aria-current={e.sheet.id === openId ? "page" : undefined}>
                      {e.sheet.title}
                    </a>
                  ) : (
                    <span className="muted">{t("sheets.setlists.missing")}</span>
                  )}
                  {view === "artist" && e.sheet?.artist && <span className="muted small setlist-artist">{e.sheet.artist}</span>}
                </span>
                <span className="setlist-tools">
                  <button type="button" onClick={() => onPlay(i)} disabled={!e.sheet} aria-label={t("sheets.setlists.playFrom")} title={t("sheets.setlists.playFrom")}>
                    ▶
                  </button>
                  {reorder && (
                    <>
                      <button type="button" onClick={() => move(i, i - 1)} disabled={i === 0} className="setlist-edit" aria-label={t("sheets.setlists.up")} title={t("sheets.setlists.up")}>
                        ↑
                      </button>
                      <button type="button" onClick={() => move(i, i + 1)} disabled={i === items.length - 1} className="setlist-edit" aria-label={t("sheets.setlists.down")} title={t("sheets.setlists.down")}>
                        ↓
                      </button>
                    </>
                  )}
                  <button type="button" onClick={() => onChange({ items: drop(i) })} className="setlist-edit" aria-label={t("sheets.setlists.remove")} title={t("sheets.setlists.remove")}>
                    ×
                  </button>
                </span>
              </li>
              );
            })}
          </ol>
        </>
      )}
      {open ? (
        <div className="sheets-form setlist-add">
          {/* newest: true keeps the stored item honest for a hub or an older page that still reads it */}
          <button type="button" onClick={() => onChange({ items: [...items, { sheet_id: open.id, newest: true }] })} data-testid="setlist-add">
            {t("sheets.setlists.add", { title: open.title })}
          </button>
        </div>
      ) : (
        <p className="muted small">{t("sheets.setlists.addHint")}</p>
      )}
    </div>
  );
}
