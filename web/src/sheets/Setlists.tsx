import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useTranslation } from "react-i18next";
import type { ApiClient } from "../api/client";
import { groupSheets } from "./group";
import { moveItem, resolveSetlist } from "./setlist";
import type { Setlist, SetlistItem, SheetSummary } from "./types";

const SETLIST_KEY = "topcoat.sheets.setlist";

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
  /** Play the setlist on the stage, from the item at this index. */
  onPlay: (setlist: Setlist, from: number) => void;
  onError: (e: unknown) => void;
}

/** The person's setlists: pick one, create, rename, delete; add the open sheet (that version or
 *  always the newest of its song), reorder by drag or Alt+↑/↓, play it on the stage. Every change
 *  is saved at once; a refused one reloads the setlists from the hub. */
export function Setlists({ api, list, openId, onPlay, onError }: Props) {
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
          onPlay={(from) => onPlay(current, from)}
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
  onPlay,
}: {
  setlist: Setlist;
  list: SheetSummary[];
  openId?: string;
  onChange: (patch: { name?: string; items?: SetlistItem[] }) => void;
  onDelete: () => void;
  onPlay: (from: number) => void;
}) {
  const { t } = useTranslation();
  const [name, setName] = useState(setlist.name);
  const [newest, setNewest] = useState(true);
  const [dragFrom, setDragFrom] = useState<number | null>(null);
  const focusAt = useRef<number | null>(null);
  const rows = useRef<(HTMLLIElement | null)[]>([]);
  const entries = useMemo(() => resolveSetlist(setlist.items, list), [setlist.items, list]);
  const items = setlist.items;

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
    const next = moveItem(items, from, to);
    if (next === items) return;
    onChange({ items: next });
    if (keepFocus) focusAt.current = Math.max(0, Math.min(items.length - 1, to));
  };
  const drop = (i: number) => items.filter((_, k) => k !== i);

  const open = openId ? list.find((s) => s.id === openId) : undefined;
  const openVersion = useMemo(() => {
    if (!open) return 0;
    const g = groupSheets(list).find((x) => x.versions.some((s) => s.id === open.id));
    return g ? g.versions.findIndex((s) => s.id === open.id) + 1 : 0;
  }, [open, list]);
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
        <button type="button" className="link-button small" onClick={onDelete}>
          {t("sheets.setlists.delete")}
        </button>
      </div>
      {items.length === 0 ? (
        <p className="muted small">{t("sheets.setlists.empty")}</p>
      ) : (
        <>
          <p className="muted small">{t("sheets.setlists.keys")}</p>
          <ol className="setlist-items" data-testid="setlist-items">
            {entries.map((e, i) => (
              <li
                key={`${i}-${e.item.sheet_id}`}
                ref={(el) => {
                  rows.current[i] = el;
                }}
                className={`setlist-item${dragFrom === i ? " is-dragged" : ""}`}
                tabIndex={0}
                draggable
                aria-label={e.sheet ? e.sheet.title : t("sheets.setlists.missing")}
                onDragStart={(ev) => {
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
                    <button type="button" className="setlist-title" onClick={() => onPlay(i)} title={t("sheets.setlists.playFrom")}>
                      {e.sheet.title}
                    </button>
                  ) : (
                    <span className="muted">{t("sheets.setlists.missing")}</span>
                  )}
                  {e.sheet && (e.item.newest || e.versions > 1) && (
                    <span className="muted small">
                      {e.item.newest ? t("sheets.setlists.newest", { n: e.version }) : t("sheets.version", { n: e.version })}
                    </span>
                  )}
                </span>
                <span className="setlist-tools">
                  <button type="button" onClick={() => move(i, i - 1)} disabled={i === 0} aria-label={t("sheets.setlists.up")} title={t("sheets.setlists.up")}>
                    ↑
                  </button>
                  <button type="button" onClick={() => move(i, i + 1)} disabled={i === items.length - 1} aria-label={t("sheets.setlists.down")} title={t("sheets.setlists.down")}>
                    ↓
                  </button>
                  <button type="button" onClick={() => onChange({ items: drop(i) })} aria-label={t("sheets.setlists.remove")} title={t("sheets.setlists.remove")}>
                    ×
                  </button>
                </span>
              </li>
            ))}
          </ol>
        </>
      )}
      {open ? (
        <div className="sheets-form setlist-add">
          <select value={newest ? "newest" : "this"} onChange={(e) => setNewest(e.target.value === "newest")} aria-label={t("sheets.setlists.which")}>
            <option value="newest">{t("sheets.setlists.newestOption")}</option>
            <option value="this">{t("sheets.setlists.thisOption", { n: openVersion })}</option>
          </select>
          <button type="button" onClick={() => onChange({ items: [...items, { sheet_id: open.id, newest }] })} data-testid="setlist-add">
            {t("sheets.setlists.add", { title: open.title })}
          </button>
        </div>
      ) : (
        <p className="muted small">{t("sheets.setlists.addHint")}</p>
      )}
    </div>
  );
}
