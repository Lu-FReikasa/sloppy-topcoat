// The view of a large task's sub-tasks (D109, D110, issue #33): each sub-task with its state, what
// it needs first, its result and the next step, grouped into steps by its dependencies (the small
// graph); the branch of the whole task, the workshop scope's card, a manual "go on", and the
// roll-out click once every sub-task is done. It reads one call (`useLargeTask`).
import { useState } from "react";
import { useTranslation } from "react-i18next";
import type { ApiClient } from "../api/client";
import { useLargeTask, useLargeTaskActions, type LargeTaskNotice } from "../data/useLargeTask";
import type { HubEvents } from "../data/hubEvents";
import { describeError } from "../errors";
import type { Subtask } from "./types";

/** Sub-tasks that only read or wait for a setting of the person (hub vorhaben.READING_LAYERS,
 *  PERSON_LAYERS); every other layer writes in the workshop and needs its scope. */
const NOT_WRITING = new Set(["research", "admin"]);
/** How long the hub gives the workshop scope (hub vorhaben.DAYS, D110). */
export const SCOPE_DAYS = 14;

/** The sub-tasks in steps: a sub-task comes one step after the last of those it depends on, so
 *  what may run side by side stands together. Unknown ids and cycles count as no dependency. */
export function stagesOf(subtasks: readonly Subtask[]): Subtask[][] {
  const byId = new Map(subtasks.map((s) => [s.id, s]));
  const depth = new Map<string, number>();
  const depthOf = (s: Subtask, seen: ReadonlySet<string>): number => {
    const known = depth.get(s.id);
    if (known !== undefined) return known;
    const inner = new Set([...seen, s.id]);
    const d = s.depends_on
      .map((id) => byId.get(id))
      .filter((dep): dep is Subtask => dep !== undefined && !inner.has(dep.id))
      .reduce((most, dep) => Math.max(most, depthOf(dep, inner) + 1), 0);
    depth.set(s.id, d);
    return d;
  };
  const depths = subtasks.map((s) => depthOf(s, new Set()));
  const deepest = depths.reduce((most, d) => Math.max(most, d), -1);
  return Array.from({ length: deepest + 1 }, (_, d) => subtasks.filter((_s, i) => depths[i] === d)).filter(
    (stage) => stage.length > 0,
  );
}

/** The hub says the next step in German (vorhaben.next_step); the known phrases have keys. */
const NEXT: Record<string, string> = {
  fertig: "done",
  "wartet auf dich": "you",
  läuft: "running",
  "wird neu geplant": "replan",
  "nächster Entwurf": "draft",
  "eine Einstellung durch dich": "setting",
  "startet, wenn die Abhängigkeiten fertig sind": "dependencies",
};

export function nextKey(text: string): string | null {
  const key = NEXT[text.trim()];
  return key ? `largeTask.next.${key}` : null;
}

function Notice({ notice }: { notice: LargeTaskNotice }) {
  const { t } = useTranslation();
  if (notice.kind === "filed") return <p role="status">{t("largeTask.filed", { count: notice.count })}</p>;
  if (notice.kind === "changeSet") return <p role="status">{t("largeTask.changeSet")}</p>;
  if (notice.kind === "scope") return <p role="status">{t("largeTask.scopeGiven")}</p>;
  if (notice.kind === "revoked") return <p role="status">{t("largeTask.scopeRevoked")}</p>;
  const { tick } = notice;
  const parts = (["started", "replanned", "blocked", "stopped"] as const)
    .filter((k) => tick[k].length > 0)
    .map((k) => t(`largeTask.tick.${k}`, { ids: tick[k].join(", ") }));
  const said = parts.length > 0 ? parts.join(" ") : t("largeTask.tick.nothing");
  return <p role="status">{tick.done ? t("largeTask.tick.done") : said}</p>;
}

