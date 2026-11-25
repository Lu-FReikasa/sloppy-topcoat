// An issue's way from the cause to the hub that runs the fix (hub project_issues.py, issue_fixes.py):
// "Fix entwerfen" investigates in rounds with Dev and Verwaltung up to a report and a plan
// (issue_investigation.py); "Plan umsetzen" carries the settled plan out in steps (fix_steps.py);
// apply and test the draft in the workshop (one click on the whole set, D103), roll it out to the
// hub (a second click, D104), and say whether it works. The page follows a
// fix while the workshop or the host is busy with it.
import { useCallback, useEffect, useState } from "react";
import { useTranslation } from "react-i18next";
import type { ApiClient } from "../api/client";
import type { IssueFix, IssuePlan, ProjectMandate, ProjectNode } from "./types";

const BUSY = new Set(["applying", "testing", "queued"]);
// the shape a plan has (hub fix_steps.parse_plan): what the person fills in when no plan exists yet
const PLAN_TEMPLATE: IssuePlan = {
  summary: "",
  steps: [{ path: "web/src/…", symbol: "…", where: "in", do: "…" }],
  test: { path: "web/src/…/name.test.tsx", do: "…" },
};
const POLL_MS = 5000;

/** For each failed fix: how many fixes in a row up to it failed the same way (hub
 *  failure_print.py; the hub stops drafting after two). `fixes` oldest first. */
export function sameFailures(fixes: IssueFix[]): Map<string, number> {
  const out = new Map<string, number>();
  let prev: IssueFix | undefined;
  for (const f of fixes) {
    if (f.state === "failed" && f.failure_print) {
      const before = prev?.state === "failed" && prev.failure_print === f.failure_print ? (out.get(prev.id) ?? 1) : 0;
      out.set(f.id, before + 1);
    }
    prev = f;
  }
  return out;
}

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
  const repeats = sameFailures(shown);

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
          {shown.map((f) => (
            <li key={f.id} data-testid={`fix-${f.state}`}>
              <p>
                <span className={`badge fix-${f.state}`}>{t(`issues.state.${f.state}`)}</span> {f.summary}
                {(repeats.get(f.id) ?? 0) > 1 && (
                  <>
                    {" "}
                    <span className="badge fix-failed" title={f.failure_print ?? ""}>
                      {t("issues.sameFailure", { count: repeats.get(f.id) })}
                    </span>
                  </>
                )}
              </p>
              {f.error && <p className="error small">{f.error}</p>}
              <details>
                <summary>{t("issues.changes", { count: f.edits.length + f.files.length })}</summary>
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
              </details>
              {f.tests?.output && (
                <details>
                  <summary>{t("issues.tests", { state: f.tests.state ?? "?" })}</summary>
                  <pre className="project-diff">{f.tests.output}</pre>
                </details>
              )}
              {f.rollout?.message && (
                <p className="small">
                  {t("issues.rollout")}: {f.rollout.message}
                  {f.rollout.build ? ` (${f.rollout.build})` : ""}
                </p>
              )}
              <div className="project-actions">
                {f.state === "draft" && (
                  <button type="button" disabled={busy} onClick={() => void act(() => api.applyFix(f.id))}>
                    {t("issues.apply")}
                  </button>
                )}
                {f.state === "passed" && (
                  <button type="button" disabled={busy} onClick={() => void act(() => api.rolloutFix(f.id))}>
                    {t("issues.rolloutButton")}
                  </button>
                )}
                {["draft", "failed", "passed", "refused", "rolled_back"].includes(f.state) && (
                  <button type="button" disabled={busy} onClick={() => void act(() => api.discardFix(f.id))}>
                    {t("issues.discard")}
                  </button>
                )}
              </div>
            </li>
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
