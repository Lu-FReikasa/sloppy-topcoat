import { MessageBlock } from "./MessageBlock";
import { useState } from "react";
import { useTranslation } from "react-i18next";
import type { ApiClient } from "../api/client";
import { Markdown } from "../chat/Markdown";
import { ThemeCardContext } from "../chat/ThemeCard";
import { HistoryPane } from "./HistoryPane";
import { IterationPanel } from "./IterationPanel";
import { CheckOpenIssues, IssuePanel } from "./IssuePanel";
import { LabelsEditor, RelationsEditor, RemindersEditor } from "./NodeExtras";
import { LargeTaskPanel } from "./SubtaskView";
import { AppBadge } from "./AppBadge";
import { WikiTakeOver } from "./WikiTakeOver";
import { fromInputValue, toInputValue } from "./time";
import {
  hasSubtasks,
  isIssue,
  isSubtask,
  type ProjectNode,
  type ProjectNodePatch,
  type ProjectRepeatRule,
  type ProjectTaskStatus,
} from "./types";

const TASK_STATUSES: readonly ProjectTaskStatus[] = [
  "todo",
  "in_progress",
  "in_review",
  "blocked",
  "done",
  "dropped",
];
const UNITS = ["minute", "hour", "day", "week", "month", "year"] as const;

export type Patch = Omit<ProjectNodePatch, "revision">;

/** The repeat rule a unit and a count make; months and years stay on their day of the month. */
export function repeatRule(
  every: number,
  unit: ProjectRepeatRule["unit"],
  zone: string,
): ProjectRepeatRule {
  return {
    every,
    unit,
    mode: unit === "month" || unit === "year" ? "monthly" : "after",
    tz: zone,
  };
}

/** The selected node: its fields, labels, relations and reminders. A save sends only what changed,
 *  with the revision the page read; a stale one is refused by the hub and the page reloads. */
