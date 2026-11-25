import { useCallback, useEffect, useState } from "react";
import { useTranslation } from "react-i18next";
import type { ApiClient } from "../api/client";
import { formatDateTime } from "../format";
import { requestOpen } from "../layout/intent";
import type { InboxCounts, InboxItem, InboxView } from "../types";
import { APPLICATIONS, AppTag } from "./AppTag";
import { ItemMarks } from "./ItemMarks";

const POLL_MS = 30_000;

/** The notes of the second look on finished answers: many, alike, and rarely urgent to read one by
 *  one. The inbox bundles them into one line per application (phase 4). */
const REVIEW_KINDS: readonly string[] = ["review_error", "review_hint"];

/** The list as it is shown: an item, or the bundle of an application's second-look notes, in the
 *  place of its first item. */
type Row = { item: InboxItem } | { bundle: string; items: InboxItem[] };

export function rowsOf(items: readonly InboxItem[]): Row[] {
  const rows: Row[] = [];
  const bundles = new Map<string, InboxItem[]>();
  for (const item of items) {
    if (!REVIEW_KINDS.includes(item.kind)) {
      rows.push({ item });
      continue;
    }
    const list = bundles.get(item.application);
    if (list) list.push(item);
    else {
      const fresh = [item];
      bundles.set(item.application, fresh);
      rows.push({ bundle: item.application, items: fresh });
    }
  }
  // a bundle of one is just the item
  return rows.map((r) => ("bundle" in r && r.items.length === 1 ? { item: r.items[0]! } : r));
}

/** What an item is called: the hub's titles of the second look are fixed English words, the kind
 *  says the same in the person's language. */
function titleOf(item: InboxItem, t: (k: string, o?: Record<string, unknown>) => string): string {
  return REVIEW_KINDS.includes(item.kind) ? t(`inbox.kind.${item.kind}`, { defaultValue: item.title }) : item.title;
}

/** Show the thing an item is about: the window that owns it. */
export function showItem(item: InboxItem): void {
  const module = item.ref.module;
  if (module) requestOpen({ module, props: item.ref.props ?? {} });
}

interface Props {
  api: ApiClient;
  /** Bumped when the hub says the inbox changed. */
  tick: number;
}

/** What happened and wants to be read (docs/PLATFORM.md, 3.7). A click opens an item and marks it
 *  read: it leaves the list, and when it is closed it is filed in the corner of its application,
 *  unless it is pinned to the board. An urgent item stays in the list until its matter is settled. */
