// An issue's way from the cause to the hub that runs the fix (hub project_issues.py, issue_fixes.py):
// "Fix entwerfen" investigates in rounds with Dev and Verwaltung up to a report and a plan
// (issue_investigation.py); "Plan umsetzen" carries the settled plan out in steps (fix_steps.py);
// apply and test the draft in the workshop (one click on the whole set, D103), roll it out to the
// hub (a second click, D104), and say whether it works. The page follows a
// fix while the workshop or the host is busy with it.
import { useCallback, useEffect, useState } from "react";
import { useTranslation } from "react-i18next";
import type { ApiClient } from "../api/client";
import { formatInt } from "../format";
import { summarizeTestOutput } from "../summaries";
import { fixHistory, fixReason, shortPrint, type FixRow } from "./fixHistory";
import type { IssueFix, IssuePlan, ProjectMandate, ProjectNode } from "./types";

const BUSY = new Set(["applying", "testing", "queued"]);
// the shape a plan has (hub fix_steps.parse_plan): what the person fills in when no plan exists yet
const PLAN_TEMPLATE: IssuePlan = {
  summary: "",
  steps: [{ path: "web/src/…", symbol: "…", where: "in", do: "…" }],
  test: { path: "web/src/…/name.test.tsx", do: "…" },
};
const POLL_MS = 5000;

export function IssuePanel({
  api,
  task,
  onChanged,
  onError,
}: {
  api: ApiClient;
  task: ProjectNode;
  onChanged: () => void;
  onError: (err: unknown) => void;
}) {
  const { t } = useTranslation();
  const [fixes, setFixes] = useState<IssueFix[]>([]);
  const [plan, setPlan] = useState<IssuePlan | null>(null);
  const [probes, setProbes] = useState<ProjectMandate | null>(null);
  const [probeScope, setProbeScope] = useState("");
  const [hint, setHint] = useState("");
  const [busy, setBusy] = useState(false);
  const [notWorking, setNotWorking] = useState("");
  // "Plan vorgeben" (hub RunService.given_plan): the person writes the plan, the planner is left out
  const [ownPlan, setOwnPlan] = useState<string | null>(null);

  const load = useCallback(async () => {
    try {
      const out = await api.issueFixes(task.id);
      setFixes(out.fixes);
      setPlan(out.plan ?? null);
      setProbes(out.probe_mandate ?? null);
      setProbeScope(out.probe_scope ?? "");
    } catch (err) {
      onError(err);
    }
  }, [api, task.id, onError]);

  useEffect(() => {
    // eslint-disable-next-line react-hooks/set-state-in-effect -- load() sets the state after an await, not synchronously
    void load();
  }, [load, task.revision]);

  const following = fixes.some((f) => BUSY.has(f.state));
  useEffect(() => {
    if (!following) return;
    const timer = window.setInterval(() => void load(), POLL_MS);
    return () => window.clearInterval(timer);
  }, [following, load]);

  const act = async (run: () => Promise<unknown>) => {
    setBusy(true);
    try {
      await run();
      await load();
      onChanged();
    } catch (err) {
      onError(err);
    } finally {
      setBusy(false);
    }
  };

  const running = task.status === "in_progress";
  const shown = fixes.filter((f) => f.state !== "discarded");
  const live = shown.some((f) => f.state === "live");
  const history = fixHistory(shown);

  return (
    <section className="project-issue" aria-label={t("issues.panel")}>
      <h4>{t("issues.panel")}</h4>
      <div className="project-form">
        <label>
          {t("issues.hint")}
          <textarea rows={2} value={hint} maxLength={4000} onChange={(e) => setHint(e.target.value)} />
        </label>
        <div className="project-actions">
          <button
            type="button"
            disabled={busy || running}
            onClick={() =>
              void act(async () => {
                await api.iterateProjectNode(task.id, { feedback: hint.trim(), keep: false, program: "issue_investigate" });
                setHint("");
              })
            }
          >
            {t("issues.draftFix")}
          </button>
          <button
            type="button"
            disabled={busy || running}
            onClick={() => setOwnPlan(ownPlan === null ? JSON.stringify(plan ?? PLAN_TEMPLATE, null, 2) : null)}
          >
            {t("issues.ownPlan")}
          </button>
        </div>
      </div>
      {ownPlan !== null && (
        <div className="project-form" data-testid="issue-own-plan">
          <label>
            {t("issues.ownPlanHelp")}
            <textarea
              rows={12}
              value={ownPlan}
              maxLength={20000}
              spellCheck={false}
              onChange={(e) => setOwnPlan(e.target.value)}
            />
          </label>
          <div className="project-actions">
            <button
              type="button"
              disabled={busy || running}
              onClick={() => {
                let given: IssuePlan;
                try {
                  given = JSON.parse(ownPlan) as IssuePlan;
                } catch {
                  onError(new Error(t("issues.ownPlanInvalid")));
                  return;
                }
                void act(async () => {
                  await api.giveIssuePlan(task.id, given);
                  setOwnPlan(null);
                });
              }}
            >
              {t("issues.ownPlanSave")}
            </button>
          </div>
        </div>
      )}
      {running && <p className="project-muted small">{t("issues.running")}</p>}

      {probeScope && (
        // the scope card of the investigation's tries (hub issue_probes.py, D83): the whole scope
        // is shown before the click, and the click is the only way a probe mandate comes to be
        <div className="project-form" data-testid="issue-probes">
          <p>
            <strong>{t("issues.probes")}</strong>{" "}
            {probes ? t("issues.probesActive", { until: new Date(probes.expires_at).toLocaleString(), uses: probes.uses }) : ""}
          </p>
          <p className="small">{probeScope}</p>
          <div className="project-actions">
            {probes ? (
              <button type="button" disabled={busy} onClick={() => void act(() => api.revokeProjectMandate(probes.id))}>
                {t("issues.probesRevoke")}
              </button>
            ) : (
              <button type="button" disabled={busy} onClick={() => void act(() => api.allowProbes(task.id))}>
                {t("issues.probesAllow")}
              </button>
            )}
          </div>
        </div>
      )}

      {plan && !running && (
        <div className="project-form" data-testid="issue-plan">
          <p>
            <strong>{t("issues.plan")}</strong> {plan.summary}
          </p>
          <ol className="small">
            {plan.steps.map((s, i) => (
              <li key={i}>
                <code>{s.path}</code>
                {s.symbol ? (
                  <>
                    {" "}
                    <code>{s.symbol}</code>
                  </>
                ) : null}
                : {s.do}
              </li>
            ))}
            <li>
              <code>{plan.test.path}</code> ({t("issues.newFile")}): {plan.test.do}
            </li>
          </ol>
          <div className="project-actions">
            <button
              type="button"
              disabled={busy}
              onClick={() => void act(() => api.iterateProjectNode(task.id, { feedback: "", keep: false, program: "issue_plan" }))}
            >
              {t("issues.runPlan")}
            </button>
          </div>
        </div>
      )}

      {shown.length > 0 && (
        <ol className="project-fixes">
          {history.map((row) => (
            <FixHistoryRow
              key={row.fix.id}
              row={row}
              busy={busy}
              onApply={(id) => void act(() => api.applyFix(id))}
              onRollout={(id) => void act(() => api.rolloutFix(id))}
              onDiscard={(id) => void act(() => api.discardFix(id))}
            />
          ))}
        </ol>
      )}

      {live && task.status !== "done" && (
        <div className="project-form">
          <p>{t("issues.confirmQuestion")}</p>
          <div className="project-actions">
            <button type="button" disabled={busy} onClick={() => void act(() => api.confirmIssue(task.id, { works: true }))}>
              {t("issues.works")}
            </button>
          </div>
          <label>
            {t("issues.stillFails")}
            <textarea rows={2} value={notWorking} maxLength={4000} onChange={(e) => setNotWorking(e.target.value)} />
          </label>
          <button
            type="button"
            disabled={busy || running}
            onClick={() => void act(() => api.confirmIssue(task.id, { works: false, feedback: notWorking.trim() }))}
          >
            {t("issues.notWorks")}
          </button>
        </div>
      )}
    </section>
  );
}