function SubtaskCard({ sub, numberOf }: { sub: Subtask; numberOf: Map<string, string> }) {
  const { t } = useTranslation();
  const next = nextKey(sub.next);
  const needs = sub.depends_on.map((id) => numberOf.get(id) ?? id);
  return (
    <li className={`subtask subtask-${sub.state}`} data-testid="subtask">
      <div className="subtask-head">
        <span className="subtask-number">{sub.number}</span> <strong>{sub.title}</strong>{" "}
        <span className={`badge subtask-badge-${sub.state}`}>{t(`largeTask.state.${sub.state}`)}</span>
      </div>
      {sub.goal && <p className="small">{sub.goal}</p>}
      <p className="small project-muted">
        {t(`largeTask.layer.${sub.layer}`, { defaultValue: sub.layer })}
        {needs.length > 0 && <> · {t("largeTask.needs", { list: needs.join(", ") })}</>}
      </p>
      {sub.result && (
        <p className="small">
          {t("largeTask.result")}: {t(`issues.state.${sub.result.state}`, { defaultValue: sub.result.state })}
          {sub.result.summary ? ` · ${sub.result.summary}` : ""}
        </p>
      )}
      {sub.result?.error && <p className="error small">{sub.result.error}</p>}
      {sub.next && (
        <p className="small">
          {t("largeTask.nextLabel")}: {next ? t(next) : sub.next}
        </p>
      )}
      {(sub.files.length > 0 || sub.new_files.length > 0 || sub.accept.length > 0) && (
        <details>
          <summary>{t("largeTask.details")}</summary>
          {(sub.files.length > 0 || sub.new_files.length > 0) && (
            <ul className="small subtask-files">
              {sub.files.map((f) => (
                <li key={f}>
                  <code>{f}</code>
                </li>
              ))}
              {sub.new_files.map((f) => (
                <li key={f}>
                  <code>{f}</code> ({t("issues.newFile")})
                </li>
              ))}
            </ul>
          )}
          {sub.accept.length > 0 && (
            <>
              <p className="small">{t("largeTask.accept")}</p>
              <ul className="small subtask-files">
                {sub.accept.map((a) => (
                  <li key={a}>
                    <code>{a}</code>
                  </li>
                ))}
              </ul>
            </>
          )}
        </details>
      )}
    </li>
  );
}

