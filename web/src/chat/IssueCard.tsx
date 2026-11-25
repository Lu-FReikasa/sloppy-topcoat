// An issue from the running system in a chat (hub project_issues.py): "Problem: …" filed it, and
// the card offers to open it or to undo it; a complaint in Projekte gets a card that files it on a
// click, with the words to trim first.
import { useState } from "react";
import { useTranslation } from "react-i18next";
import type { ApiClient } from "../api/client";
import { describeError, toActionError } from "../errors";
import { requestOpen } from "../layout/intent";
import type { IssueCardData } from "../types";

export function IssueCard({ api, issue }: { api: ApiClient; issue: IssueCardData }) {
  const { t, i18n } = useTranslation();
  const [text, setText] = useState(issue.text ?? "");
  const [filed, setFiled] = useState<{ id: string; number: string; project: string } | null>(
    issue.filed && issue.task_id
      ? { id: issue.task_id, number: issue.number ?? "", project: issue.project_id ?? "" }
      : null,
  );
  const [undone, setUndone] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const fail = (err: unknown) => setError(describeError(toActionError(err), t, i18n));

  async function file() {
    setBusy(true);
    setError(null);
    try {
      const out = await api.fileIssue({ text: text.trim(), application: issue.application, conversation: issue.conversation });
      setFiled({ id: out.task.id, number: out.task.number, project: out.task.project_id });
    } catch (err) {
      fail(err);
    } finally {
      setBusy(false);
    }
  }

  async function undo() {
    if (!filed) return;
    setBusy(true);
    try {
      await api.trashProjectNode(filed.id);
      setUndone(true);
    } catch (err) {
      fail(err);
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="issue-card" data-testid="issue-card">
      {filed ? (
        <>
          <p>
            <strong>{t("issues.filedAs", { number: filed.number })}</strong> {issue.title ?? text}
          </p>
          {issue.similar && issue.similar.length > 0 && (
            <p className="muted small">
              {t("issues.similar")}: {issue.similar.map((s) => `${s.number} „${s.title}“`).join(", ")}
            </p>
          )}
          {undone ? (
            <p role="status">{t("issues.undone")}</p>
          ) : (
            <div>
              <button
                type="button"
                onClick={() => requestOpen({ module: "page.project", props: { project: filed.project, node: filed.id } })}
              >
                {t("issues.open")}
              </button>{" "}
              <button type="button" disabled={busy} onClick={() => void undo()}>
                {t("issues.undo")}
              </button>
            </div>
          )}
        </>
      ) : (
        <>
          <label>
            {t("issues.text")}
            <textarea rows={3} value={text} maxLength={4000} onChange={(e) => setText(e.target.value)} />
          </label>
          <button type="button" disabled={busy || text.trim().length < 3} onClick={() => void file()}>
            {t("issues.file")}
          </button>
        </>
      )}
      {error && (
        <p className="error small" role="alert">
          {error}
        </p>
      )}
    </div>
  );
}
