import { useCallback, useEffect, useState } from "react";
import { useTranslation } from "react-i18next";
import { UnauthorizedError, type ApiClient } from "../api/client";
import { describeError, toActionError } from "../errors";
import { formatDateTime, formatInt } from "../format";
import { tasksHref } from "../route";
import type {
  AgentEvidence,
  AgentStep,
  AgentTaskDetail,
  Finding,
} from "../types";
import { ACTIVE, canContinue, evidenceId, said } from "./agent";
import { Proposals } from "./Proposals";

interface Props {
  api: ApiClient;
  id: string;
  tick: number;
  onChanged: () => void;
}

/** One task: its progress as it happens, the diagnosis with tagged findings, the evidence. */
export function TaskView({ api, id, tick, onChanged }: Props) {
  const { t, i18n } = useTranslation();
  const [task, setTask] = useState<AgentTaskDetail | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const load = useCallback(() => {
    api.agentTask(id).then(
      (d) => {
        setTask(d);
        setError(null);
      },
      (err: unknown) => {
        if (!(err instanceof UnauthorizedError))
          setError(describeError(toActionError(err), t, i18n));
      },
    );
  }, [api, id, t, i18n]);

  useEffect(() => {
    load();
  }, [load, tick]);

  const running = task !== null && ACTIVE.includes(task.status);
  useEffect(() => {
    if (!running) return;
    const timer = setInterval(load, 3000);
    return () => clearInterval(timer);
  }, [running, load]);

  async function act(kind: "cancel" | "continue") {
    setBusy(true);
    setError(null);
    try {
      if (kind === "cancel") await api.cancelAgentTask(id);
      else await api.continueAgentTask(id);
      load();
      onChanged();
    } catch (err) {
      if (!(err instanceof UnauthorizedError))
        setError(describeError(toActionError(err), t, i18n));
    } finally {
      setBusy(false);
    }
  }

  if (!task) {
    return (
      <section>
        <p>
          <a href={tasksHref()}>← {t("agent.back")}</a>
        </p>
        {error && (
          <p className="error" role="alert">
            {error}
          </p>
        )}
      </section>
    );
  }

  const locale = i18n.language;
  return (
    <section className="task">
      <p>
        <a href={tasksHref()}>← {t("agent.back")}</a>
      </p>
      <div className="detail-head">
        <h1>{task.title}</h1>
        <span className={`badge badge-task-${task.status}`}>
          {t(`agent.status.${task.status}`)}
        </span>
      </div>
      <p className="muted small">
        {t("agent.progress", {
          steps: task.step_count,
          calls: task.tool_calls,
          runs: task.runs,
        })}
        {task.model ? ` · ${t("agent.model", { model: task.model })}` : ""}
        {task.created_at ? ` · ${formatDateTime(task.created_at, locale)}` : ""}
      </p>
      <div className="dialog-actions">
        {running && (
          <button
            type="button"
            disabled={busy}
            onClick={() => void act("cancel")}
          >
            {busy ? t("agent.canceling") : t("agent.cancel")}
          </button>
        )}
        {canContinue(task.status, task.stop_reason) && (
          <button
            type="button"
            className="primary"
            disabled={busy}
            onClick={() => void act("continue")}
          >
            {t("agent.continue")}
          </button>
        )}
      </div>
      {error && (
        <p className="error" role="alert">
          {error}
        </p>
      )}
      {task.stop_reason && (
        <p className="notice">
          {t(`agent.stop.${task.stop_reason}`, {
            defaultValue: task.stop_reason,
          })}
        </p>
      )}
      {task.status === "failed" && task.error && (
        <p className="error" role="alert">
          {t("agent.error", { message: task.error })}
        </p>
      )}
      {running && (
        <p className="notice" role="status">
          {t("agent.working")}
        </p>
      )}

      <h2>{t("agent.prompt")}</h2>
      <p className="task-prompt">{task.prompt}</p>

      {task.kind === "coordinator" && (task.children ?? []).length > 0 && (
        <>
          <h2>{t("agent.checks")}</h2>
          <ul className="checks" data-testid="checks">
            {(task.children ?? []).map((c) => (
              <li key={c.id}>
                <strong>{c.node ?? "?"}</strong>{" "}
                <span className={`badge badge-task-${c.status}`}>
                  {t(`agent.status.${c.status}`)}
                </span>{" "}
                <span className="muted small">
                  {t("agent.checkProgress", {
                    steps: c.step_count,
                    calls: c.tool_calls,
                  })}
                </span>{" "}
                <a href={tasksHref(c.id)}>{t("agent.checkOpen")}</a>
                {c.summary && <div className="small">{c.summary}</div>}
              </li>
            ))}
          </ul>
        </>
      )}

      {task.diagnosis && (
        <>
          <h2>{t("agent.diagnosis")}</h2>
          <p className="diagnosis-summary">{task.diagnosis.summary}</p>
          <ul className="findings">
            {task.diagnosis.findings.map((f, i) => (
              <FindingItem key={i} finding={f} />
            ))}
          </ul>
          {task.diagnosis.next && (
            <>
              <h3>{t("agent.next")}</h3>
              <p>{task.diagnosis.next}</p>
            </>
          )}
          {task.diagnosis.notes.length > 0 && (
            <>
              <h3>{t("agent.notes")}</h3>
              <ul className="muted small">
                {task.diagnosis.notes.map((n, i) => (
                  <li key={i}>{n}</li>
                ))}
              </ul>
            </>
          )}
        </>
      )}

      <Proposals
        api={api}
        question={task.prompt}
        proposals={task.proposals ?? []}
        onChanged={() => {
          load();
          onChanged();
        }}
      />

      {task.evidence.length > 0 && (
        <>
          <h2>{t("agent.evidence")}</h2>
          {task.evidence.map((e) => (
            <EvidenceItem key={e.ref} evidence={e} />
          ))}
        </>
      )}

      <h2>{t("agent.steps")}</h2>
      <ol className="events agent-steps">
        {task.steps.map((s) => (
          <StepItem key={s.seq} step={s} />
        ))}
      </ol>
    </section>
  );
}

