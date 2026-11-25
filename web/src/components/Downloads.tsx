import { useMemo, useState, type FormEvent } from "react";
import { useTranslation } from "react-i18next";
import { UnauthorizedError, type ApiClient } from "../api/client";
import { describeError, toActionError } from "../errors";
import { formatBytes, percent } from "../format";
import { downloadsFromEvents, type DownloadInfo } from "../nodeActivity";
import type { NodeEvent } from "../types";
import { Dialog } from "./Dialog";

const REPO_RE = /^[A-Za-z0-9][A-Za-z0-9._-]*\/[A-Za-z0-9][A-Za-z0-9._-]*$/;

/** Arguments of the models.download action (docs/PROTOCOL.md). */
export function downloadArgs(
  repo: string,
  file: string,
  revision: string,
  name: string,
): Record<string, unknown> {
  const source: Record<string, string> = { type: "huggingface", repo: repo.trim(), file: file.trim() };
  if (revision.trim()) source.revision = revision.trim();
  const args: Record<string, unknown> = { source };
  if (name.trim()) args.target_name = name.trim();
  return args;
}

export function DownloadDialog({
  api,
  nodeId,
  onClose,
}: {
  api: ApiClient;
  nodeId: string;
  onClose: () => void;
}) {
  const { t, i18n } = useTranslation();
  const [repo, setRepo] = useState("");
  const [file, setFile] = useState("");
  const [revision, setRevision] = useState("");
  const [name, setName] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const repoValid = REPO_RE.test(repo.trim());
  const fileValid = file.trim().toLowerCase().endsWith(".gguf") && !file.includes("..");
  const valid = repoValid && fileValid;

  async function submit(e: FormEvent) {
    e.preventDefault();
    if (!valid) return;
    setBusy(true);
    setError(null);
    try {
      const res = await api.runAction(
        nodeId,
        "models.download",
        downloadArgs(repo, file, revision, name),
      );
      if (res.ok) {
        onClose();
        return;
      }
      setError(describeError(res.error ?? { code: "unknown", message: "" }, t, i18n));
    } catch (err) {
      if (!(err instanceof UnauthorizedError)) {
        setError(describeError(toActionError(err), t, i18n));
      }
    }
    setBusy(false);
  }

  return (
    <Dialog title={t("download.dialogTitle")} onClose={onClose}>
      <form className="form" onSubmit={(e) => void submit(e)}>
        <label>
          {t("download.repo")}
          <input
            value={repo}
            onChange={(e) => setRepo(e.target.value)}
            placeholder="Qwen/Qwen2.5-1.5B-Instruct-GGUF"
            autoFocus
            required
          />
        </label>
        {repo.trim() !== "" && !repoValid && (
          <p className="error small">{t("download.repoInvalid")}</p>
        )}
        <label>
          {t("download.file")}
          <input
            value={file}
            onChange={(e) => setFile(e.target.value)}
            placeholder="qwen2.5-1.5b-instruct-q4_k_m.gguf"
            required
          />
        </label>
        {file.trim() !== "" && !fileValid && (
          <p className="error small">{t("download.fileInvalid")}</p>
        )}
        <label>
          {t("download.revision")}
          <input value={revision} onChange={(e) => setRevision(e.target.value)} placeholder="main" />
        </label>
        <label>
          {t("download.name")}
          <input value={name} onChange={(e) => setName(e.target.value)} />
        </label>
        <p className="muted small">{t("download.hint")}</p>
        {error && (
          <p className="error" role="alert">
            {error}
          </p>
        )}
        <div className="dialog-actions">
          <button type="button" onClick={onClose}>
            {t("dialog.cancel")}
          </button>
          <button type="submit" className="primary" disabled={busy || !valid}>
            {t("download.submit")}
          </button>
        </div>
      </form>
    </Dialog>
  );
}

/** Latest download_progress per model, derived from the live event list. */
export function DownloadList({ events }: { events: NodeEvent[] }) {
  const { t } = useTranslation();
  const downloads = useMemo(() => downloadsFromEvents(events), [events]);
  if (downloads.length === 0) return null;
  return (
    <>
      <h3>{t("download.progressHeading")}</h3>
      <ul className="downloads">
        {downloads.map((d) => (
          <DownloadItem key={d.model} info={d} />
        ))}
      </ul>
    </>
  );
}

function DownloadItem({ info }: { info: DownloadInfo }) {
  const { t, i18n } = useTranslation();
  const locale = i18n.language;
  const total = info.totalBytes;
  const done = info.downloadedBytes;
  const pct = done !== null && total ? percent(done, total) : null;
  let amount = "";
  if (done !== null && total) {
    amount = t("download.amountOf", {
      done: formatBytes(done, locale),
      total: formatBytes(total, locale),
      pct,
    });
  } else if (done !== null) {
    amount = formatBytes(done, locale);
  }
  const statusText = t(`download.status.${info.status}`);
  return (
    <li className={`download download-${info.status}`}>
      <span className="model-name">{info.model}</span>
      <span className="download-status">{statusText}</span>
      {amount && <span className="usage-value">{amount}</span>}
      {info.status === "failed" && info.error && <span className="error">{info.error}</span>}
      {info.status === "running" && (
        <div
          className="usage-track"
          role="progressbar"
          aria-label={info.model}
          aria-valuemin={0}
          aria-valuemax={100}
          aria-valuenow={pct ?? undefined}
        >
          <div className="usage-fill" style={{ width: `${Math.min(100, pct ?? 0)}%` }} />
        </div>
      )}
    </li>
  );
}
