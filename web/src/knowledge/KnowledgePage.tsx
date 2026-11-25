import { useCallback, useEffect, useState, type FormEvent } from "react";
import { useTranslation } from "react-i18next";
import { UnauthorizedError, type ApiClient } from "../api/client";
import { describeError, toActionError } from "../errors";
import { formatDateTime, formatInt } from "../format";
import type {
  KnowledgeHit,
  KnowledgeSource,
  KnowledgeStatus,
  Me,
} from "../types";
import { askConfirm } from "../components/askConfirm";

interface Props {
  api: ApiClient;
  me: Me | null;
  /** "care" = adding, re-indexing and removing sources (admin area); the default is search and the
   *  list of sources, which everyone may read. */
  mode?: "user" | "care";
}

/** The knowledge base (E2-03): the folders of notes the assistant can search, a search box, and —
 *  for the admin — adding, re-indexing and removing a source. */
export function KnowledgePage({ api, me, mode = "user" }: Props) {
  const { t, i18n } = useTranslation();
  const care = mode === "care";
  const admin = me?.role === "admin" && care;
  const [sources, setSources] = useState<KnowledgeSource[] | null>(null);
  const [status, setStatus] = useState<KnowledgeStatus | null>(null);
  const [error, setError] = useState<string | null>(null);

  const fail = useCallback(
    (err: unknown) => {
      if (!(err instanceof UnauthorizedError))
        setError(describeError(toActionError(err), t, i18n));
    },
    [t, i18n],
  );
  const load = useCallback(() => {
    api.knowledgeSources().then(setSources, fail);
  }, [api, fail]);

  useEffect(() => {
    load();
    api.knowledgeStatus().then(setStatus, () => undefined);
  }, [api, load]);

  // a source that is being indexed changes without a click: look again
  const indexing =
    sources?.some((s) => s.status === "indexing" || s.status === "new") ??
    false;
  useEffect(() => {
    if (!indexing) return;
    const timer = setInterval(load, 2000);
    return () => clearInterval(timer);
  }, [indexing, load]);

  if (care) {
    return (
      <section className="knowledge">
        <h2>{t("care.knowledge")}</h2>
        <p className="muted small">{t("care.knowledgeIntro")}</p>
        {error && (
          <p className="error" role="alert">
            {error}
          </p>
        )}
        {sources?.map((s) => (
          <SourceCard key={s.id} api={api} source={s} admin={admin} onChanged={load} fail={fail} />
        ))}
        {admin && <AddSource api={api} onAdded={load} />}
        {admin && <UploadFiles api={api} onDone={load} />}
        {status && (
          <p className="muted small" data-testid="knowledge-exclude">
            {t("knowledge.exclude", { list: status.exclude.join(", ") })}
          </p>
        )}
      </section>
    );
  }

  return (
    <section className="knowledge">
      <h1>{t("knowledge.heading")}</h1>
      <p className="muted">{t("knowledge.intro")}</p>
      {status?.transitional && (
        <p className="notice" data-testid="knowledge-transitional">
          {t("knowledge.transitional")}
        </p>
      )}
      {error && (
        <p className="error" role="alert">
          {error}
        </p>
      )}
      <Search api={api} fail={fail} />
      <h2>{t("knowledge.sources")}</h2>
      {sources && sources.length === 0 && <p className="muted">{t("knowledge.none")}</p>}
      {sources?.map((s) => (
        <SourceCard key={s.id} api={api} source={s} admin={false} onChanged={load} fail={fail} />
      ))}
    </section>
  );
}

function Search({ api, fail }: { api: ApiClient; fail: (e: unknown) => void }) {
  const { t } = useTranslation();
  const [q, setQ] = useState("");
  const [hits, setHits] = useState<KnowledgeHit[] | null>(null);
  const [busy, setBusy] = useState(false);

  async function run(e: FormEvent) {
    e.preventDefault();
    if (!q.trim()) return;
    setBusy(true);
    try {
      setHits((await api.knowledgeSearch(q.trim())).hits);
    } catch (err) {
      fail(err);
    } finally {
      setBusy(false);
    }
  }

  return (
    <form
      className="form"
      onSubmit={(e) => void run(e)}
      data-testid="knowledge-search"
    >
      <label>
        <span>{t("knowledge.search")}</span>
        <input
          value={q}
          maxLength={300}
          placeholder={t("knowledge.searchPlaceholder")}
          onChange={(e) => setQ(e.target.value)}
        />
      </label>
      <div className="dialog-actions">
        <button type="submit" className="primary" disabled={busy || !q.trim()}>
          {t("knowledge.searchButton")}
        </button>
      </div>
      {hits && hits.length === 0 && (
        <p className="muted">{t("knowledge.noHits")}</p>
      )}
      {hits?.map((h, i) => (
        <article key={i} className="card" data-testid="knowledge-hit">
          <p className="muted small">
            <strong>{h.path}</strong>
            {h.heading ? ` · ${h.heading}` : ""} · {h.source}
          </p>
          <p className="knowledge-text">{h.text}</p>
        </article>
      ))}
    </form>
  );
}