export function NodeDetail({
  api,
  node,
  nodes,
  zone,
  onSave,
  onMove,
  onAnswer,
  onComplete,
  onRun,
  onTrash,
  onChanged,
  onRestored,
  onError,
}: {
  api: ApiClient;
  node: ProjectNode;
  nodes: ProjectNode[];
  zone: string;
  onSave: (patch: Patch) => void;
  onMove: (parentId: string | null) => void;
  onAnswer: (answer: string) => void;
  onComplete: (node: ProjectNode) => void;
  onRun?: (node: ProjectNode) => void;
  onTrash: () => void;
  onChanged: () => void;
  onRestored: () => void;
  onError: (err: unknown) => void;
}) {
  const { t } = useTranslation();
  const [title, setTitle] = useState(node.title);
  const [body, setBody] = useState(node.body);
  // a result (a report) is read, not edited: it opens as it looks, a theme in it as a card to try
  const [preview, setPreview] = useState(node.kind === "report");
  const [answer, setAnswer] = useState("");
  const [parent, setParent] = useState<string>(node.parent_id ?? "");
  const [start, setStart] = useState(toInputValue(node.start_at, zone));
  const [due, setDue] = useState(toInputValue(node.due_at, zone));
  const [end, setEnd] = useState(toInputValue(node.end_at, zone));
  const [priority, setPriority] = useState(node.priority ?? 0);
  const [percent, setPercent] = useState(node.percent_done ?? 0);
  const [estimate, setEstimate] = useState(
    node.estimate_minutes == null ? "" : String(node.estimate_minutes),
  );
  const [milestone, setMilestone] = useState(!!node.is_milestone);
  const [favorite, setFavorite] = useState(!!node.favorite);
  const rule = node.repeat_rule as ProjectRepeatRule | null | undefined;
  const [repeatOn, setRepeatOn] = useState(!!rule);
  const [every, setEvery] = useState(rule?.every ?? 1);
  const [unit, setUnit] = useState<ProjectRepeatRule["unit"]>(
    rule?.unit ?? "week",
  );
  const movable = nodes.filter((n) => n.kind !== "report" && n.id !== node.id);
  const isTask = node.kind === "task";
  // the task whose iterations this node shows: itself, or the task a result belongs to
  const iterTask = isTask
    ? node
    : node.kind === "report"
      ? nodes.find((n) => n.id === node.parent_id && n.kind === "task")
      : undefined;

  const save = () => {
    const patch: Patch = {};
    if (title.trim() !== node.title) patch.title = title.trim();
    if (body !== node.body) patch.body = body;
    if (isTask) {
      const date = (value: string, was: string | null | undefined) =>
        value === toInputValue(was, zone)
          ? undefined
          : fromInputValue(value, zone);
      const s = date(start, node.start_at);
      const d = date(due, node.due_at);
      const e = date(end, node.end_at);
      if (s !== undefined) patch.start_at = s;
      if (d !== undefined) patch.due_at = d;
      if (e !== undefined) patch.end_at = e;
      if (priority !== (node.priority ?? 0)) patch.priority = priority;
      if (percent !== (node.percent_done ?? 0)) patch.percent_done = percent;
      const minutes = estimate === "" ? null : Number(estimate);
      if (minutes !== (node.estimate_minutes ?? null))
        patch.estimate_minutes = minutes;
      if (milestone !== !!node.is_milestone) patch.is_milestone = milestone;
      if (favorite !== !!node.favorite) patch.favorite = favorite;
      const changedRule = repeatOn
        ? !rule || rule.every !== every || rule.unit !== unit
        : !!rule;
      if (changedRule)
        patch.repeat_rule = repeatOn ? repeatRule(every, unit, zone) : null;
    }
    if (Object.keys(patch).length > 0) onSave(patch);
  };

  return (
    <div className="project-detail" aria-label={t("project.detail")}>
      <h3>
        <span className="project-number">{node.number}</span> {node.title}
        <AppBadge application={node.application} mode={node.mode} />
      </h3>
      <p className="project-muted">{node.ref}</p>
      <label>
        {t("project.nodeTitle")}
        <input
          value={title}
          onChange={(e) => setTitle(e.target.value)}
          maxLength={200}
        />
      </label>
      <div className="project-bodyfield">
        <div className="project-bar">
          <span id={`body-${node.id}`}>{t("project.body")}</span>
          <button
            type="button"
            aria-pressed={preview}
            onClick={() => setPreview((v) => !v)}
          >
            {preview ? t("project.edit") : t("project.preview")}
          </button>
        </div>
        {preview ? (
          <div className="project-preview" aria-labelledby={`body-${node.id}`}>
            <ThemeCardContext.Provider value={api}>
              <Markdown text={body} />
            </ThemeCardContext.Provider>
          </div>
        ) : (
          <textarea
            aria-labelledby={`body-${node.id}`}
            value={body}
            onChange={(e) => setBody(e.target.value)}
            rows={5}
          />
        )}
      </div>

      {isTask && node.status && (
        <>
          <label>
            {t("project.taskStatus")}
            <select
              value={node.status}
              onChange={(e) =>
                onSave({ status: e.target.value as ProjectTaskStatus })
              }
            >
              {TASK_STATUSES.map((s) => (
                <option key={s} value={s}>
                  {t(`project.taskStatuses.${s}`)}
                </option>
              ))}
            </select>
          </label>
          <fieldset className="project-extra">
            <legend>{t("project.dates")}</legend>
            {(
              [
                ["fieldStart", start, setStart],
                ["fieldDue", due, setDue],
                ["fieldEnd", end, setEnd],
              ] as const
            ).map(([key, value, set]) => (
              <div key={key} className="project-bar">
                <label>
                  {t(`project.${key}`)}
                  <input
                    type="datetime-local"
                    value={value}
                    onChange={(e) => set(e.target.value)}
                  />
                </label>
                <button type="button" onClick={() => set("")} disabled={!value}>
                  {t("project.clearDate")}
                </button>
              </div>
            ))}
            <label>
              {t("project.fieldPriority")}
              <select
                value={priority}
                onChange={(e) => setPriority(Number(e.target.value))}
              >
                {[0, 1, 2, 3, 4, 5].map((p) => (
                  <option key={p} value={p}>
                    {p} · {t(`project.priorityLevels.${p}`)}
                  </option>
                ))}
              </select>
            </label>
            <label>
              {t("project.fieldPercent")}
              <input
                type="number"
                min={0}
                max={100}
                value={percent}
                onChange={(e) => setPercent(Number(e.target.value))}
              />
            </label>
            <label>
              {t("project.fieldEstimate")}
              <input
                type="number"
                min={0}
                value={estimate}
                onChange={(e) => setEstimate(e.target.value)}
              />
            </label>
            <label className="project-bar">
              <input
                type="checkbox"
                checked={milestone}
                onChange={(e) => setMilestone(e.target.checked)}
              />
              {t("project.milestone")}
            </label>
            <label className="project-bar">
              <input
                type="checkbox"
                checked={favorite}
                onChange={(e) => setFavorite(e.target.checked)}
              />
              {t("project.favorite")}
            </label>
            <div className="project-bar">
              <label className="project-bar">
                <input
                  type="checkbox"
                  checked={repeatOn}
                  onChange={(e) => setRepeatOn(e.target.checked)}
                />
                {t("project.repeat")}
              </label>
              {repeatOn && (
                <>
                  <span>{t("project.repeatEvery")}</span>
                  <input
                    type="number"
                    min={1}
                    max={1000}
                    aria-label={t("project.repeatEvery")}
                    value={every}
                    onChange={(e) =>
                      setEvery(Math.max(1, Number(e.target.value)))
                    }
                  />
                  <select
                    aria-label={t("project.repeat")}
                    value={unit}
                    onChange={(e) => setUnit(e.target.value as typeof unit)}
                  >
                    {UNITS.map((u) => (
                      <option key={u} value={u}>
                        {t(`project.repeatUnits.${u}`)}
                      </option>
                    ))}
                  </select>
                </>
              )}
              {!repeatOn && (
                <span className="project-muted">{t("project.repeatNone")}</span>
              )}
            </div>
          </fieldset>
        </>
      )}
      <div className="project-actions">
        <button type="button" onClick={save}>
          {t("project.save")}
        </button>
        {isTask && node.status !== "done" && node.status !== "dropped" && (
          <button type="button" onClick={() => onComplete(node)}>
            {t("project.complete")}
          </button>
        )}
        {isTask &&
          onRun &&
          (node.status === "todo" || node.status === "blocked") && (
            <button type="button" onClick={() => onRun(node)}>
              {t("project.runNode")}
            </button>
          )}
      </div>

      {isIssue(node) && (
        <IssuePanel api={api} task={node} onChanged={onChanged} onError={onError} />
      )}
      {isTask && !isIssue(node) && !isSubtask(node) && (
        <LargeTaskPanel
          api={api}
          nodeId={node.id}
          open={hasSubtasks(node, nodes)}
          onChanged={onChanged}
        />
      )}
      {node.kind === "topic" && node.title === "Issues" && (
        <CheckOpenIssues
          api={api}
          projectId={node.project_id}
          onChanged={onChanged}
          onError={onError}
        />
      )}

      {iterTask && (
        <IterationPanel
          api={api}
          node={node}
          task={iterTask}
          onChanged={onChanged}
          onError={onError}
        />
      )}

      {node.kind !== "report" && (
        <LabelsEditor
          api={api}
          node={node}
          onChanged={onChanged}
          onError={onError}
        />
      )}
      <RelationsEditor
        api={api}
        node={node}
        onChanged={onChanged}
        onError={onError}
      />
      <RemindersEditor api={api} node={node} zone={zone} onError={onError} />

      <HistoryPane
        api={api}
        node={node}
        zone={zone}
        onRestored={onRestored}
        onError={onError}
      />

      <label>
        {t("project.moveTo")}
        <select value={parent} onChange={(e) => setParent(e.target.value)}>
          <option value="">{t("project.topLevel")}</option>
          {movable.map((n) => (
            <option key={n.id} value={n.id}>
              {n.number} {n.title}
            </option>
          ))}
        </select>
      </label>
      <div className="project-actions">
        <button type="button" onClick={() => onMove(parent || null)}>
          {t("project.move")}
        </button>
        <button type="button" onClick={onTrash}>
          {t("project.moveToTrash")}
        </button>
      </div>

      {node.kind === "report" && node.report_kind === "question" && (
        <form
          className="project-form"
          onSubmit={(e) => {
            e.preventDefault();
            if (answer.trim()) onAnswer(answer.trim());
          }}
        >
          <p>
            {node.answer
              ? `${t("project.answer")}: ${node.answer}`
              : t("project.unanswered")}
          </p>
          <input
            aria-label={t("project.answer")}
            placeholder={t("project.answerPlaceholder")}
            value={answer}
            onChange={(e) => setAnswer(e.target.value)}
          />
          <button type="submit" disabled={!answer.trim()}>
            {t("project.answer")}
          </button>
        </form>
      )}

      {node.kind === "report" && node.report_kind === "document" && (
        <WikiTakeOver api={api} nodeId={node.id} onError={onError} />
      )}

      <MessageBlock
        api={api}
        node={node}
        onChanged={onChanged}
        onError={onError}
      />
    </div>
  );
}
