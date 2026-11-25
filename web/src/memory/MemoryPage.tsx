import { useCallback, useEffect, useState } from "react";
import { useTranslation } from "react-i18next";
import { UnauthorizedError, type ApiClient } from "../api/client";
import { describeError, toActionError } from "../errors";
import { formatDateTime } from "../format";
import { chatHref, settingsHref } from "../route";
import { QuestionCards } from "./QuestionCard";
import { announceMemoryChange } from "./useQuestions";
import { CareSection } from "./CareSection";
import { ReviewRuns } from "./ReviewRuns";
import { NoteCare } from "./NoteCare";
import { Placement } from "./Placement";
import { SortBanner } from "./SortBanner";
import { TagTree, tagLabel } from "./TagTree";
import { PROGRAMS } from "./programs";
import { Reports } from "./Reports";
import type { MemoryItem, MemoryOverview, MemoryRun, MemoryTag, MemoryVersion } from "../types";
import { askConfirm } from "../components/askConfirm";

interface Props {
  api: ApiClient;
  /** "care" = the technical runs of the memory (admin area, `#/admin/care`); the default is what
   *  a person sees in everyday use. */
  mode?: "user" | "care";
  /** The program it is shown in: a program shows its own memory only (D74); Settings (or none)
   *  shows the core, which every program reads, and the tag tree. */
  application?: string;
}

const STATUSES = ["active", "disputed", "superseded", "deleted"] as const;
const EVIDENCE = ["live", "doc", "assumption"] as const;
/** A program sees its own memory and its archive; Settings the core, its archive and the tag tree.
 *  No page shows another program's memory: each program keeps to its own. */
type Tab = "own" | "core" | "archive" | "tags";

function belongs(item: MemoryItem, tab: Tab, program: string | null): boolean {
  if (tab === "archive") return item.status === "archived";
  if (item.status === "archived") return false;
  return tab === "core" ? !item.application : item.application === program;
}

/** What the assistant remembers about the signed-in user: search, filter, history, edit,
 *  revert, delete, decide disputes, run the consolidation, forget everything (D19). */