function SourceCard({
  api,
  source: s,
  admin,
  onChanged,
  fail,
}: {
  api: ApiClient;
  source: KnowledgeSource;
  admin: boolean;
  onChanged: () => void;
  fail: (e: unknown) => void;
}) {
  const { t, i18n } = useTranslation();
  const [detail, setDetail] = useState<KnowledgeSource | null>(null);

  async function act(run: () => Promise<unknown>) {
    try {
      await run();
      onChanged();
    } catch (err) {
      fail(err);
    }
  }

  return (
    <article className="card" data-testid="knowledge-source">
      <div className="detail-head">
        <h3>{s.name}</h3>
        <span className={`badge badge-knowledge-${s.status}`}>
          {t(`knowledge.status.${s.status}`)}
        </span>
        <span className="muted small">
          {t(`knowledge.visibility.${s.visibility}`)}
        </span>
      </div>
      <p className="muted small">
        {t("knowledge.counts", {
          files: formatInt(s.files, i18n.language),
          chunks: formatInt(s.chunks, i18n.language),
        })}
        {s.indexed_at
          ? ` · ${formatDateTime(s.indexed_at, i18n.language)}`
          : ""}
        {s.embedded !== null && s.chunks > 0
          ? ` · ${t("knowledge.embedded", {
              embedded: formatInt(s.embedded, i18n.language),
              chunks: formatInt(s.chunks, i18n.language),
            })}`
          : ""}
        {s.own && s.kind === "upload"
          ? ` · ${t("knowledge.uploaded", { count: s.files })}`
          : ""}
        {s.own && s.kind === "node"
          ? ` · ${s.node}${s.root ? `:${s.root}` : ""}`
          : ""}
      </p>
      {(s.excluded > 0 || s.blocked > 0) && (
        <p className="muted small">
          {t("knowledge.kept", { excluded: s.excluded, blocked: s.blocked })}
        </p>
      )}
      {s.error && (
        <p className="error" role="alert">
          {s.error}
        </p>
      )}
      {s.blocked > 0 && s.own && (
        <div>
          <button
            type="button"
            onClick={() => void api.knowledgeSource(s.id).then(setDetail, fail)}
          >
            {t("knowledge.showBlocked")}
          </button>
          {detail?.blocked_files && (
            <ul className="muted small" data-testid="knowledge-blocked">
              {detail.blocked_files.map((f) => (
                <li key={f.path}>
                  {f.path} — {f.rules.join(", ")}
                </li>
              ))}
            </ul>
          )}
        </div>
      )}
      {admin && s.own && s.kind === "upload" && (
        <UploadFiles api={api} source={s} onDone={onChanged} />
      )}
      {admin && s.own && (
        <div className="dialog-actions">
          <button
            type="button"
            disabled={s.status === "indexing"}
            onClick={() => void act(() => api.reindexKnowledgeSource(s.id))}
          >
            {t("knowledge.reindex")}
          </button>
          <button
            type="button"
            className="danger"
            onClick={async () => {
              if (
                await askConfirm(t("knowledge.confirmDelete", { name: s.name }))
              )
                void act(() => api.deleteKnowledgeSource(s.id));
            }}
          >
            {t("knowledge.delete")}
          </button>
        </div>
      )}
    </article>
  );
}

