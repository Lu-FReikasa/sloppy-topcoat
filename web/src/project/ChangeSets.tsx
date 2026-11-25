import { useCallback, useEffect, useState } from "react";
import { useTranslation } from "react-i18next";
import type { ApiClient } from "../api/client";
import type { ProjectChangeSet } from "./types";

type Preview = ProjectChangeSet["preview"][number];
type Op = Record<string, unknown>;

const str = (v: unknown): string => (typeof v === "string" ? v : "");

/** What one operation does, in words the person can check: the new title, the new text, the status
 *  before and after. The hub's preview carries `change` (field -> [before, after]) for operations
 *  on existing nodes; a node to be created is described by the operation itself. Older sets have
 *  no `change`; they fall back to the kinds of the Akte lines. */
function OperationDetail({ p, op }: { p: Preview; op: Op }) {
  const { t } = useTranslation();
  const status = (s: unknown) =>
    t(`project.taskStatuses.${str(s)}`, { defaultValue: str(s) || "–" });
  if (p.op === "create_node") {
    const body = str(op.body);
    return (
      <div className="project-setdetail">
        <p className="project-muted">
          {t(`project.setKinds.${str(op.kind)}`, { defaultValue: str(op.kind) })}
          {p.lines[0]?.node ? ` · ${p.lines[0].node}` : ""}
        </p>
        {body && <pre className="project-settext">{body}</pre>}
      </div>
    );
  }
  const change = p.change;
  if (change !== undefined) {
    const entries = Object.entries(change);
    if (entries.length === 0) {
      return <p className="project-setdetail project-warn">{t("project.setNoChange")}</p>;
    }
    return (
      <dl className="project-setdetail project-setchange">
        {entries.map(([field, [before, after]]) => (
          <div key={field}>
            <dt>{t(`project.setFields.${field}`, { defaultValue: field })}</dt>
            <dd>
              {field === "body" ? (
                <>
                  <pre className="project-settext">{str(after) || t("project.setEmptyText")}</pre>
                  {str(before) && (
                    <details>
                      <summary className="project-muted">{t("project.setBefore")}</summary>
                      <pre className="project-settext project-setold">{str(before)}</pre>
                    </details>
                  )}
                </>
              ) : field === "status" ? (
                <>
                  {status(before)} → <strong>{status(after)}</strong>
                </>
              ) : (
                <>
                  <s className="project-muted">{str(before) || "–"}</s> → <strong>{str(after)}</strong>
                </>
              )}
            </dd>
          </div>
        ))}
      </dl>
    );
  }
  const shown = p.lines.filter((l) => l.payload && Object.keys(l.payload).length > 0);
  if (p.lines.length > 0 && shown.length === 0 && ["update_node", "set_status"].includes(p.op)) {
    return <p className="project-setdetail project-warn">{t("project.setNoChange")}</p>;
  }
  return (
    <ul className="project-setlines">
      {p.lines.slice(0, 6).map((l, i) => (
        <li key={i} className="project-muted">
          {l.kind}
          {l.node ? ` · ${l.node}` : ""}
        </li>
      ))}
    </ul>
  );
}

/** One change set the agent prepared: what each operation would change, a box per operation, and
 *  one click for the rest (D15, D83). Unticking an operation that others need unticks them too (the
 *  hub says the same). Nothing happens before the click. */