export function InboxPage({ api, tick }: Props) {
  const { t, i18n } = useTranslation();
  const [tab, setTab] =
    useState<Extract<InboxView, "inbox" | "filed">>("inbox");
  const [application, setApplication] = useState<string | null>(null);
  const [items, setItems] = useState<InboxItem[]>([]);
  const [counts, setCounts] = useState<InboxCounts | null>(null);
  const [reading, setReading] = useState<InboxItem | null>(null);
  const [failed, setFailed] = useState(false);
  // the bundles that are open
  const [openBundles, setOpenBundles] = useState<string[]>([]);
  const [settling, setSettling] = useState<string | null>(null);

  const load = useCallback(() => {
    api.inbox(tab, application ?? undefined).then(
      (r) => {
        setItems(r.items);
        setCounts(r.counts);
        setFailed(false);
      },
      () => setFailed(true),
    );
  }, [api, tab, application]);

  useEffect(() => {
    load();
    const timer = setInterval(load, POLL_MS);
    return () => clearInterval(timer);
  }, [load, tick]);

  async function read(item: InboxItem) {
    try {
      setReading(await api.openInboxItem(item.id));
      load();
    } catch {
      setFailed(true);
    }
  }

  async function change(action: Promise<InboxItem>) {
    try {
      setReading(await action);
      load();
    } catch {
      setFailed(true);
    }
  }

  async function remove(item: InboxItem) {
    try {
      await api.deleteInboxItem(item.id);
      setReading(null);
      load();
    } catch {
      setFailed(true);
    }
  }

  /** Put a whole bundle away: an urgent note is settled, any other read (it is filed). */
  async function settle(key: string, list: InboxItem[]) {
    setSettling(key);
    try {
      for (const item of list) {
        if (item.unresolved) await api.resolveInboxItem(item.id);
        if (!item.read) await api.openInboxItem(item.id);
      }
    } catch {
      setFailed(true);
    } finally {
      setSettling(null);
      load();
    }
  }

  const itemButton = (item: InboxItem) => (
    <button
      type="button"
      className={`inbox-item${item.unresolved ? " is-urgent" : ""}${item.read ? " is-read" : ""}`}
      onClick={() => void read(item)}
    >
      <span className="inbox-dot" aria-hidden="true">
        {item.read ? "○" : "●"}
      </span>
      <AppTag application={item.application} />
      <span className="inbox-title">{titleOf(item, t)}</span>
      {item.unresolved && <span className="chip urgent">{t("inbox.urgent")}</span>}
    </button>
  );

  const unread = (key: string) => counts?.applications[key] ?? 0;
  return (
    <section className="inbox" aria-label={t("inbox.title")}>
      <div className="inbox-tabs" role="tablist">
        {(["inbox", "filed"] as const).map((v) => (
          <button
            key={v}
            type="button"
            role="tab"
            aria-selected={tab === v}
            className={tab === v ? "primary" : undefined}
            onClick={() => {
              setTab(v);
              setReading(null);
            }}
          >
            {v === "inbox" ? t("inbox.tabInbox") : t("inbox.tabFiled")}
            {v === "inbox" && counts && counts.unread > 0 && (
              <span className="nav-badge" data-testid="inbox-unread">
                {counts.unread}
              </span>
            )}
          </button>
        ))}
        {tab === "inbox" && counts && counts.unread > 0 && (
          <button
            type="button"
            title={t("inbox.readAllHint")}
            onClick={() =>
              void api.readAllInbox(application ?? undefined).then(
                () => load(),
                () => setFailed(true),
              )
            }
          >
            {t("inbox.readAll")}
          </button>
        )}
        <span className="spacer" />
        <button
          type="button"
          className={application === null ? "primary" : undefined}
          onClick={() => setApplication(null)}
        >
          {t("inbox.all")}
        </button>
        {APPLICATIONS.map((key) => (
          <button
            key={key}
            type="button"
            className={application === key ? "primary" : undefined}
            onClick={() => setApplication(key)}
          >
            <AppTag application={key} />
            {tab === "inbox" && unread(key) > 0 ? ` ${unread(key)}` : ""}
          </button>
        ))}
      </div>

      {failed && (
        <p className="bad" role="alert">
          {t("inbox.loadFailed")}{" "}
          <button type="button" onClick={load}>
            {t("inbox.retry")}
          </button>
        </p>
      )}

      {reading && (
        <article className="inbox-reading" data-testid="inbox-reading">
          <header>
            <AppTag application={reading.application} />
            {reading.urgent && (
              <span className={`chip${reading.unresolved ? " urgent" : ""}`}>
                {reading.unresolved ? t("inbox.unresolved") : t("inbox.urgent")}
              </span>
            )}
          </header>
          <h2>{titleOf(reading, t)}</h2>
          <p className="muted small">
            {t(`inbox.kind.${reading.kind}`, { defaultValue: reading.kind })}
            {reading.created_at
              ? ` · ${formatDateTime(reading.created_at, i18n.language) ?? ""}`
              : ""}
          </p>
          {reading.summary && (
            <p className="inbox-summary">{reading.summary}</p>
          )}
          <ItemMarks api={api} itemId={reading.id} tick={tick} />
          <div className="actions">
            {reading.ref.module && (
              <button
                type="button"
                className="primary"
                onClick={() => showItem(reading)}
              >
                {t("inbox.show")}
              </button>
            )}
            <button
              type="button"
              onClick={() =>
                void change(api.pinInboxItem(reading.id, !reading.pinned))
              }
            >
              {reading.pinned ? t("inbox.unpin") : t("inbox.pin")}
            </button>
            {reading.unresolved && (
              <button
                type="button"
                onClick={() => void change(api.resolveInboxItem(reading.id))}
              >
                {t("inbox.resolve")}
              </button>
            )}
            {reading.filed && (
              <button
                type="button"
                className="danger"
                onClick={() => void remove(reading)}
              >
                {t("inbox.delete")}
              </button>
            )}
            <button type="button" onClick={() => setReading(null)}>
              {t("inbox.close")}
            </button>
          </div>
        </article>
      )}

      {!failed && items.length === 0 && (
        <p className="muted" data-testid="inbox-empty">
          {tab === "inbox" ? t("inbox.empty") : t("inbox.filedEmpty")}
        </p>
      )}
      <ul className="inbox-list">
        {rowsOf(items).map((row) => {
          if ("item" in row) return <li key={row.item.id}>{itemButton(row.item)}</li>;
          const open = openBundles.includes(row.bundle);
          const errors = row.items.filter((i) => i.kind === "review_error").length;
          const pending = row.items.some((i) => i.unresolved || !i.read);
          return (
            <li key={`bundle-${row.bundle}`} className="inbox-bundle" data-testid="inbox-bundle">
              <div className="inbox-bundle-head">
                <button
                  type="button"
                  className={`inbox-item${errors > 0 ? " is-urgent" : ""}`}
                  aria-expanded={open}
                  onClick={() =>
                    setOpenBundles((o) => (open ? o.filter((k) => k !== row.bundle) : [...o, row.bundle]))
                  }
                >
                  <span className="inbox-dot" aria-hidden="true">
                    {open ? "▾" : "▸"}
                  </span>
                  <AppTag application={row.bundle} />
                  <span className="inbox-title">
                    {t("inbox.bundle.title", { count: row.items.length })}
                    {errors > 0 && ` · ${t("inbox.bundle.errors", { count: errors })}`}
                  </span>
                </button>
                {pending && tab === "inbox" && (
                  <button
                    type="button"
                    disabled={settling !== null}
                    onClick={() => void settle(row.bundle, row.items)}
                  >
                    {settling === row.bundle ? t("inbox.bundle.settling") : t("inbox.bundle.settle")}
                  </button>
                )}
              </div>
              {open && (
                <ul className="inbox-list inbox-bundle-items">
                  {row.items.map((item) => (
                    <li key={item.id}>{itemButton(item)}</li>
                  ))}
                </ul>
              )}
            </li>
          );
        })}
      </ul>
    </section>
  );
}