const DISCARDABLE = new Set(["draft", "failed", "passed", "refused", "rolled_back"]);

/** One row of the fix history (#180): state, fingerprint and reason in view, the output folded;
 *  a row that stands for several equal attempts says how many and keeps the older ones folded. */
function FixHistoryRow({
  row,
  busy,
  onApply,
  onRollout,
  onDiscard,
}: {
  row: FixRow;
  busy: boolean;
  onApply: (id: string) => void;
  onRollout: (id: string) => void;
  onDiscard: (id: string) => void;
}) {
  const { t } = useTranslation();
  const f = row.fix;
  const print = shortPrint(f.failure_print);
  const reason = fixReason(f);
  // the whole error goes into the fold only when the one sentence above leaves something out
  const fullError = f.error && f.error.trim() !== reason ? f.error : "";
  return (
    <li className="project-fix" data-testid={`fix-${f.state}`}>
      <p className="project-fix-head">
        <span className={`badge fix-${f.state}`}>{t(`issues.state.${f.state}`)}</span>
        {row.times > 1 && (
          <>
            {" "}
            <span className="badge fix-failed" title={f.failure_print ?? ""}>
              {t("issues.sameFailure", { count: row.times })}
            </span>
          </>
        )}{" "}
        {f.summary}
      </p>
      {(print || reason) && (
        <p className="project-fix-why small" data-testid="fix-why">
          {print && (
            <code className="project-fix-print" title={f.failure_print ?? ""}>
              {print}
            </code>
          )}
          {print && reason && " "}
          {reason && <span className={f.error ? "error" : undefined}>{reason}</span>}
        </p>
      )}
      <details className="project-fix-output">
        <summary>{t("issues.output")}</summary>
        {fullError && <pre className="project-diff">{fullError}</pre>}
        <p className="small">
          <strong>{t("issues.changes", { count: f.edits.length + f.files.length })}</strong>
        </p>
        {f.diff ? (
          <pre className="project-diff">{f.diff}</pre>
        ) : (
          <ul className="small">
            {f.edits.map((e, i) => (
              <li key={i}>
                <code>{e.path}</code>
              </li>
            ))}
            {f.files.map((e) => (
              <li key={e.path}>
                <code>{e.path}</code> ({t("issues.newFile")})
              </li>
            ))}
          </ul>
        )}
        {f.tests?.output && <TestRun state={f.tests.state} output={f.tests.output} />}
        {f.rollout?.message && (
          <p className="small">
            {t("issues.rollout")}: {f.rollout.message}
            {f.rollout.build ? ` (${f.rollout.build})` : ""}
          </p>
        )}
        {row.earlier.length > 0 && (
          <div data-testid="fix-earlier">
            <p className="small">
              <strong>{t("issues.earlier", { count: row.earlier.length })}</strong>
            </p>
            <ul className="small">
              {row.earlier.map((e) => (
                <li key={e.id}>
                  {e.summary}{" "}
                  {DISCARDABLE.has(e.state) && (
                    <button type="button" className="link" disabled={busy} onClick={() => onDiscard(e.id)}>
                      {t("issues.discard")}
                    </button>
                  )}
                </li>
              ))}
            </ul>
          </div>
        )}
      </details>
      <div className="project-actions">
        {f.state === "draft" && (
          <button type="button" disabled={busy} onClick={() => onApply(f.id)}>
            {t("issues.apply")}
          </button>
        )}
        {f.state === "passed" && (
          <button type="button" disabled={busy} onClick={() => onRollout(f.id)}>
            {t("issues.rolloutButton")}
          </button>
        )}
        {DISCARDABLE.has(f.state) && (
          <button type="button" disabled={busy} onClick={() => onDiscard(f.id)}>
            {t("issues.discard")}
          </button>
        )}
      </div>
    </li>
  );
}