export function MemoryPage({ api, mode = "user", application }: Props) {
  const { t, i18n } = useTranslation();
  const [overview, setOverview] = useState<MemoryOverview | null>(null);
  const [query, setQuery] = useState("");
  const [status, setStatus] = useState("");
  const [evidence, setEvidence] = useState("");
  const program = (PROGRAMS as readonly string[]).includes(application ?? "") ? application! : null;
  const tabs: Tab[] = program ? ["own", "archive"] : ["core", "archive", "tags"];
  const [tab, setTab] = useState<Tab>(program ? "own" : "core");
  const [tag, setTag] = useState("");
  const [tags, setTags] = useState<MemoryTag[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [message, setMessage] = useState<string | null>(null);

  const fail = useCallback(
    (err: unknown) => {
      if (!(err instanceof UnauthorizedError)) setError(describeError(toActionError(err), t, i18n));
    },
    [t, i18n],
  );

  const reload = useCallback(() => {
    if (tab === "tags") return;
    api
      .memory({
        q: query,
        status: tab === "archive" ? "archived" : status,
        evidence,
        tag,
        // a program asks for its own memory, Settings for the core — the archive too; the care
        // form (the admin area) shows the runs of all of it
        application: mode === "care" ? undefined : (program ?? "core"),
      })
      .then(setOverview, fail);
  }, [api, fail, query, status, evidence, tab, tag, program, mode]);

  useEffect(() => {
    if (tab === "tags") return;
    api.memoryTags().then(
      (tree) => setTags(tree.tags),
      () => undefined,
    );
  }, [api, tab]);

  useEffect(() => {
    const timer = setTimeout(reload, 200);
    return () => clearTimeout(timer);
  }, [reload]);

  // A running consolidation or care run is followed until it ends.
  const digesting = (overview?.digest?.running ?? 0) + (overview?.digest?.waiting ?? 0) > 0;
  const running =
    overview?.running === true || overview?.care?.running === true || digesting;
  useEffect(() => {
    if (!running) return;
    const timer = setInterval(reload, 3000);
    return () => clearInterval(timer);
  }, [running, reload]);

  async function act<T>(fn: () => Promise<T>, done?: string) {
    setError(null);
    setMessage(null);
    try {
      await fn();
      if (done) setMessage(done);
      reload();
      announceMemoryChange();
    } catch (err) {
      fail(err);
    }
  }

  async function consolidate() {
    setError(null);
    setMessage(null);
    try {
      await api.consolidateMemory();
      reload();
    } catch (err) {
      const code = toActionError(err).code;
      if (["no_node_available", "already_running", "memory_disabled"].includes(code)) {
        setMessage(t(`memory.consolidateRefused.${code}`));
      } else {
        fail(err);
      }
    }
  }

  if (!overview) {
    return error ? (
      <p className="error" role="alert">
        {error}
      </p>
    ) : (
      <p className="muted">{t("app.loading")}</p>
    );
  }
  if (mode === "care") {
    return (
      <section className="memory">
        <h2>{t("care.memory")}</h2>
        <p className="muted small">{t("care.memoryIntro")}</p>
        {error && (
          <p className="error" role="alert">
            {error}
          </p>
        )}
        {message && (
          <p className="notice" role="status">
            {message}
          </p>
        )}
        {overview.care ? (
          <CareSection
            api={api}
            care={overview.care}
            locale={i18n.language}
            act={act}
            onError={fail}
            onMessage={setMessage}
            reload={reload}
          />
        ) : (
          <p className="muted">{t("care.none")}</p>
        )}
        {overview.reviews && <ReviewRuns runs={overview.reviews.runs} locale={i18n.language} />}
      </section>
    );
  }
  const byId = new Map(overview.items.map((i) => [i.id, i]));
  const lastRun = overview.runs[0];
  const visible = overview.items.filter((i) => belongs(i, tab, program));
  const tabCount = (k: Tab): number | null => {
    if (k === "tags") return null;
    if (k === "archive") return overview.counts.archived ?? 0;
    const key = k === "core" ? "core" : program;
    return overview.applications && key ? (overview.applications[key] ?? 0) : null;
  };
  const tabLabel = (k: Tab): string =>
    k === "own" ? t("memory.tabs.own", { program: t(`inbox.apps.${program}`) }) : t(`memory.tabs.${k}`);
  // the questions about this page's notes (a program's doubts stay with the program); one of the
  // cross-check is shown where the decision lies only (D74)
  const questions = overview.questions.filter((q) =>
    q.program ? q.program === (program ?? "person") : q.candidates.some((c) => byId.has(c.id)),
  );

  return (
    <section className="memory">
      <h1>{t("memory.heading")}</h1>
      <p>{t("memory.intro")}</p>
      {!overview.enabled && (
        <p className="notice" role="status">
          {t("memory.off")} <a href={settingsHref}>{t("memory.offLink")}</a>
        </p>
      )}
      {overview.embedder.transitional && (
        <p className="muted small" data-testid="transitional-note">
          {t("memory.transitional")}
        </p>
      )}
      {error && (
        <p className="error" role="alert">
          {error}
        </p>
      )}
      {message && (
        <p className="notice" role="status">
          {message}
        </p>
      )}

      {program === "chat" && <SortBanner api={api} tick={overview.items.length} onDone={reload} />}
      {program && (
        <p className="muted small" data-testid="core-hint">
          {t("memory.coreHint")}
        </p>
      )}

      {questions.length > 0 && (
        <>
          <h2>{t("memory.openQuestions")}</h2>
          <p className="muted small">{t("memory.openQuestionsHint")}</p>
          <QuestionCards api={api} questions={questions} onDone={reload} />
        </>
      )}

      <div className="memory-tabs" role="tablist" aria-label={t("memory.tabs.label")}>
        {tabs.map((k) => (
          <button
            key={k}
            type="button"
            role="tab"
            id={`memory-tab-${k}`}
            aria-selected={tab === k}
            aria-controls="memory-panel"
            className={tab === k ? "tab active" : "tab"}
            onClick={() => setTab(k)}
          >
            {tabLabel(k)}
            {tabCount(k) !== null && <span className="muted small"> ({tabCount(k)})</span>}
          </button>
        ))}
      </div>
      {tab !== "tags" && (
        <p className="muted small" data-testid="tab-hint">
          {tab === "own" ? t(`memory.tabHint.${program}`) : t(`memory.tabHint.${tab}`)}
        </p>
      )}

      {tab === "tags" ? (
        <TagTree api={api} />
      ) : (
        <>
      <div
        className="memory-filters"
        role="tabpanel"
        id="memory-panel"
        aria-labelledby={`memory-tab-${tab}`}
      >
        <input
          type="search"
          aria-label={t("memory.search")}
          placeholder={t("memory.search")}
          value={query}
          onChange={(e) => setQuery(e.target.value)}
        />
        {tab !== "archive" && (
          <label>
            <span className="fact-label">{t("memory.filterStatus")}</span>
            <select value={status} onChange={(e) => setStatus(e.target.value)}>
              <option value="">{t("memory.allExceptDeleted")}</option>
              {STATUSES.map((s) => (
                <option key={s} value={s}>
                  {t(`memory.status.${s}`)} ({overview.counts[s]})
                </option>
              ))}
            </select>
          </label>
        )}
        <label>
          <span className="fact-label">{t("memory.filterTag")}</span>
          <select value={tag} onChange={(e) => setTag(e.target.value)} data-testid="tag-filter">
            <option value="">{t("memory.allTags")}</option>
            {tags.map((x) => (
              <option key={x.id} value={x.name}>
                {tagLabel(x, t)}
              </option>
            ))}
          </select>
        </label>
        <label>
          <span className="fact-label">{t("memory.filterEvidence")}</span>
          <select value={evidence} onChange={(e) => setEvidence(e.target.value)}>
            <option value="">{t("memory.allEvidence")}</option>
            {EVIDENCE.map((e) => (
              <option key={e} value={e}>
                [{e}]
              </option>
            ))}
          </select>
        </label>
      </div>

      {visible.length === 0 ? (
        <p className="muted">
          {query || status || evidence || tag
            ? t("memory.noMatches")
            : t(
                tab === "archive"
                  ? "memory.emptyArchive"
                  : tab === "own"
                    ? "memory.emptyApp"
                    : "memory.empty",
              )}
        </p>
      ) : (
        <ul className="memory-list">
          {visible.map((item) => (
            <MemoryCard
              key={item.id}
              api={api}
              item={item}
              tags={tags}
              partner={item.disputed_with ? byId.get(item.disputed_with) : undefined}
              locale={i18n.language}
              act={act}
              onError={fail}
            />
          ))}
        </ul>
      )}

          {tab !== "archive" && <Reports api={api} recipient={program ?? "person"} />}
        </>
      )}

      <div className="memory-actions">
        <button type="button" onClick={() => void consolidate()} disabled={running}>
          {running ? t("memory.consolidating") : t("memory.consolidate")}
        </button>
        {lastRun && <RunSummary run={lastRun} locale={i18n.language} />}
        <span className="spacer" />
        {overview.counts.deleted > 0 && (
          <button
            type="button"
            className="danger"
            onClick={async () => {
              if (await askConfirm(t("memory.confirmPurge")))
                void act(() => api.purgeDeletedMemory(), t("memory.purged"));
            }}
          >
            {t("memory.purge")}
          </button>
        )}
        <button
          type="button"
          className="danger"
          onClick={async () => {
            if (await askConfirm(t("memory.confirmForget")))
              void act(() => api.forgetAllMemory(), t("memory.forgotten"));
          }}
        >
          {t("memory.forgetAll")}
        </button>
      </div>
      {digesting && overview.digest && (
        <p className="muted" role="status" data-testid="digest-status">
          {t("memory.digest", {
            running: overview.digest.running,
            waiting: overview.digest.waiting,
          })}
        </p>
      )}
      {overview.blocked > 0 && (
        <p className="muted small" data-testid="blocked-total">
          {t("memory.blockedTotal", { count: overview.blocked })}
        </p>
      )}
    </section>
  );
}

function RunSummary({ run, locale }: { run: MemoryRun; locale: string }) {
  const { t } = useTranslation();
  const s = run.stats as Record<string, number | undefined>;
  return (
    <span className="muted small" data-testid="run-summary">
      {t("memory.lastRun", {
        when: formatDateTime(run.finished_at ?? run.started_at, locale) ?? "–",
        status: t(`memory.runStatus.${run.status}`),
        merged: s.merged ?? 0,
        resolved: s.resolved ?? 0,
        disputed: s.disputed ?? 0,
        corrected: s.corrected ?? 0,
        questions: s.questions ?? 0,
      })}
    </span>
  );
}

interface CardProps {
  api: ApiClient;
  item: MemoryItem;
  tags: MemoryTag[];
  partner: MemoryItem | undefined;
  locale: string;
  act: <T>(fn: () => Promise<T>, done?: string) => Promise<void>;
  onError: (err: unknown) => void;
}

function MemoryCard({ api, item, tags, partner, locale, act, onError }: CardProps) {
  const { t } = useTranslation();
  const [editing, setEditing] = useState<string | null>(null);
  const [versions, setVersions] = useState<MemoryVersion[] | null>(null);
  const conversationId = item.source_ref.conversation_id;
  // The newer note of a dispute is the suggestion: usually the user has moved on.
  const suggested: "this" | "other" | null =
    item.status === "disputed" && partner && item.created_at && partner.created_at
      ? item.created_at > partner.created_at
        ? "this"
        : "other"
      : null;

  async function toggleHistory() {
    if (versions) {
      setVersions(null);
      return;
    }
    try {
      setVersions((await api.memoryItem(item.id)).versions);
    } catch (err) {
      onError(err);
    }
  }

  return (
    <li className={`memory-item memory-${item.status}`} data-testid="memory-item">
      {editing === null ? (
        <p className="memory-text">{item.text}</p>
      ) : (
        <div className="memory-edit">
          <textarea
            aria-label={t("memory.editText")}
            value={editing}
            rows={2}
            maxLength={1000}
            onChange={(e) => setEditing(e.target.value)}
          />
          <button
            type="button"
            className="primary"
            disabled={!editing.trim()}
            onClick={() => {
              const text = editing.trim();
              setEditing(null);
              void act(() => api.editMemory(item.id, { text }));
            }}
          >
            {t("memory.save")}
          </button>
          <button type="button" onClick={() => setEditing(null)}>
            {t("memory.cancel")}
          </button>
        </div>
      )}
      <p className="muted small memory-meta">
        <span className={`tag tag-${item.evidence}`}>[{item.evidence}]</span>{" "}
        <span className={`badge badge-memory-${item.status}`}>
          {t(`memory.status.${item.status}`)}
        </span>{" "}
        {t("memory.confidence", { value: Math.round(item.confidence * 100) })} ·{" "}
        {t("memory.version", { value: item.version })} ·{" "}
        {formatDateTime(item.updated_at, locale) ?? "–"}
        {item.confirmed_count > 0 && (
          <span data-testid="confirmed">
            {" · "}
            {t("memory.confirmedCount", {
              count: item.confirmed_count,
              when: formatDateTime(item.last_confirmed_at, locale) ?? "–",
            })}
          </span>
        )}
        {conversationId && (
          <>
            {" · "}
            <a href={chatHref(conversationId)}>{t("memory.fromChat")}</a>
          </>
        )}
      </p>
      <NoteCare item={item} locale={locale} />
      {item.status !== "archived" && <Placement api={api} item={item} tags={tags} act={act} />}
      {item.status === "disputed" && (
        <div className="notice-line" data-testid="dispute">
          {partner ? (
            <p>{t("memory.contradicts", { text: partner.text })}</p>
          ) : (
            <p>{t("memory.contradictsHidden")}</p>
          )}
          {suggested === "this" && (
            <p className="small" data-testid="suggested-newer">
              {t("memory.newerSuggested")}
            </p>
          )}
          <button
            type="button"
            className={suggested === "this" ? "primary" : undefined}
            onClick={() => void act(() => api.resolveMemory(item.id, "this"))}
          >
            {t("memory.keepThis")}
          </button>
          <button
            type="button"
            className={suggested === "other" ? "primary" : undefined}
            onClick={() => void act(() => api.resolveMemory(item.id, "other"))}
          >
            {suggested === "other" ? t("memory.keepOtherNewer") : t("memory.keepOther")}
          </button>
          <button type="button" onClick={() => void act(() => api.resolveMemory(item.id, "both"))}>
            {t("memory.keepBoth")}
          </button>
        </div>
      )}
      <div className="row-actions">
        {item.status === "deleted" || item.status === "archived" ? (
          <button type="button" onClick={() => void act(() => api.restoreMemory(item.id))}>
            {t("memory.restore")}
          </button>
        ) : (
          <>
            <button type="button" onClick={() => setEditing(item.text)}>
              {t("memory.edit")}
            </button>
            {item.status === "active" && (
              <button type="button" onClick={() => void act(() => api.archiveMemory(item.id))}>
                {t("memory.archive")}
              </button>
            )}
            {/* a note about the person can be held in the core, so it is always there (the
                profile of the old instructions page, one list since phase 4) */}
            {item.status === "active" && !item.application && item.kind !== "operational" && (
              <label className="check pin-check">
                <input
                  type="checkbox"
                  checked={item.core === true}
                  onChange={(e) => {
                    const core = e.target.checked;
                    void act(() => api.editCoreItem(item.id, { core }));
                  }}
                />
                {t("instructions.pin")}
              </label>
            )}
            <button
              type="button"
              className="danger"
              onClick={() => void act(() => api.deleteMemory(item.id))}
            >
              {t("memory.delete")}
            </button>
          </>
        )}
        <button type="button" onClick={() => void toggleHistory()} aria-expanded={!!versions}>
          {versions ? t("memory.hideHistory") : t("memory.history")}
        </button>
      </div>
      {versions && (
        <ol className="memory-history" data-testid="history">
          {versions.map((v) => (
            <li key={v.version}>
              <p>
                <strong>v{v.version}</strong> · {t(`memory.actor.${v.actor}`)} ·{" "}
                {formatDateTime(v.created_at, locale) ?? "–"} ·{" "}
                <span className="muted">{t(`memory.status.${v.status}`)}</span>
              </p>
              <p>{v.text}</p>
              {v.reason && <p className="muted small">{v.reason}</p>}
              <VersionSource source={v.source} />
              {v.version !== item.version && (
                <button
                  type="button"
                  onClick={() => {
                    setVersions(null);
                    void act(() => api.revertMemory(item.id, v.version));
                  }}
                >
                  {t("memory.revert", { version: v.version })}
                </button>
              )}
            </li>
          ))}
        </ol>
      )}
    </li>
  );
}

/** What a change is based on (D19 extended): the conversation, the user's cue, the question. */
function VersionSource({ source }: { source: MemoryVersion["source"] }) {
  const { t } = useTranslation();
  if (!source) return null;
  const parts = [
    source.conversation_id && (
      <a key="c" href={chatHref(source.conversation_id)}>
        {t("memory.source.conversation")}
      </a>
    ),
    source.question_id && <span key="q">{t("memory.source.question")}</span>,
    source.cue && <span key="k">{t("memory.source.cue", { cue: source.cue })}</span>,
  ].filter(Boolean);
  if (parts.length === 0) return null;
  return (
    <p className="muted small" data-testid="version-source">
      {parts.map((p, i) => (
        <span key={i}>
          {i > 0 && " · "}
          {p}
        </span>
      ))}
    </p>
  );
}
