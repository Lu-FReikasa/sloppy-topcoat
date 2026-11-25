// Iterations of a task (hub: project_iterations.py, 2026-11-17). A task is the fixed order; every
// run of it is an iteration whose result knows the version it continued and the feedback it
// answers. The task keeps requirements (what every further iteration must hold), which the person
// adds and strikes out. The next iteration is one click with the feedback (D15: the person's).
import { useCallback, useEffect, useState } from "react";
import { useTranslation } from "react-i18next";
import type { ApiClient } from "../api/client";
import { requestOpen } from "../layout/intent";
import type { ProjectIterations, ProjectNode } from "./types";

export const WORKSHOP_MODULE = "page.theme-workshop";

/** The panel below a task (its iterations and requirements) or below one of its results (the
 *  iteration it is, and "go on from here"). */
export function IterationPanel({
  api,
  node,
  task,
  onChanged,
  onError,
}: {
  api: ApiClient;
  node: ProjectNode;
  /** the task the node is (a task) or belongs to (a result) */
  task: ProjectNode;
  onChanged: () => void;
  onError: (err: unknown) => void;
}) {
  const { t } = useTranslation();
  const [data, setData] = useState<ProjectIterations | null>(null);
  const [feedback, setFeedback] = useState("");
  const [keep, setKeep] = useState(true);
  const [base, setBase] = useState<string>(node.kind === "report" ? node.id : "");
  const [newReq, setNewReq] = useState("");
  const [busy, setBusy] = useState(false);
  const [started, setStarted] = useState(false);

  const load = useCallback(async () => {
    try {
      setData(await api.projectIterations(task.id));
    } catch (err) {
      onError(err);
    }
  }, [api, task.id, onError]);
  useEffect(() => {
    // eslint-disable-next-line react-hooks/set-state-in-effect -- load() sets the state after an await, not synchronously
    void load();
  }, [load, task.revision]);

  const isResult = node.kind === "report";
  const iterations = data?.iterations ?? [];
  const running = task.status === "in_progress" || !!data?.iterating;
  const mine = isResult ? iterations.find((i) => i.id === node.id) : undefined;
  const themeHere = isResult ? !!mine?.theme : iterations.some((i) => i.theme);

  async function iterate() {
    setBusy(true);
    try {
      await api.iterateProjectNode(task.id, {
        based_on: base || null,
        feedback: feedback.trim(),
        keep: keep && !!feedback.trim(),
      });
      setFeedback("");
      setStarted(true);
      onChanged();
    } catch (err) {
      onError(err);
    } finally {
      setBusy(false);
    }
  }

  async function putRequirements(items: (string | { id: string; text: string })[]) {
    try {
      await api.putProjectRequirements(task.id, { revision: task.revision, items });
      onChanged();
    } catch (err) {
      onError(err);
    }
  }

  const requirements = data?.requirements ?? task.requirements ?? [];
  const openWorkshop = () => {
    const from = isResult ? node.id : (iterations.filter((i) => i.theme).at(-1)?.id ?? "");
    requestOpen({ module: WORKSHOP_MODULE, props: { task: task.id, base: from } });
  };

  if (task.kind !== "task") return null;
  return (
    <section className="project-iterations" aria-label={t("iterations.title")}>
      <h4>
        {isResult && mine?.iteration
          ? t("iterations.thisOne", { n: mine.iteration })
          : t("iterations.title")}
      </h4>
      {isResult && mine && (
        <p className="project-muted small">
          {mine.based_on
            ? t("iterations.basedOn", { number: mine.based_on })
            : t("iterations.fromScratch")}
          {mine.feedback ? ` · ${t("iterations.answers")}: „${mine.feedback}“` : ""}
        </p>
      )}

      {!isResult && iterations.length > 0 && (
        <ol className="project-iteration-list">
          {iterations.map((i) => (
            <li key={i.id}>
              <strong>v{i.iteration ?? "?"}</strong> <span className="project-number">{i.number}</span>
              {i.based_on && <span className="project-muted"> ← {i.based_on}</span>}
              {i.feedback && <span className="project-muted"> · „{i.feedback}“</span>}
            </li>
          ))}
        </ol>
      )}

      <div className="project-requirements">
        <span>{t("iterations.requirements")}</span>
        {requirements.length === 0 && (
          <p className="project-muted small">{t("iterations.noRequirements")}</p>
        )}
        <ul>
          {requirements.map((r) => (
            <li key={r.id}>
              {r.text}
              {r.from ? <span className="project-muted"> (v{r.from})</span> : null}{" "}
              <button
                type="button"
                className="link"
                aria-label={t("iterations.strike", { text: r.text })}
                onClick={() => void putRequirements(requirements.filter((x) => x.id !== r.id))}
              >
                ×
              </button>
            </li>
          ))}
        </ul>
        <form
          className="project-bar"
          onSubmit={(e) => {
            e.preventDefault();
            if (!newReq.trim()) return;
            void putRequirements([...requirements, newReq.trim()]);
            setNewReq("");
          }}
        >
          <input
            aria-label={t("iterations.addRequirement")}
            placeholder={t("iterations.addRequirement")}
            value={newReq}
            maxLength={400}
            onChange={(e) => setNewReq(e.target.value)}
          />
          <button type="submit" disabled={!newReq.trim()}>
            {t("iterations.add")}
          </button>
        </form>
      </div>

      {(iterations.length > 0 || isResult) && (
        <form
          className="project-form"
          onSubmit={(e) => {
            e.preventDefault();
            void iterate();
          }}
        >
          <label>
            {t("iterations.feedback")}
            <textarea
              rows={3}
              value={feedback}
              maxLength={4000}
              placeholder={t("iterations.feedbackPlaceholder")}
              onChange={(e) => setFeedback(e.target.value)}
            />
          </label>
          {!isResult && (
            <label>
              {t("iterations.continueFrom")}
              <select value={base} onChange={(e) => setBase(e.target.value)}>
                <option value="">{t("iterations.newest")}</option>
                {iterations.map((i) => (
                  <option key={i.id} value={i.id}>
                    v{i.iteration ?? "?"} · {i.number}
                  </option>
                ))}
              </select>
            </label>
          )}
          <label className="project-bar">
            <input type="checkbox" checked={keep} onChange={(e) => setKeep(e.target.checked)} />
            {t("iterations.keep")}
          </label>
          <div className="project-actions">
            <button type="submit" disabled={busy || running}>
              {isResult ? t("iterations.goOnFromHere") : t("iterations.next")}
            </button>
            {themeHere && (
              <button type="button" onClick={openWorkshop}>
                {t("iterations.openWorkshop")}
              </button>
            )}
          </div>
          {running && <p className="project-muted small">{t("iterations.running")}</p>}
          {started && !running && <p role="status">{t("iterations.started")}</p>}
        </form>
      )}
    </section>
  );
}