/** On the topic "Issues": every open issue without a result, checked as one batch. */
export function CheckOpenIssues({
  api,
  projectId,
  onChanged,
  onError,
}: {
  api: ApiClient;
  projectId: string;
  onChanged: () => void;
  onError: (err: unknown) => void;
}) {
  const { t } = useTranslation();
  const [note, setNote] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  return (
    <div className="project-actions">
      <button
        type="button"
        disabled={busy}
        onClick={() => {
          setBusy(true);
          api
            .checkOpenIssues(projectId)
            .then((out) => {
              setNote(t("issues.checkOpenStarted", { count: out.count }));
              onChanged();
            })
            .catch(onError)
            .finally(() => setBusy(false));
        }}
      >
        {t("issues.checkOpen")}
      </button>
      {note && <span role="status">{note}</span>}
    </div>
  );
}

/** A fix's test run: the counts and the first failing test first, the whole log one click away. */
function TestRun({ state, output }: { state: string | undefined; output: string }) {
  const { t, i18n } = useTranslation();
  const { counts, firstFailure } = summarizeTestOutput(output);
  const n = (count: number) => formatInt(count, i18n.language);
  const parts = counts
    ? [
        t("issues.testsPassed", { n: n(counts.passed) }),
        t("issues.testsFailed", { n: n(counts.failed) }),
        ...(counts.errors > 0 ? [t("issues.testsErrored", { n: n(counts.errors) })] : []),
        ...(counts.skipped > 0 ? [t("issues.testsSkipped", { n: n(counts.skipped) })] : []),
      ]
    : [];
  // a red run whose log names no failing test (lint, type check, a crash, or the hub's summary
  // cut off): "0 failed" alone would read as a contradiction
  const unexplained =
    state === "failed" && !firstFailure && (!counts || counts.failed + counts.errors === 0);
  return (
    <div className="test-run" data-testid="test-run">
      <p className="small">
        <strong>{t("issues.tests", { state: state ?? "?" })}</strong>
        {parts.length > 0 && ` · ${parts.join(", ")}`}
      </p>
      {firstFailure && (
        <p className="small test-first-failure">
          {t("issues.firstFailure")} <code>{firstFailure.test}</code>
          {firstFailure.error && (
            <>
              {" – "}
              <span className="error">{firstFailure.error}</span>
            </>
          )}
        </p>
      )}
      {unexplained && <p className="small">{t("issues.testsNoFailure")}</p>}
      <details>
        <summary>{t("issues.testOutput")}</summary>
        <pre className="project-diff">{output}</pre>
      </details>
    </div>
  );
}
