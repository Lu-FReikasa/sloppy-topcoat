import { useState } from "react";
import type { TFunction } from "i18next";
import { useTranslation } from "react-i18next";
import type { ApiClient, CheckDone, CheckStepEvent } from "../api/client";
import { tasksHref } from "../route";
import type { AgentTaskDetail } from "../types";
import type { CheckState } from "./state";

/** What the chat shows of a server check (E3-01d): the model's plan for each call, the call with
 *  its result and the evidence it produced. Collapsible; open while the check runs. */

function inputText(input: Record<string, unknown> | undefined): string {
  if (!input || Object.keys(input).length === 0) return "";
  const text = JSON.stringify(input);
  return text.length > 80 ? `${text.slice(0, 77)}…` : text;
}

export function CheckSteps({ steps }: { steps: CheckStepEvent[] }) {
  const { t } = useTranslation();
  return (
    <ol className="check-steps">
      {steps.map((s) => {
        if (s.kind === "plan")
          return (
            <li key={s.seq} className="muted">
              {t("check.plan", { text: s.text })}
            </li>
          );
        if (s.kind === "note")
          return (
            <li key={s.seq} className="muted">
              {s.text}
            </li>
          );
        return (
          <li key={s.seq} className={s.ok ? undefined : "step-error"}>
            <code>{`${s.capability ?? "?"} @ ${s.node ?? "?"} ${inputText(s.input)}`.trim()}</code>{" "}
            {s.ok ? (
              <span className="badge badge-tag-live">{s.ref ?? "ok"}</span>
            ) : (
              <span>{t("check.failedCall", { code: s.code ?? "error" })}</span>
            )}
          </li>
        );
      })}
    </ol>
  );
}

function Evidence({ evidence }: { evidence: CheckDone["evidence"] }) {
  const { t } = useTranslation();
  if (evidence.length === 0) return null;
  return (
    <ul className="check-evidence small">
      {evidence.map((e) => (
        <li key={e.ref}>
          <span className={`badge badge-tag-${e.tag}`}>{e.ref}</span>{" "}
          {t(`check.tag.${e.tag}`, { defaultValue: e.tag })}
          {e.capability ? ` · ${e.capability}` : ""}
          {e.node ? ` @ ${e.node}` : ""}
          {e.url ? ` · ${e.url}` : ""}
        </li>
      ))}
    </ul>
  );
}

/** One line for a finished check. A check that failed after it had looked at something is
 *  incomplete, not "could not be made": what it saw is listed below the line. */
function doneSummary(t: TFunction, done: CheckDone): string {
  const why = done.error ?? done.stop_reason ?? done.status;
  if (done.status === "done") {
    return t("check.done", { calls: done.calls, evidence: done.evidence.length });
  }
  return done.evidence.length > 0
    ? t("check.incomplete", { evidence: done.evidence.length, message: why })
    : t("check.failed", { message: why });
}


function stepsOf(task: AgentTaskDetail): CheckStepEvent[] {
  const out: CheckStepEvent[] = [];
  for (const s of task.steps) {
    const data = s.data as Record<string, unknown>;
    if (s.kind === "assistant") {
      try {
        const obj = JSON.parse(String(data.content ?? "")) as Record<string, unknown>;
        if (obj.action === "tool" && obj.reason)
          out.push({ seq: s.seq, kind: "plan", text: String(obj.reason) });
      } catch {
        /* not an action */
      }
    } else if (s.kind === "observation" && data.tool && typeof data.tool === "object") {
      const tool = data.tool as Record<string, unknown>;
      out.push({
        seq: s.seq,
        kind: "call",
        capability: String(tool.capability ?? ""),
        node: String(tool.node ?? ""),
        input: (tool.input as Record<string, unknown>) ?? {},
        ok: tool.ok === true,
        ref: (data.evidence as string | null) ?? null,
        code: (tool.code as string | null) ?? null,
      });
    }
  }
  return out;
}

/** The check behind a stored answer: what this session saw of it, or loaded when opened. */
export function StoredCheck({
  api,
  taskId,
  snapshot,
}: {
  api: ApiClient;
  taskId: string;
  snapshot?: CheckState;
}) {
  const { t } = useTranslation();
  const [task, setTask] = useState<AgentTaskDetail | null>(null);
  const [error, setError] = useState(false);
  const steps = snapshot?.steps ?? (task ? stepsOf(task) : []);
  const evidence: CheckDone["evidence"] =
    snapshot?.done?.evidence ??
    (task?.evidence ?? []).map((e) => ({
      ref: e.ref,
      tag: e.tag,
      node: String((e.source as Record<string, unknown>).node ?? ""),
      capability: String((e.source as Record<string, unknown>).capability ?? ""),
      bytes: e.bytes,
    }));
  const calls = steps.filter((s) => s.kind === "call").length;
  return (
    <details
      className="check-panel"
      data-testid="stored-check"
      onToggle={(e) => {
        if ((e.currentTarget as HTMLDetailsElement).open && !snapshot && !task) {
          api.agentTask(taskId).then(setTask, () => setError(true));
        }
      }}
    >
      <summary>{snapshot?.done ? doneSummary(t, snapshot.done) : t("check.stored")}</summary>
      {error && <p className="error small">{t("check.loadFailed")}</p>}
      {!snapshot && !task && !error && <p className="muted small">{t("app.loading")}</p>}
      <CheckSteps steps={steps} />
      <Evidence evidence={evidence} />
      {calls > 0 || task ? (
        <p className="small">
          <a href={tasksHref(taskId)}>{t("check.openTask")}</a>
        </p>
      ) : null}
    </details>
  );
}