export function SubtaskView({
  api,
  nodeId,
  events,
  pollMs,
  onChanged,
  headed = true,
}: {
  api: Pick<
    ApiClient,
    "largeTask" | "decomposeLargeTask" | "giveWorkshopScope" | "tickLargeTask" | "revokeProjectMandate"
  >;
  nodeId: string;
  events?: HubEvents;
  pollMs?: number;
  onChanged?: () => void;
  /** Its own heading; off inside the folded panel, whose summary names it. */
  headed?: boolean;
}) {
  const { t, i18n } = useTranslation();
  const task = useLargeTask(api, nodeId, { events, pollMs });
  const actions = useLargeTaskActions(api, nodeId, task, onChanged);
  const data = task.data;

  if (!data) {
    return (
      <section className="large-task" aria-label={t("largeTask.title")}>
        {headed && <h4>{t("largeTask.title")}</h4>}
        {task.error ? (
          <p className="error">{describeError(task.error, t, i18n)}</p>
        ) : (
          <p className="project-muted">{t("largeTask.loading")}</p>
        )}
      </section>
    );
  }

  const { large_task: large, subtasks, rollout } = data;
  const numberOf = new Map(subtasks.map((s) => [s.id, s.number]));
  const stages = stagesOf(subtasks);
  const writes = subtasks.some((s) => !NOT_WRITING.has(s.layer) && s.state !== "done");
  const scopeActive = large.scope?.state === "active";
  const busy = actions.pending !== null;
  const done = subtasks.filter((s) => s.state === "done").length;
  const scopeLines = {
    active: () => t("largeTask.scopeActive", { until: new Date(large.scope?.expires_at ?? "").toLocaleString(i18n.language) }),
    ended: () => t("largeTask.scopeEnded"),
    none: () => t("largeTask.scopeMissing"),
  };
  const scopeLine = scopeLines[large.scope?.state ?? "none"]();

  return (
    <section className="large-task" aria-label={t("largeTask.title")} data-testid="large-task">
      {headed && <h4>{t("largeTask.title")}</h4>}
      <p className="small">
        <span className={`badge large-task-badge-${large.state}`}>{t(`largeTask.overall.${large.state}`)}</span>{" "}
        {subtasks.length > 0 && t("largeTask.progress", { done, count: subtasks.length })}
      </p>
      <p className="small">
        {t("largeTask.branch")}: <code>{large.branch}</code>
      </p>

      {large.state === "planning" ? (
        <div className="project-form">
          <p className="small">{t("largeTask.planningHint")}</p>
          <div className="project-actions">
            <button type="button" disabled={busy} onClick={() => void actions.decompose()}>
              {actions.pending === "decompose" ? t("largeTask.decomposing") : t("largeTask.decompose")}
            </button>
          </div>
        </div>
      ) : (
        <ol className="subtask-stages">
          {stages.map((stage, i) => (
            <li key={i} className="subtask-stage">
              <p className="subtask-stage-title">{t("largeTask.stage", { n: i + 1 })}</p>
              <ul className="subtask-list">
                {stage.map((sub) => (
                  <SubtaskCard key={sub.id} sub={sub} numberOf={numberOf} />
                ))}
              </ul>
            </li>
          ))}
        </ol>
      )}

      {large.state !== "planning" && writes && (
        // the card of the workshop scope (D110): the whole scope is shown before the click, and
        // only the signed-in admin's click gives it; the hub sets branch, actions and duration
        <div className="project-form large-task-scope" data-testid="workshop-scope">
          <p>
            <strong>{t("largeTask.scopeTitle")}</strong>{" "}
            {scopeLine}
          </p>
          {!scopeActive && (
            <p className="small">
              {t("largeTask.scopeText", { number: large.number, title: large.title, branch: large.branch, days: SCOPE_DAYS })}
            </p>
          )}
          <div className="project-actions">
            {scopeActive && large.scope ? (
              <button type="button" disabled={busy} onClick={() => void actions.revokeScope(large.scope!.id)}>
                {t("largeTask.scopeRevoke")}
              </button>
            ) : (
              <button type="button" disabled={busy} onClick={() => void actions.giveScope()}>
                {t("largeTask.scopeGive")}
              </button>
            )}
          </div>
        </div>
      )}

      {(large.state === "running" || large.state === "blocked") && (
        <div className="project-actions">
          <button type="button" disabled={busy} onClick={() => void actions.tick()}>
            {t("largeTask.tickButton")}
          </button>
          <span className="small project-muted">{t("largeTask.tickHint")}</span>
        </div>
      )}

      {rollout.ready && (
        <div className="project-form" data-testid="large-task-rollout">
          <p>{t("largeTask.rolloutReady")}</p>
          <div className="project-actions">
            <button type="button" disabled aria-describedby={`rollout-note-${large.id}`}>
              {t("largeTask.rolloutButton")}
            </button>
          </div>
          <p className="small project-muted" id={`rollout-note-${large.id}`}>
            {t("largeTask.rolloutLater")}
          </p>
        </div>
      )}

      {actions.notice && <Notice notice={actions.notice} />}
      {actions.problems.length > 0 && (
        <div role="alert" className="error">
          <p>{t("largeTask.refused")}</p>
          <ul className="small">
            {actions.problems.map((p, i) => (
              <li key={i}>{p}</li>
            ))}
          </ul>
        </div>
      )}
      {actions.error && <p role="alert" className="error">{describeError(actions.error, t, i18n)}</p>}
      {task.error && <p className="error small">{describeError(task.error, t, i18n)}</p>}
    </section>
  );
}

/** On a task in Projects: the sub-task view, open when the task has sub-tasks, otherwise folded
 *  (it loads only when opened, so selecting a plain task costs no request). */
export function LargeTaskPanel({
  api,
  nodeId,
  open,
  onChanged,
}: {
  api: Parameters<typeof SubtaskView>[0]["api"];
  nodeId: string;
  open: boolean;
  onChanged?: () => void;
}) {
  const { t } = useTranslation();
  const [unfolded, setUnfolded] = useState(false);
  if (open) return <SubtaskView api={api} nodeId={nodeId} onChanged={onChanged} />;
  return (
    <details className="large-task-folded" onToggle={(e) => setUnfolded(e.currentTarget.open)}>
      <summary>{t("largeTask.title")}</summary>
      {unfolded && <SubtaskView api={api} nodeId={nodeId} onChanged={onChanged} headed={false} />}
    </details>
  );
}