export function ChangeSetCard({
  api,
  set,
  onDecided,
  onError,
}: {
  api: ApiClient;
  set: ProjectChangeSet;
  onDecided: (set: ProjectChangeSet, message: string) => void;
  onError: (err: unknown) => void;
}) {
  const { t } = useTranslation();
  const [ticked, setTicked] = useState<Set<number>>(
    () => new Set(set.preview.map((p) => p.index)),
  );
  const [busy, setBusy] = useState(false);
  const [problem, setProblem] = useState(false);
  const pending = set.status === "pending";

  /** An operation and, one after the other, everything that needs what it creates. */
  const closure = (i: number): number[] => {
    const seen = new Set<number>([i]);
    const queue = [i];
    while (queue.length > 0) {
      for (const d of set.dependants[String(queue.shift())] ?? []) {
        if (!seen.has(d)) {
          seen.add(d);
          queue.push(d);
        }
      }
    }
    return [...seen];
  };
  const toggle = (i: number) => {
    setProblem(false);
    setTicked((prev) => {
      const next = new Set(prev);
      if (next.has(i)) for (const j of closure(i)) next.delete(j);
      else next.add(i);
      return next;
    });
  };
  const decide = async (apply: boolean) => {
    if (apply && ticked.size === 0) return setProblem(true);
    setBusy(true);
    try {
      const done = apply
        ? await api.applyProjectChangeSet(
            set.id,
            [...ticked].sort((a, b) => a - b),
          )
        : await api.refuseProjectChangeSet(set.id);
      onDecided(
        done,
        apply ? t("project.setApplied") : t("project.setRefused"),
      );
    } catch (err) {
      onError(err);
    } finally {
      setBusy(false);
    }
  };
  const failure = set.status === "failed" ? str(set.result?.error ?? set.result?.message) : "";
  const superseded = set.status === "refused" && str(set.result?.superseded_by) !== "";
  return (
    <article className="project-set" data-status={set.status}>
      <header>
        <h3>{set.title || t("project.changeSetsTab")}</h3>
        <span className="project-muted">
          {t("project.setOperations", { count: set.preview.length })} ·{" "}
          {t(`project.setStatus.${set.status}`)}
        </span>
      </header>
      {set.summary && <p className="project-setsummary">{set.summary}</p>}
      {pending && <p className="project-muted">{t("project.changeSetHint")}</p>}
      <ol className="project-setops">
        {set.preview.map((p) => {
          const op = (set.operations[p.index] ?? {}) as Op;
          const needed = Object.entries(set.dependants)
            .filter(([, ds]) => ds.includes(p.index))
            .map(([k]) => Number(k) + 1);
          const verb = t(`project.setOps.${p.op}`, { defaultValue: p.op });
          const title = (p.op === "create_node" ? str(op.title) : "") || (p.target ?? "");
          const ref = p.ref ?? p.lines[0]?.node ?? null;
          return (
            <li key={p.index}>
              <label className="project-sethead">
                <input
                  type="checkbox"
                  checked={ticked.has(p.index)}
                  disabled={!pending}
                  onChange={() => toggle(p.index)}
                  aria-label={`${p.index + 1}. ${verb} ${title}`}
                />
                <span className="project-setverb">{verb}</span>
                <span className="project-settitle">
                  {title}
                  {ref && p.op !== "create_node" && (
                    <span className="project-muted"> · {ref}</span>
                  )}
                </span>
              </label>
              {needed.length > 0 && (
                <p className="project-muted project-setneeded">
                  {t("project.setNeeded", { numbers: needed.join(", ") })}
                </p>
              )}
              <OperationDetail p={p} op={op} />
            </li>
          );
        })}
      </ol>
      {failure && (
        <p className="project-error">{t("project.setFailed", { reason: failure })}</p>
      )}
      {superseded && <p className="project-muted">{t("project.setSuperseded")}</p>}
      {problem && (
        <p role="alert" className="project-error">
          {t("project.setNothingTicked")}
        </p>
      )}
      {pending && (
        <div className="project-actions">
          <button
            type="button"
            disabled={busy}
            onClick={() => void decide(true)}
          >
            {t("project.applySelected")}
          </button>
          <button
            type="button"
            disabled={busy}
            onClick={() => void decide(false)}
          >
            {t("project.refuseSet")}
          </button>
        </div>
      )}
    </article>
  );
}

/** The change sets of a project that wait for the person; the decided ones only on request. */
export function ChangeSets({
  api,
  projectId,
  tick,
  onDecided,
  onError,
}: {
  api: ApiClient;
  projectId: string;
  tick: number;
  onDecided: (message: string) => void;
  onError: (err: unknown) => void;
}) {
  const { t } = useTranslation();
  const [sets, setSets] = useState<ProjectChangeSet[] | null>(null);
  const [showDecided, setShowDecided] = useState(false);
  const load = useCallback(async () => {
    try {
      setSets(
        (await api.projectChangeSets({ project: projectId })).change_sets,
      );
    } catch (err) {
      onError(err);
    }
  }, [api, projectId, onError]);
  useEffect(() => {
    void Promise.resolve().then(load);
  }, [load, tick]);
  if (sets === null)
    return <p className="project-muted">{t("project.loading")}</p>;
  const open = sets.filter((s) => s.status === "pending" || s.status === "running");
  const decided = sets.filter((s) => !open.includes(s));
  const shown = showDecided ? [...open, ...decided] : open;
  return (
    <section className="project-sets" aria-label={t("project.changeSetsTab")}>
      {open.length === 0 && (
        <p className="project-muted">{t("project.changeSetsNoneOpen")}</p>
      )}
      {shown.map((s) => (
        <ChangeSetCard
          key={`${s.id}:${s.status}`}
          api={api}
          set={s}
          onError={onError}
          onDecided={(done, message) => {
            setSets((list) =>
              (list ?? []).map((x) => (x.id === done.id ? done : x)),
            );
            onDecided(message);
          }}
        />
      ))}
      {decided.length > 0 && (
        <button
          type="button"
          className="project-linkbutton"
          aria-expanded={showDecided}
          onClick={() => setShowDecided((v) => !v)}
        >
          {showDecided
            ? t("project.setsHideDecided")
            : t("project.setsShowDecided", { count: decided.length })}
        </button>
      )}
    </section>
  );
}
