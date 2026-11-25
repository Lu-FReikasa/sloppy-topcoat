import { useCallback, useEffect, useState } from "react";
import { useTranslation } from "react-i18next";
import { UnauthorizedError, type ApiClient } from "../api/client";
import { describeError, toActionError } from "../errors";
import { formatDateTime } from "../format";
import type {
  ApplicationRow,
  ApplicationsView,
  BackgroundTask,
  SchedulingView,
} from "../types";
import { askConfirm } from "../components/askConfirm";

const POLL_MS = 5000;
/** Pause lengths offered next to "resume", in minutes. */
const PAUSES = [30, 120, 480];
/** Background jobs the admin can start by hand. */
const TASKS: BackgroundTask[] = ["digest", "care", "consolidate"];
/** The values of a row that stand for something instead of a name (D85). */
export const SAME_AS_ANSWER = "same_as_answer";
export const ALL_NODES = "all";

type Rows = Record<string, ApplicationRow>;

/** A row without any setting is no row: the hub drops it as well. */
export function cleanRows(rows: Rows): Rows {
  const out: Rows = {};
  for (const [key, row] of Object.entries(rows)) {
    const clean: ApplicationRow = {};
    if (row.priority) clean.priority = row.priority;
    if (row.models?.length) clean.models = row.models;
    if (row.nodes?.length) clean.nodes = row.nodes;
    if (Object.keys(clean).length > 0) out[key] = clean;
  }
  return out;
}

/** Why a row cannot run as written: models none of its nodes has. The hub does not check model
 *  names, so this is the only place that can say it. */
export function problemsOf(
  row: ApplicationRow,
  nodes: { name: string; kind: string; models: string[] }[],
): string[] {
  const models = (row.models ?? []).filter((m) => m !== SAME_AS_ANSWER);
  if (models.length === 0) return [];
  const compute = nodes.filter((n) => n.kind === "compute");
  const named = (row.nodes ?? []).filter((n) => n !== ALL_NODES);
  const allowed =
    named.length === 0 || (row.nodes ?? []).includes(ALL_NODES)
      ? compute
      : compute.filter((n) => named.includes(n.name));
  return models.filter((m) => !allowed.some((n) => n.models.includes(m)));
}

interface ListProps {
  /** what the list is, for the labels: "research · models" */
  name: string;
  items: string[];
  /** names the admin can add (a node, or the special values) */
  options: { value: string; label: string }[];
  /** a model can be typed, a node can only be picked */
  free?: boolean;
  suggestions?: string[];
  idPrefix: string;
  onChange: (next: string[]) => void;
}

