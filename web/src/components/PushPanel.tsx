import { useCallback, useState, type FormEvent } from "react";
import { useTranslation } from "react-i18next";
import { ProposalCard } from "../agent/Proposals";
import { UnauthorizedError, type ApiClient } from "../api/client";
import { describeError, toActionError } from "../errors";
import type { AgentProposal, DevRepo } from "../types";

/** Step one of a push (E6-01 S2): a commit message, then the card that shows the workshop's diff.
 *  Nothing leaves the workshop before the click on "Run" on that card. */
export function PushPanel({ api, repo }: { api: ApiClient; repo: DevRepo }) {
  const { t, i18n } = useTranslation();
  const [message, setMessage] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [proposal, setProposal] = useState<AgentProposal | null>(null);

  async function propose(e: FormEvent) {
    e.preventDefault();
    if (message.trim().length < 3) return;
    setBusy(true);
    setError(null);
    try {
      setProposal(await api.proposeDevPush(repo.id, message.trim()));
      setMessage("");
    } catch (err) {
      if (!(err instanceof UnauthorizedError))
        setError(describeError(toActionError(err), t, i18n));
    } finally {
      setBusy(false);
    }
  }

  const reload = useCallback(() => {
    if (!proposal) return;
    api.proposal(proposal.id).then(setProposal, () => undefined);
  }, [api, proposal]);

  return (
    <div className="push-panel">
      <form className="form-grid" onSubmit={(e) => void propose(e)}>
        <label>
          {t("repos.pushMessage")}
          <input
            type="text"
            value={message}
            maxLength={2000}
            placeholder={t("repos.pushMessagePh")}
            onChange={(e) => setMessage(e.target.value)}
          />
        </label>
        <p className="muted small">{t("repos.pushHint")}</p>
        <button
          type="submit"
          className="primary"
          disabled={busy || message.trim().length < 3}
        >
          {t("repos.pushShow")}
        </button>
      </form>
      {error && (
        <p className="error" role="alert">
          {error}
        </p>
      )}
      {proposal && (
        <ProposalCard api={api} proposal={proposal} onChanged={reload} />
      )}
    </div>
  );
}
