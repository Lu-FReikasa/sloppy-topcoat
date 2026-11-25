import { useCallback, useEffect, useMemo, useState } from "react";
import { useTranslation } from "react-i18next";
import type { ApiClient } from "../api/client";
import { Markdown } from "../chat/Markdown";
import { describeError, toActionError } from "../errors";
import { formatDateTime } from "../format";
import type { ReportDiff, WikiPage as WikiPageRow, WikiPageDetail, WikiTree } from "../types";
import { linkCitations, parseDoc } from "./doc";
import { allFolders } from "./tree";
import { WikiNav } from "./WikiNav";
import "./wiki.css";

interface Props {
  api: ApiClient;
  /** The id of the open page. */
  id?: string;
}

/** Topcoat's own wiki: the pages by section on the left, the open page as a document (header with
 *  its version and origin, contents, sources apart), its versions and their diff. Pages come from
 *  projects ("Take into the wiki" on a document); there is no editor here. */
export function WikiPage({ api, id }: Props) {
  const { t, i18n } = useTranslation();
  const [tree, setTree] = useState<WikiTree | null>(null);
  const [error, setError] = useState("");
  const [loads, setLoads] = useState(0);

  useEffect(() => {
    api.wikiPages().then(
      (r) => setTree(r),
      (e) => setError(describeError(toActionError(e), t, i18n)),
    );
  }, [api, t, i18n, loads]);
  const reload = useCallback(() => setLoads((n) => n + 1), []);

  // the person's order: a page dragged onto a folder, a folder renamed
  const failed = useCallback((e: unknown) => setError(t("wiki.failed", { error: describeError(toActionError(e), t, i18n) })), [t, i18n]);
  const move = useCallback(
    (page: WikiPageRow, folder: string) => {
      setError("");
      api.wikiUpdatePage(page.id, { folder }).then(reload, failed);
    },
    [api, reload, failed],
  );
  const renameFolder = useCallback(
    (folder: string, next: string) => {
      setError("");
      api.wikiRenameFolder(folder, next).then(reload, failed);
    },
    [api, reload, failed],
  );

  const empty = tree !== null && tree.sections.length === 0;
  return (
    <section className="wiki-shell" data-testid="wiki">
      <div className="wiki">
      <nav className="wiki-nav" aria-label={t("wiki.tree")} data-testid="wiki-tree">
        <h1 className="wiki-nav-title">{t("wiki.title")}</h1>
        {error && (
          <p className="error" role="alert">
            {error}
          </p>
        )}
        {empty && <p className="muted small">{t("wiki.empty")}</p>}
        {tree && !empty && <WikiNav tree={tree} id={id} onMove={move} onRenameFolder={renameFolder} />}
      </nav>
      {id ? (
        <OpenPage key={id} api={api} id={id} folders={tree ? allFolders(tree) : []} onChanged={reload} />
      ) : (
        !empty && <p className="wiki-pick muted">{t("wiki.pick")}</p>
      )}
      </div>
    </section>
  );
}