/** An ordered list of names with a keyboard path for every move. */
function NameList({
  name,
  items,
  options,
  free,
  suggestions,
  idPrefix,
  onChange,
}: ListProps) {
  const { t } = useTranslation();
  const [typed, setTyped] = useState("");
  const label = (value: string) =>
    options.find((o) => o.value === value)?.label ?? value;
  const move = (i: number, by: -1 | 1) => {
    const j = i + by;
    if (j < 0 || j >= items.length) return;
    const next = [...items];
    [next[i], next[j]] = [next[j]!, next[i]!];
    onChange(next);
  };
  const add = (value: string) => {
    const v = value.trim();
    if (v && !items.includes(v)) onChange([...items, v]);
    setTyped("");
  };
  const addable = options.filter((o) => !items.includes(o.value));
  return (
    <div data-testid={idPrefix}>
      {items.length > 0 && (
        <ol className="name-list">
          {items.map((item, i) => (
            <li key={item}>
              <span className="name-list-name">{label(item)}</span>
              <button
                type="button"
                aria-label={t("scheduling.itemUp", { n: i + 1, name })}
                disabled={i === 0}
                onClick={() => move(i, -1)}
              >
                ↑
              </button>
              <button
                type="button"
                aria-label={t("scheduling.itemDown", { n: i + 1, name })}
                disabled={i === items.length - 1}
                onClick={() => move(i, 1)}
              >
                ↓
              </button>
              <button
                type="button"
                aria-label={t("scheduling.itemRemove", { n: i + 1, name })}
                onClick={() => onChange(items.filter((_, j) => j !== i))}
              >
                ×
              </button>
            </li>
          ))}
        </ol>
      )}
      <div className="row-actions">
        {free && (
          <>
            <input
              list={`${idPrefix}-choices`}
              value={typed}
              aria-label={t("scheduling.itemType", { name })}
              onChange={(ev) => setTyped(ev.target.value)}
            />
            <datalist id={`${idPrefix}-choices`}>
              {(suggestions ?? [])
                .filter((s) => !items.includes(s))
                .map((s) => (
                  <option key={s} value={s} />
                ))}
            </datalist>
            <button
              type="button"
              disabled={typed.trim() === ""}
              onClick={() => add(typed)}
            >
              {t("scheduling.itemAdd", { name })}
            </button>
          </>
        )}
        {addable.length > 0 && (
          <select
            aria-label={t("scheduling.itemPick", { name })}
            value=""
            onChange={(ev) => ev.target.value && add(ev.target.value)}
          >
            <option value="">{t("scheduling.itemPickNone")}</option>
            {addable.map((o) => (
              <option key={o.value} value={o.value}>
                {o.label}
              </option>
            ))}
          </select>
        )}
      </div>
    </div>
  );
}

/** Which model and node each application uses: one table, one row per application and for the
 *  second look and the background work (D85). Below it the rules that stay: the pause of background
 *  work, the embedding model, a job started by hand and what the admin did here. */
