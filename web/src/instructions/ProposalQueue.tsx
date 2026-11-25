import { useState } from "react";
import { useTranslation } from "react-i18next";
import type { ApiClient } from "../api/client";
import { copyToClipboard } from "../chat/Markdown";
import type { CoreProposal } from "../types";

interface Props {
  api: ApiClient;
  proposals: CoreProposal[];
  onChanged: () => void;
  onError: (err: unknown) => void;
}

/** What the assistant (or the care run) would like to change in the instructions: one card each,
 *  with the reason, the evidence and the difference. Nothing happens until the user clicks: accept,
 *  edit and accept, or reject (the same wish does not come back for 90 days). */
export function ProposalQueue({ api, proposals, onChanged, onError }: Props) {
  const { t } = useTranslation();
  if (proposals.length === 0) {
    return (
      <p className="muted small" data-testid="no-proposals">
        {t("instructions.proposals.none")}
      </p>
    );
  }
  return (
    <ul className="proposals">
      {proposals.map((p) => (
        <ProposalCard key={p.id} api={api} proposal={p} onChanged={onChanged} onError={onError} />
      ))}
    </ul>
  );
}

function ProposalCard({
  api,
  proposal: p,
  onChanged,
  onError,
}: {
  api: ApiClient;
  proposal: CoreProposal;
  onChanged: () => void;
  onError: (err: unknown) => void;
}) {
  const { t } = useTranslation();
  const [editing, setEditing] = useState(false);
  const [text, setText] = useState(p.text);
  const [example, setExample] = useState(p.example ?? "");
  const [copied, setCopied] = useState(false);
  const before = typeof p.diff.before?.text === "string" ? p.diff.before.text : "";
  const isFile = p.action === "kb_edit";
  const hasText = p.action === "add" || p.action === "change";

  async function accept(edited: boolean) {
    try {
      await api.acceptProposal(
        p.id,
        edited
          ? {
              text: text.trim() || undefined,
              ...(p.kind === "style" ? { example: example.trim() || null } : {}),
            }
          : undefined,
      );
      onChanged();
    } catch (err) {
      onError(err);
    }
  }

  async function reject() {
    try {
      await api.rejectProposal(p.id);
      onChanged();
    } catch (err) {
      onError(err);
    }
  }

  return (
    <li className="proposal" data-testid="proposal">
      <p className="proposal-head">
        <span className="tag">{t(`instructions.proposals.action.${p.action}`)}</span>{" "}
        <span className="muted small">
          {t(`instructions.kind.${p.kind}`, { defaultValue: p.kind })} ·{" "}
          {t(`instructions.proposals.origin.${p.origin}`)}
        </span>
      </p>
      {editing && hasText ? (
        <div className="stack">
          <label>
            <span className="fact-label">{t("instructions.text")}</span>
            <textarea
              value={text}
              rows={3}
              maxLength={1000}
              onChange={(e) => setText(e.target.value)}
              aria-label={t("instructions.text")}
            />
          </label>
          {p.kind === "style" && (
            <label>
              <span className="fact-label">{t("instructions.example")}</span>
              <textarea
                value={example}
                rows={2}
                maxLength={480}
                onChange={(e) => setExample(e.target.value)}
                aria-label={t("instructions.example")}
              />
            </label>
          )}
        </div>
      ) : (
        <>
          {(p.text || before) && (
            <p className="proposal-text">
              {before && p.action !== "add" && <del className="muted">{before}</del>}
              {before && p.text && p.action !== "add" ? " → " : ""}
              {p.text && <strong>{p.text}</strong>}
              {!p.text && p.action !== "add" && !before ? null : null}
            </p>
          )}
          {p.example && (
            <p className="small" data-testid="proposal-example">
              {t("instructions.example")}: <em>“{p.example}”</em>
            </p>
          )}
          {p.weak && <p className="muted small">{t("instructions.weak")}</p>}
        </>
      )}
      <p className="small">{p.reason}</p>
      {p.evidence.quote && (
        <p className="small muted" data-testid="proposal-quote">
          {t("instructions.proposals.quote")}: “{p.evidence.quote}”
        </p>
      )}
      {isFile && (
        <div className="stack" data-testid="proposal-markdown">
          <p className="small">
            {t("instructions.proposals.fileHint", { path: p.evidence.path ?? "" })}
          </p>
          <pre className="plain-pre">{p.evidence.markdown}</pre>
          <div className="row-actions">
            <button
              type="button"
              onClick={() => void copyToClipboard(p.evidence.markdown ?? "").then(setCopied)}
            >
              {copied ? t("chat.copied") : t("instructions.proposals.copy")}
            </button>
          </div>
        </div>
      )}
      <div className="row-actions">
        <button type="button" className="primary" onClick={() => void accept(editing)}>
          {isFile ? t("instructions.proposals.done") : t("instructions.proposals.accept")}
        </button>
        {hasText && !editing && (
          <button type="button" onClick={() => setEditing(true)}>
            {t("instructions.proposals.edit")}
          </button>
        )}
        {editing && (
          <button type="button" onClick={() => setEditing(false)}>
            {t("instructions.cancel")}
          </button>
        )}
        <button type="button" onClick={() => void reject()}>
          {t("instructions.proposals.reject")}
        </button>
      </div>
    </li>
  );
}