function OpenPage({
  api,
  id,
  folders,
  onChanged,
}: {
  api: ApiClient;
  id: string;
  /** Every folder there is (to move the page to). */
  folders: string[];
  /** The page's place or categories changed: the tree loads again. */
  onChanged: () => void;
}) {
  const { t, i18n } = useTranslation();
  const [detail, setDetail] = useState<WikiPageDetail | null>(null);
  const [diff, setDiff] = useState<ReportDiff | null>(null);
  const [error, setError] = useState("");

  const load = useCallback(
    (version?: number) => {
      api.wikiPage(id, version).then(
        (d) => {
          setDetail(d);
          setError("");
        },
        (e) => setError(describeError(toActionError(e), t, i18n)),
      );
    },
    [api, id, t, i18n],
  );
  useEffect(() => load(), [load]);

  const doc = useMemo(() => (detail ? parseDoc(detail.shown.body) : null), [detail]);

  const compare = async (from: number, to: number) => {
    try {
      setDiff(await api.wikiDiff(id, from, to));
    } catch (e) {
      setError(describeError(toActionError(e), t, i18n));
    }
  };

  if (!detail || !doc) {
    return (
      <article className="wiki-doc">
        {error ? <p className="error" role="alert">{error}</p> : <p className="muted">{t("app.loading")}</p>}
      </article>
    );
  }
  const shown = detail.shown;
  const old = shown.version !== detail.current_version;
  const meta = shown.meta as Record<string, unknown>;
  const build = typeof meta.build === "string" ? meta.build : null;
  return (
    <>
      <article className="wiki-doc" data-testid="wiki-open">
        <header className="wiki-head">
          <h1>{doc.title ?? detail.title}</h1>
          {(detail.categories ?? []).length > 0 && (
            <p className="wiki-cats">
              {(detail.categories ?? []).map((c) => (
                <span key={c} className="wiki-cat">
                  {c}
                </span>
              ))}
            </p>
          )}
          <PageOrder
            api={api}
            page={detail}
            folders={folders}
            onSaved={() => {
              load(shown.version);
              onChanged();
            }}
          />
          <p className="wiki-meta" data-testid="wiki-status">
            {t("wiki.version", { n: shown.version })}
            {" · "}
            {formatDateTime(shown.created_at, i18n.language) ?? ""}
            {build && (
              <>
                {" · "}
                {t("wiki.build", { build, date: String(meta.date ?? "") })}
              </>
            )}
          </p>
          {old && (
            <p className="wiki-old" role="status">
              {t("wiki.oldVersion", { n: shown.version, current: detail.current_version })}{" "}
              <button type="button" className="link" onClick={() => load()}>
                {t("wiki.toCurrent")}
              </button>
            </p>
          )}
          {doc.made && <p className="wiki-made">{t("wiki.checked")}</p>}
        </header>
        {error && (
          <p className="error" role="alert">
            {error}
          </p>
        )}
        <div className="wiki-body" data-testid="wiki-body">
          <Markdown text={linkCitations(doc.body)} doc />
        </div>
        {doc.sources.length > 0 && (
          <section className="wiki-sources" aria-labelledby="wiki-sources">
            <h2 id="wiki-sources">{t("wiki.sources")}</h2>
            <ol>
              {doc.sources.map((s, i) => (
                <li key={`${s.ref}-${i}`} id={s.ref ? `cite-${s.ref}` : undefined}>
                  {s.ref && <span className="wiki-ref">{s.ref}</span>}
                  <Markdown text={s.text} />
                </li>
              ))}
            </ol>
          </section>
        )}
        {doc.removed.length > 0 && (
          <details className="wiki-removed">
            <summary>{t("wiki.removed", { count: doc.removed.length })}</summary>
            <ul>
              {doc.removed.map((r, i) => (
                <li key={i}>
                  <Markdown text={r} />
                </li>
              ))}
            </ul>
          </details>
        )}
        <details className="wiki-versions">
          <summary>{t("wiki.versions", { count: detail.versions.length })}</summary>
          <ol data-testid="wiki-versions">
            {detail.versions.map((v) => (
              <li key={v.version}>
                <span>
                  {t("wiki.version", { n: v.version })} · {formatDateTime(v.created_at, i18n.language) ?? ""}
                  {v.version === detail.current_version ? ` · ${t("wiki.current")}` : ""}
                </span>
                <button type="button" disabled={v.version === shown.version} onClick={() => load(v.version)}>
                  {t("wiki.show")}
                </button>
                {v.version > 1 && (
                  <button type="button" onClick={() => void compare(v.version - 1, v.version)}>
                    {t("wiki.diffTo", { from: v.version - 1, to: v.version })}
                  </button>
                )}
              </li>
            ))}
          </ol>
          {diff && (
            <div data-testid="wiki-diff">
              <p className="muted small">
                {t("wiki.diffTo", { from: diff.from, to: diff.to })} ·{" "}
                {t("wiki.diffCount", { added: diff.added, removed: diff.removed })}
              </p>
              <pre className="report-diff">
                {diff.diff.split("\n").map((line, i) => (
                  <div
                    key={i}
                    className={
                      line.startsWith("+") && !line.startsWith("+++")
                        ? "diff-add"
                        : line.startsWith("-") && !line.startsWith("---")
                          ? "diff-del"
                          : undefined
                    }
                  >
                    {line || " "}
                  </div>
                ))}
              </pre>
            </div>
          )}
        </details>
      </article>
      {doc.headings.length > 1 && (
        <nav className="wiki-toc" aria-label={t("wiki.onThisPage")}>
          <h2>{t("wiki.onThisPage")}</h2>
          <ul>
            {doc.headings.map((h) => (
              <li key={h.id}>
                <a
                  href={`#${h.id}`}
                  onClick={(e) => {
                    e.preventDefault();
                    document.getElementById(h.id)?.scrollIntoView({ behavior: "smooth", block: "start" });
                  }}
                >
                  {h.text}
                </a>
              </li>
            ))}
            {doc.sources.length > 0 && (
              <li>
                <a
                  href="#wiki-sources"
                  onClick={(e) => {
                    e.preventDefault();
                    document.getElementById("wiki-sources")?.scrollIntoView({ behavior: "smooth" });
                  }}
                >
                  {t("wiki.sources")}
                </a>
              </li>
            )}
          </ul>
        </nav>
      )}
    </>
  );
}

/** The page's place and labels, changed by the person: folder (an existing one or a new path),
 *  title, categories. */
function PageOrder({
  api,
  page,
  folders,
  onSaved,
}: {
  api: ApiClient;
  page: WikiPageDetail;
  folders: string[];
  onSaved: () => void;
}) {
  const { t, i18n } = useTranslation();
  const [open, setOpen] = useState(false);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  if (!open) {
    return (
      <button type="button" className="link wiki-order-open" onClick={() => setOpen(true)}>
        {t("wiki.order")}
      </button>
    );
  }
  return (
    <form
      className="wiki-order"
      onSubmit={(e) => {
        e.preventDefault();
        const f = new FormData(e.currentTarget);
        const categories = String(f.get("categories") ?? "")
          .split(",")
          .map((c) => c.trim())
          .filter(Boolean);
        setBusy(true);
        setError("");
        api
          .wikiUpdatePage(page.id, {
            folder: String(f.get("folder") ?? ""),
            title: String(f.get("title") ?? ""),
            categories,
          })
          .then(
            () => {
              setOpen(false);
              onSaved();
            },
            (err: unknown) => setError(describeError(toActionError(err), t, i18n)),
          )
          .finally(() => setBusy(false));
      }}
    >
      <label>
        {t("wiki.folder")}
        <input name="folder" list="wiki-folders" defaultValue={page.folder ?? page.section} required />
        <datalist id="wiki-folders">
          {folders.map((f) => (
            <option key={f} value={f} />
          ))}
        </datalist>
      </label>
      <label>
        {t("wiki.pageTitle")}
        <input name="title" defaultValue={page.title} required maxLength={200} />
      </label>
      <label>
        {t("wiki.categoriesField")}
        <input name="categories" defaultValue={(page.categories ?? []).join(", ")} />
      </label>
      {error && (
        <p className="error small" role="alert">
          {error}
        </p>
      )}
      <div className="wiki-order-buttons">
        <button type="submit" className="primary" disabled={busy}>
          {t("wiki.save")}
        </button>
        <button type="button" onClick={() => setOpen(false)}>
          {t("wiki.cancel")}
        </button>
      </div>
    </form>
  );
}