function AddSource({ api, onAdded }: { api: ApiClient; onAdded: () => void }) {
  const { t, i18n } = useTranslation();
  const [name, setName] = useState("");
  const [node, setNode] = useState("");
  const [root, setRoot] = useState("");
  const [visibility, setVisibility] = useState<"shared" | "private">("shared");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function add(e: FormEvent) {
    e.preventDefault();
    setBusy(true);
    setError(null);
    try {
      await api.addKnowledgeSource({
        name: name.trim(),
        node: node.trim(),
        root: root.trim(),
        visibility,
      });
      setName("");
      setRoot("");
      onAdded();
    } catch (err) {
      if (!(err instanceof UnauthorizedError))
        setError(describeError(toActionError(err), t, i18n));
    } finally {
      setBusy(false);
    }
  }

  return (
    <form
      className="form card"
      onSubmit={(e) => void add(e)}
      data-testid="knowledge-add"
    >
      <h3>{t("knowledge.add")}</h3>
      <p className="muted small">{t("knowledge.addHint")}</p>
      {error && (
        <p className="error" role="alert">
          {error}
        </p>
      )}
      <label>
        <span>{t("knowledge.name")}</span>
        <input
          value={name}
          maxLength={100}
          onChange={(e) => setName(e.target.value)}
        />
      </label>
      <label>
        <span>{t("knowledge.node")}</span>
        <input
          value={node}
          maxLength={63}
          onChange={(e) => setNode(e.target.value)}
        />
      </label>
      <label>
        <span>{t("knowledge.root")}</span>
        <input
          value={root}
          maxLength={512}
          placeholder={t("knowledge.rootPlaceholder")}
          onChange={(e) => setRoot(e.target.value)}
        />
      </label>
      <label>
        <span>{t("knowledge.visibilityLabel")}</span>
        <select
          value={visibility}
          onChange={(e) =>
            setVisibility(e.target.value as "shared" | "private")
          }
        >
          <option value="shared">{t("knowledge.visibility.shared")}</option>
          <option value="private">{t("knowledge.visibility.private")}</option>
        </select>
      </label>
      <div className="dialog-actions">
        <button
          type="submit"
          className="primary"
          disabled={busy || !name.trim() || !node.trim()}
        >
          {t("knowledge.addButton")}
        </button>
      </div>
    </form>
  );
}

/** Upload Markdown/text files: into an existing upload source (`source`), or into a new one. */
function UploadFiles({
  api,
  source,
  onDone,
}: {
  api: ApiClient;
  source?: KnowledgeSource;
  onDone: () => void;
}) {
  const { t, i18n } = useTranslation();
  const [name, setName] = useState("");
  const [visibility, setVisibility] = useState<"shared" | "private">("shared");
  const [files, setFiles] = useState<File[]>([]);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function send(e: FormEvent) {
    e.preventDefault();
    setBusy(true);
    setError(null);
    let sourceId = source?.id;
    let current = "";
    try {
      for (const file of files) {
        current = file.name;
        const created = await api.uploadKnowledgeFile({
          source_id: sourceId,
          name: sourceId ? undefined : name.trim(),
          visibility,
          filename: file.name,
          text: await file.text(),
        });
        sourceId = created.id;
      }
      setFiles([]);
      if (!source) setName("");
    } catch (err) {
      if (!(err instanceof UnauthorizedError))
        setError(
          t("knowledge.uploadFailed", {
            file: current,
            message: describeError(toActionError(err), t, i18n),
          }),
        );
    } finally {
      setBusy(false);
      onDone();
    }
  }

  return (
    <form
      className={source ? "form" : "form card"}
      onSubmit={(e) => void send(e)}
      data-testid={source ? "knowledge-upload-more" : "knowledge-upload"}
    >
      {!source && (
        <>
          <h3>{t("knowledge.upload")}</h3>
          <p className="muted small">{t("knowledge.uploadHint")}</p>
        </>
      )}
      {error && (
        <p className="error" role="alert">
          {error}
        </p>
      )}
      {!source && (
        <>
          <label>
            <span>{t("knowledge.name")}</span>
            <input
              value={name}
              maxLength={100}
              onChange={(e) => setName(e.target.value)}
            />
          </label>
          <label>
            <span>{t("knowledge.visibilityLabel")}</span>
            <select
              value={visibility}
              onChange={(e) =>
                setVisibility(e.target.value as "shared" | "private")
              }
            >
              <option value="shared">{t("knowledge.visibility.shared")}</option>
              <option value="private">
                {t("knowledge.visibility.private")}
              </option>
            </select>
          </label>
        </>
      )}
      <label>
        <span>{t("knowledge.uploadFiles")}</span>
        <input
          type="file"
          multiple
          accept=".md,.markdown,.txt,text/markdown,text/plain"
          onChange={(e) => setFiles(Array.from(e.target.files ?? []))}
        />
      </label>
      <div className="dialog-actions">
        <button
          type="submit"
          className="primary"
          disabled={busy || files.length === 0 || (!source && !name.trim())}
        >
          {source ? t("knowledge.uploadAdd") : t("knowledge.uploadButton")}
        </button>
      </div>
    </form>
  );
}
