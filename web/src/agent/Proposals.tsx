import { useState } from "react";
import { useTranslation } from "react-i18next";
import { UnauthorizedError, type ApiClient } from "../api/client";
import { describeError, toActionError } from "../errors";
import { formatDateTime } from "../format";
import { tasksHref } from "../route";
import type { AgentProposal } from "../types";

interface Props {
  api: ApiClient;
  /** The task's question, for the follow-up check. */
  question: string;
  proposals: AgentProposal[];
  onChanged: () => void;
}

/** What the agent proposes to change (E3-01b). Nothing runs until the person clicks "Run" on that
 *  one card: no "run all", no "remember my answer". */
export function Proposals({ api, question, proposals, onChanged }: Props) {
  const { t } = useTranslation();
  if (proposals.length === 0) return null;
  return (
    <section className="proposals" aria-label={t("proposal.heading")}>
      <h2>{t("proposal.heading")}</h2>
      <p className="muted small">{t("proposal.intro")}</p>
      {proposals.map((p) => (
        <ProposalCard
          key={p.id}
          api={api}
          question={question}
          proposal={p}
          onChanged={onChanged}
        />
      ))}
    </section>
  );
}

export function ProposalCard({
  api,
  question,
  proposal: p,
  onChanged,
}: {
  api: ApiClient;
  /** Without a question (a proposal from the inventory) there is no follow-up check. */
  question?: string;
  proposal: AgentProposal;
  onChanged: () => void;
}) {
  const { t, i18n } = useTranslation();
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [checking, setChecking] = useState(false);

  async function decide(kind: "execute" | "refuse") {
    setBusy(true);
    setError(null);
    try {
      if (kind === "execute") await api.executeProposal(p.id);
      else await api.refuseProposal(p.id);
      onChanged();
    } catch (err) {
      if (!(err instanceof UnauthorizedError))
        setError(describeError(toActionError(err), t, i18n));
      onChanged(); // the state may have changed (expired, already run)
    } finally {
      setBusy(false);
    }
  }

  /** "Did it help?" is an ordinary read-only task of its own. */
  async function check() {
    setChecking(true);
    setError(null);
    try {
      const prompt = t("proposal.followupPrompt", {
        capability: p.capability,
        node: p.node,
        input: JSON.stringify(p.input),
        question: question ?? "",
      });
      const created = await api.startAgentTask(prompt);
      window.location.hash = tasksHref(created.id).replace(/^#/, "");
    } catch (err) {
      if (!(err instanceof UnauthorizedError))
        setError(describeError(toActionError(err), t, i18n));
    } finally {
      setChecking(false);
    }
  }

  const pending = p.status === "pending";
  return (
    <article className={`proposal proposal-${p.status}`} data-testid="proposal">
      <div className="detail-head">
        <h3>
          <code>{p.capability}</code> {t("proposal.on", { node: p.node })}
        </h3>
        <span className={`badge badge-proposal-${p.status}`}>
          {t(`proposal.status.${p.status}`)}
        </span>
      </div>
      <code className="event-data">{JSON.stringify(p.input)}</code>
      {p.preview && (
        <details open className="proposal-preview">
          <summary>{t("proposal.preview")}</summary>
          <pre className="diff">{p.preview}</pre>
        </details>
      )}
      <dl className="proposal-facts">
        <dt>{t("proposal.reason")}</dt>
        <dd>{p.reason}</dd>
        <dt>{t("proposal.effect")}</dt>
        <dd>{p.effect}</dd>
        <dt>{t("proposal.whoNotices")}</dt>
        <dd>{p.who_notices}</dd>
        <dt>{t("proposal.wayBack")}</dt>
        <dd>{p.way_back}</dd>
      </dl>
      {pending && p.expires_at && (
        <p className="muted small">
          {t("proposal.expires", {
            when: formatDateTime(p.expires_at, i18n.language),
          })}
        </p>
      )}
      {pending && (
        <div className="dialog-actions">
          <button
            type="button"
            className="primary"
            disabled={busy}
            onClick={() => void decide("execute")}
          >
            {busy ? t("proposal.running") : t("proposal.run")}
          </button>
          <button
            type="button"
            disabled={busy}
            onClick={() => void decide("refuse")}
          >
            {t("proposal.refuse")}
          </button>
        </div>
      )}
      {p.status === "running" && (
        <p className="notice" role="status">
          {t("proposal.running")}
        </p>
      )}
      {p.status === "executed" && (
        <>
          <p className="notice">{t("proposal.done")}</p>
          <pre>{JSON.stringify(p.result?.output ?? {}, null, 2)}</pre>
          {question !== undefined && (
            <button
              type="button"
              disabled={checking}
              onClick={() => void check()}
            >
              {t("proposal.check")}
            </button>
          )}
        </>
      )}
      {p.status === "failed" && (
        <p className="error" role="alert">
          {t("proposal.failed", { message: p.result?.error?.message ?? "" })}
        </p>
      )}
      {error && (
        <p className="error" role="alert">
          {error}
        </p>
      )}
    </article>
  );
}