export function SchedulingPage({ api }: { api: ApiClient }) {
  const { t, i18n } = useTranslation();
  const [view, setView] = useState<SchedulingView | null>(null);
  const [table, setTable] = useState<ApplicationsView | null>(null);
  const [draft, setDraft] = useState<Rows | null>(null);
  const [embedding, setEmbedding] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [notice, setNotice] = useState<string | null>(null);

  const fail = useCallback(
    (err: unknown) => {
      if (!(err instanceof UnauthorizedError))
        setError(describeError(toActionError(err), t, i18n));
    },
    [t, i18n],
  );
  const load = useCallback(() => {
    api.getScheduling().then((v) => {
      setError(null);
      setView(v);
    }, fail);
    api.getApplications().then(setTable, fail);
  }, [api, fail]);

  useEffect(() => {
    load();
    const timer = setInterval(load, POLL_MS);
    return () => clearInterval(timer);
  }, [load]);

  async function run(call: () => Promise<unknown>, after?: () => void) {
    setBusy(true);
    setError(null);
    try {
      await call();
      after?.();
      load(); // a failed call keeps its error on screen: the next poll would clear it
    } catch (err) {
      fail(err);
    } finally {
      setBusy(false);
    }
  }

  if (!view || !table) {
    return (
      <section>
        <h1>{t("scheduling.heading")}</h1>
        {error ? (
          <p className="error" role="alert">
            {error}
          </p>
        ) : (
          <p className="muted">{t("app.loading")}</p>
        )}
      </section>
    );
  }

  const rows: Rows = draft ?? table.rows;
  const edit = (key: string, patch: ApplicationRow) =>
    setDraft({ ...rows, [key]: { ...(rows[key] ?? {}), ...patch } });
  const compute = view.nodes.filter((n) => n.kind === "compute");
  const modelNames = [...new Set(compute.flatMap((n) => n.models))].sort();
  const nodeOptions = [
    ...compute.map((n) => ({ value: n.name, label: n.name })),
    { value: ALL_NODES, label: t("scheduling.allNodes") },
  ];
  const locale = i18n.resolvedLanguage ?? "en";
  const paused = view.paused_until !== null;
  const chosenEmbedding = embedding ?? view.embedding_model;

  const rowName = (key: string) =>
    table.system_keys.includes(key)
      ? t(`scheduling.row.${key}`)
      : t(`inbox.apps.${key}`, { defaultValue: key });

  async function start(task: BackgroundTask) {
    setNotice(null);
    await run(
      () => api.runBackgroundTask(task),
      () =>
        setNotice(
          t("scheduling.manage.started", {
            task: t(`scheduling.manage.task.${task}`),
          }),
        ),
    );
  }

  return (
    <section className="scheduling">
      <h1>{t("scheduling.heading")}</h1>
      <p className="muted">{t("scheduling.intro")}</p>
      {error && (
        <p className="error" role="alert">
          {error}
        </p>
      )}

      <h2>{t("scheduling.appsHeading")}</h2>
      <p className="muted small">{t("scheduling.appsHint")}</p>

      {table.keys.map((key) => {
        const row = rows[key] ?? {};
        const isSystem = table.system_keys.includes(key);
        const followsAnswer = key === "check" || key === "background";
        const isApi = key === "api";
        const preview = table.preview[key];
        const problems = problemsOf(row, view.nodes);
        const decides = (row.models?.length ?? 0) + (row.nodes?.length ?? 0) > 0;
        const name = rowName(key);
        return (
          <div className="card" key={key} data-testid={`row-${key}`}>
            <h3>{name}</h3>
            {isApi && <p className="muted small">{t("scheduling.row.apiHint")}</p>}
            {!decides && (
              <p className="muted small" data-testid={`row-${key}-empty`}>
                {t("scheduling.rowEmpty")}
              </p>
            )}
            {preview && preview.model && (
              <p className="small" data-testid={`row-${key}-now`}>
                {t("scheduling.rowNow", {
                  model: preview.model,
                  node: preview.node ?? t("scheduling.anyNode"),
                })}{" "}
                <span className="muted">({preview.reason})</span>
              </p>
            )}
            {preview && !preview.model && preview.reason && (
              <p className="muted small">{preview.reason}</p>
            )}
            {problems.map((m) => (
              <p className="warn-text small" role="status" key={m}>
                {t("scheduling.warnModel", { model: m })}
              </p>
            ))}
            <label>
              {t("scheduling.priority")}{" "}
              <select
                aria-label={t("scheduling.priorityOf", { name })}
                value={row.priority ?? ""}
                onChange={(ev) =>
                  edit(key, {
                    priority: (ev.target.value || undefined) as
                      | ApplicationRow["priority"],
                  })
                }
              >
                <option value="">{t("scheduling.priorityDefault")}</option>
                {table.priorities.map((p) => (
                  <option key={p} value={p}>
                    {t(`scheduling.priorityName.${p}`)}
                  </option>
                ))}
              </select>
            </label>
            {!isApi && (
              <>
                <h4>{t("scheduling.modelsOf")}</h4>
                <NameList
                  name={t("scheduling.modelsName", { row: name })}
                  items={row.models ?? []}
                  options={
                    followsAnswer
                      ? [
                          {
                            value: SAME_AS_ANSWER,
                            label: t("scheduling.sameAsAnswer"),
                          },
                        ]
                      : []
                  }
                  free
                  suggestions={modelNames}
                  idPrefix={`row-${key}-models`}
                  onChange={(models) => edit(key, { models })}
                />
              </>
            )}
            <h4>{t("scheduling.nodesOf")}</h4>
            <NameList
              name={t("scheduling.nodesName", { row: name })}
              items={row.nodes ?? []}
              options={nodeOptions}
              idPrefix={`row-${key}-nodes`}
              onChange={(nodes) => edit(key, { nodes })}
            />
            {isSystem && !isApi && (
              <p className="muted small">{t("scheduling.row.systemHint")}</p>
            )}
          </div>
        );
      })}

      <div className="row-actions">
        <button
          type="button"
          className="primary"
          disabled={busy || draft === null}
          onClick={() =>
            void run(
              () => api.putApplications(cleanRows(rows)),
              () => setDraft(null),
            )
          }
        >
          {t("scheduling.save")}
        </button>
        <button
          type="button"
          disabled={busy || draft === null}
          onClick={() => setDraft(null)}
        >
          {t("scheduling.discard")}
        </button>
      </div>

      <h2>{t("scheduling.rulesHeading")}</h2>
      <p>
        <label>
          {t("scheduling.embeddingLabel")}{" "}
          <select
            data-testid="scheduling-embedding"
            value={chosenEmbedding}
            onChange={(ev) => setEmbedding(ev.target.value)}
          >
            <option value="">{t("scheduling.embeddingAuto")}</option>
            {view.embedding_choices.map((m) => (
              <option key={m} value={m}>
                {m}
              </option>
            ))}
          </select>
        </label>{" "}
        <button
          type="button"
          disabled={busy || embedding === null}
          onClick={() =>
            void run(
              () => api.putScheduling({ embedding_model: chosenEmbedding }),
              () => setEmbedding(null),
            )
          }
        >
          {t("scheduling.saveRules")}
        </button>
      </p>
      <p className="muted small">{t("scheduling.embeddingHint")}</p>

      <div className="card" data-testid="scheduling-pause">
        <h3>{t("scheduling.pauseHeading")}</h3>
        <p role="status">
          {paused
            ? t("scheduling.pausedUntil", {
                time: formatDateTime(view.paused_until, locale) ?? "",
              })
            : t("scheduling.notPaused")}
        </p>
        <p className="muted small">{t("scheduling.pauseHint")}</p>
        <div className="row-actions">
          {PAUSES.map((minutes) => (
            <button
              key={minutes}
              type="button"
              disabled={busy}
              onClick={() => void run(() => api.pauseBackground(minutes))}
            >
              {t("scheduling.pauseFor", {
                label: t(`scheduling.minutes.${minutes}`),
              })}
            </button>
          ))}
          {paused && (
            <button
              type="button"
              disabled={busy}
              onClick={() => void run(() => api.pauseBackground(0))}
            >
              {t("scheduling.resume")}
            </button>
          )}
        </div>
      </div>

      <div className="card" data-testid="scheduling-manage">
        <h3>{t("scheduling.manage.heading")}</h3>
        <p className="muted small">
          {paused
            ? t("scheduling.manage.pausedHint")
            : t("scheduling.manage.hint")}
        </p>
        <div className="row-actions">
          {TASKS.map((task) => (
            <button
              key={task}
              type="button"
              disabled={busy || paused}
              title={t(`scheduling.manage.taskHint.${task}`)}
              onClick={() => void start(task)}
            >
              {t(`scheduling.manage.run.${task}`)}
            </button>
          ))}
        </div>
        {notice && <p role="status">{notice}</p>}
        <details data-testid="scheduling-history">
          <summary>
            {t("scheduling.history.heading", { count: view.history.length })}
          </summary>
          {view.history.length === 0 ? (
            <p className="muted small">{t("scheduling.history.empty")}</p>
          ) : (
            <ul className="history-list">
              {view.history.map((h) => (
                <li
                  key={`${h.ts}-${h.kind}-${h.detail}`}
                  className={h.ok ? undefined : "warn-text"}
                >
                  <time dateTime={h.ts}>
                    {formatDateTime(h.ts, locale) ?? h.ts}
                  </time>{" "}
                  {t(`scheduling.history.kind.${h.kind}`)}
                  {h.detail ? `: ${h.detail}` : ""} ({h.actor})
                  {h.ok ? "" : ` – ${t("scheduling.history.failed")}`}
                </li>
              ))}
            </ul>
          )}
        </details>
      </div>

      <div className="row-actions">
        <button
          type="button"
          disabled={busy || !view.customised}
          onClick={async () => {
            if (await askConfirm(t("scheduling.confirmReset")))
              void run(
                () => api.resetScheduling(),
                () => setDraft(null),
              );
          }}
        >
          {t("scheduling.reset")}
        </button>
      </div>
    </section>
  );
}