function FindingItem({ finding }: { finding: Finding }) {
  const { t } = useTranslation();
  return (
    <li className={`finding finding-${finding.tag}`}>
      <span
        className={`tag tag-${finding.tag}`}
        title={t(`agent.tagHint.${finding.tag}`)}
      >
        [{t(`agent.tag.${finding.tag}`)}]
      </span>{" "}
      {finding.node && <strong>{finding.node}: </strong>}
      {finding.claim}
      {finding.evidence.length > 0 && (
        <span className="muted small">
          {" "}
          {finding.evidence.map((ref) => (
            <a
              key={ref}
              href={`#${evidenceId(ref)}`}
              onClick={(e) => openEvidence(e, ref)}
            >
              [{ref}]{" "}
            </a>
          ))}
        </span>
      )}
      {finding.downgraded && (
        <div className="muted small">{t("agent.downgraded")}</div>
      )}
      {finding.contradicts && finding.contradicts.length > 0 && (
        <div className="small" data-testid="contradicts">
          {t("agent.contradicts", { refs: finding.contradicts.join(", ") })}
        </div>
      )}
      {finding.tag === "assumption" && finding.confirm_with && (
        <div className="muted small">
          {t("agent.checkWith", { what: finding.confirm_with })}
        </div>
      )}
    </li>
  );
}

/** The link scrolls to the evidence and opens it (a hash link would change the page route). */
function openEvidence(e: React.MouseEvent, ref: string) {
  e.preventDefault();
  const el = document.getElementById(evidenceId(ref));
  if (el instanceof HTMLDetailsElement) el.open = true;
  el?.scrollIntoView?.({ block: "center" });
}

function EvidenceItem({ evidence: e }: { evidence: AgentEvidence }) {
  const { t, i18n } = useTranslation();
  return (
    <details id={evidenceId(e.ref)} className="evidence">
      <summary>
        <span className={`tag tag-${e.tag}`}>[{e.ref}]</span>{" "}
        {t("agent.evidenceSource", {
          capability: e.source.capability ?? "?",
          node: e.source.node ?? "?",
        })}
        <span className="muted small">
          {" "}
          ·{" "}
          {t("agent.evidenceBytes", {
            bytes: formatInt(e.bytes, i18n.language),
          })}
          {e.source.at
            ? ` · ${formatDateTime(e.source.at, i18n.language)}`
            : ""}
        </span>
      </summary>
      {e.source.url && <p className="muted small">{e.source.url}</p>}
      {e.source.input && Object.keys(e.source.input).length > 0 && (
        <code className="event-data">{JSON.stringify(e.source.input)}</code>
      )}
      <pre>{e.excerpt}</pre>
    </details>
  );
}

function StepItem({ step }: { step: AgentStep }) {
  const { t, i18n } = useTranslation();
  const when = step.created_at ? (
    <time dateTime={step.created_at}>
      {formatDateTime(step.created_at, i18n.language)}
    </time>
  ) : null;
  if (step.kind === "assistant") {
    const s = said(step);
    return (
      <li>
        {when}
        <span className="event-kind">
          {s.kind === "tool"
            ? t("agent.stepAsks", { capability: s.capability, node: s.node })
            : s.kind === "finish"
              ? t("agent.stepFinish")
              : t("agent.stepRaw")}
        </span>
        {s.kind === "tool" && s.reason && (
          <span className="event-detail">{s.reason}</span>
        )}
        <details>
          <summary>{t("agent.details")}</summary>
          <pre>{step.data.content}</pre>
        </details>
      </li>
    );
  }
  if (step.kind === "observation") {
    const d = step.data;
    const label =
      d.kind === "tool"
        ? t("agent.stepResult", { ref: d.evidence ?? "" })
        : d.kind === "error"
          ? t("agent.stepError")
          : t("agent.stepLimit");
    return (
      <li className={d.kind === "error" ? "step-error" : undefined}>
        {when}
        <span className="event-kind">{label}</span>
        <details>
          <summary>{t("agent.details")}</summary>
          <pre>{d.text}</pre>
        </details>
      </li>
    );
  }
  return (
    <li>
      {when}
      <span className="event-kind">{step.data.text}</span>
    </li>
  );
}
