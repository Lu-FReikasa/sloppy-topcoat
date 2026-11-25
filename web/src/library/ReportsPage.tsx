import { useCallback, useEffect, useMemo, useState } from "react";
import { useTranslation } from "react-i18next";
import type { ApiClient } from "../api/client";
import { describeError, toActionError } from "../errors";
import { formatDateTime } from "../format";
import { chatHref, libraryHref } from "../route";
import type {
  ReportComment,
  ReportDetail,
  ReportDiff,
  ReportStatus,
  ReportVersionInfo,
} from "../types";
import {
  PAPER_CSS,
  ReportBody,
  exportHtml,
  fileName,
  headingsOf,
} from "./reportDoc";
import { askConfirm } from "../components/askConfirm";

/** The address of a report inside the library (`#/library/report:<id>`). */
export const REPORT_PREFIX = "report:";

export function reportHref(id: string): string {
  return libraryHref(`${REPORT_PREFIX}${id}`);
}
type Panel = "document" | "versions" | "comments";

function download(name: string, mime: string, text: string) {
  const url = URL.createObjectURL(new Blob([text], { type: `${mime};charset=utf-8` }));
  const a = document.createElement("a");
  a.href = url;
  a.download = name;
  document.body.appendChild(a);
  a.click();
  a.remove();
  URL.revokeObjectURL(url);
}

/** One report: the page, its versions with a diff, its comments, the person's status clicks. */
export function ReportView({
  api,
  id,
  onChanged,
  onDeleted,
}: {
  api: ApiClient;
  id: string;
  onChanged: () => void;
  /** Called after a delete (a document window shows that it is gone); else the Ablage opens. */
  onDeleted?: () => void;
}) {
  const { t, i18n } = useTranslation();
  const [detail, setDetail] = useState<ReportDetail | null>(null);
  const [panel, setPanel] = useState<Panel>("document");
  const [versions, setVersions] = useState<ReportVersionInfo[]>([]);
  const [diff, setDiff] = useState<ReportDiff | null>(null);
  const [from, setFrom] = useState(1);
  const [to, setTo] = useState(1);
  const [anchor, setAnchor] = useState("");
  const [text, setText] = useState("");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const [revising, setRevising] = useState<{ task_id: string; open_comments: number } | null>(null);

  const fail = useCallback(
    (e: unknown) => setError(describeError(toActionError(e), t, i18n)),
    [t, i18n],
  );

  const load = useCallback(
    (version?: number) => {
      api.report(id, version).then(
        (d) => {
          setError("");
          setDetail(d);
          if (d.unread && !version) void api.markReportRead(id).then(onChanged, () => undefined);
        },
        fail,
      );
      api.reportVersions(id).then((r) => {
        setVersions(r.versions);
        const last = r.versions[r.versions.length - 1]?.version ?? 1;
        setTo(last);
        setFrom(Math.max(1, last - 1));
      }, fail);
    },
    [api, id, fail, onChanged],
  );

  useEffect(() => load(), [load]);

  const headings = useMemo(() => headingsOf(detail?.shown.body ?? ""), [detail]);
  const loadImage = useCallback((path: string) => api.reportImage(path), [api]);

  if (!detail) {
    return (
      <article className="library-open" data-testid="report-open">
        {error ? <p className="error" role="alert">{error}</p> : <p className="muted">{t("app.loading")}</p>}
      </article>
    );
  }

  const current = detail.current_version;
  const shownVersion = detail.shown.version;
  const isCurrent = shownVersion === current;
  const incomplete = detail.shown.meta.complete === false;

  async function act(call: () => Promise<unknown>) {
    setBusy(true);
    setError("");
    try {
      await call();
      load(isCurrent ? undefined : shownVersion);
      onChanged();
    } catch (e) {
      fail(e);
    } finally {
      setBusy(false);
    }
  }

  const setStatus = (status: ReportStatus) => act(() => api.setReportStatus(id, status));

  async function remove() {
    if (!await askConfirm(t("reports.confirmDelete"))) return;
    setBusy(true);
    try {
      await api.deleteReport(id);
      if (onDeleted) onDeleted();
      else window.location.hash = libraryHref();
      onChanged();
    } catch (e) {
      fail(e);
    } finally {
      setBusy(false);
    }
  }

  async function revise() {
    setBusy(true);
    setError("");
    try {
      setRevising(await api.reviseReport(id));
    } catch (e) {
      fail(e);
    } finally {
      setBusy(false);
    }
  }

  async function compare() {
    try {
      setDiff(await api.reportDiff(id, from, to));
    } catch (e) {
      fail(e);
    }
  }

  async function comment() {
    if (!text.trim()) return;
    await act(async () => {
      await api.addReportComment(id, { text: text.trim(), anchor, version: shownVersion });
      setText("");
    });
  }

  function printIt() {
    const w = window.open("", "_blank");
    if (!w) return;
    w.document.write(exportHtml(detail!.title, detail!.shown.body, detail!.language));
    w.document.close();
    w.focus();
    w.print();
  }

  const status = detail.status;
  return (
    <article className="library-open report-view" data-testid="report-open">
      <style>{PAPER_CSS}</style>
      <h2>{detail.title}</h2>
      <p className="muted small">
        <span className="chip" data-testid="report-status">
          {t(`reports.status.${status}`)}
        </span>
        {incomplete && (
          <>
            {" "}
            <span className="chip warn-text" data-testid="report-incomplete">
              {t("reports.incomplete")}
            </span>
          </>
        )}{" "}
        {t("reports.versionOf", { n: shownVersion, total: current })} ·{" "}
        {formatDateTime(detail.shown.created_at, i18n.language)}
        {detail.released_version !== null && (
          <> · {t("reports.releasedVersion", { n: detail.released_version })}</>
        )}
        {detail.task_id && (
          <>
            {" · "}
            <a href={`#/tasks/${encodeURIComponent(detail.task_id)}`}>{t("library.task")}</a>
          </>
        )}
        {detail.conversation_id && (
          <>
            {" · "}
            <a href={chatHref(detail.conversation_id)}>{t("library.fromChat")}</a>
          </>
        )}
      </p>
      {error && (
        <p className="error" role="alert">
          {error}
        </p>
      )}
      <div className="memory-actions">
        {isCurrent && status === "draft" && (
          <button type="button" disabled={busy} onClick={() => void setStatus("reviewed")}>
            {t("reports.markReviewed")}
          </button>
        )}
        {isCurrent && status === "reviewed" && (
          <>
            <button type="button" className="primary" disabled={busy} onClick={() => void setStatus("released")}>
              {t("reports.release")}
            </button>
            <button type="button" disabled={busy} onClick={() => void setStatus("draft")}>
              {t("reports.backToDraft")}
            </button>
          </>
        )}
        {isCurrent && (status === "released" || (status === "draft" && detail.released_version !== null)) && (
          <button type="button" disabled={busy} onClick={() => void setStatus("draft")}>
            {t("reports.withdraw")}
          </button>
        )}
        <button
          type="button"
          onClick={() =>
            download(fileName(detail.title, "html"), "text/html", exportHtml(detail.title, detail.shown.body, detail.language))
          }
        >
          {t("reports.exportHtml")}
        </button>
        <button
          type="button"
          onClick={() => download(fileName(detail.title, "md"), "text/markdown", detail.shown.body)}
        >
          {t("reports.exportMarkdown")}
        </button>
        <button type="button" onClick={printIt}>
          {t("reports.print")}
        </button>
        {isCurrent && (
          <button type="button" disabled={busy} onClick={() => void revise()}>
            {t("reports.revise")}
          </button>
        )}
        <button type="button" className="danger" disabled={busy} onClick={() => void remove()}>
          {t("reports.delete")}
        </button>
      </div>
      {revising && (
        <p role="status" data-testid="report-revising">
          {t("reports.revising", { n: revising.open_comments })}{" "}
          <a href={`#/tasks/${encodeURIComponent(revising.task_id)}`}>{t("reports.reviseTask")}</a>
        </p>
      )}
      {!isCurrent && (
        <p className="muted small" role="status">
          {t("reports.olderNote", { n: shownVersion, current })}{" "}
          <button type="button" onClick={() => load()}>
            {t("reports.showCurrent")}
          </button>
        </p>
      )}
      <div className="inbox-tabs" role="tablist" aria-label={detail.title}>
        {(["document", "versions", "comments"] as const).map((p) => (
          <button
            key={p}
            type="button"
            role="tab"
            aria-selected={panel === p}
            className={panel === p ? "primary" : undefined}
            onClick={() => setPanel(p)}
          >
            {t(`reports.panel.${p}`)}
            {p === "comments" && detail.comments.length > 0 ? ` (${detail.comments.length})` : ""}
          </button>
        ))}
      </div>

      {panel === "document" && (
        <article className="report-paper" data-testid="report-paper">
          <ReportBody
            text={detail.shown.body}
            idPrefix="rep-"
            inApp
            loadImage={loadImage}
          />
        </article>
      )}

      {panel === "versions" && (
        <div data-testid="report-versions">
          <ol className="name-list">
            {versions.map((v) => (
              <li key={v.version}>
                <span className="name-list-name">
                  {t("reports.versionRow", {
                    n: v.version,
                    kind: t(`reports.kind.${v.kind}`),
                    when: formatDateTime(v.created_at, i18n.language) ?? "",
                  })}
                  {v.parent_version !== null && v.parent_version !== v.version - 1
                    ? ` · ${t("reports.parent", { n: v.parent_version })}`
                    : ""}
                  {v.note ? ` · ${v.note}` : ""}
                </span>
                <button
                  type="button"
                  aria-label={t("reports.showVersion", { n: v.version })}
                  disabled={v.version === shownVersion}
                  onClick={() => {
                    setPanel("document");
                    load(v.version);
                  }}
                >
                  {t("reports.show")}
                </button>
              </li>
            ))}
          </ol>
          {versions.length > 1 && (
            <div className="row-actions">
              <label>
                {t("reports.from")}{" "}
                <select value={from} onChange={(e) => setFrom(Number(e.target.value))}>
                  {versions.map((v) => (
                    <option key={v.version} value={v.version}>
                      {v.version}
                    </option>
                  ))}
                </select>
              </label>
              <label>
                {t("reports.to")}{" "}
                <select value={to} onChange={(e) => setTo(Number(e.target.value))}>
                  {versions.map((v) => (
                    <option key={v.version} value={v.version}>
                      {v.version}
                    </option>
                  ))}
                </select>
              </label>
              <button type="button" onClick={() => void compare()}>
                {t("reports.compare")}
              </button>
            </div>
          )}
          {diff && (
            <div data-testid="report-diff">
              <p className="muted small">
                {t("reports.diffSummary", { added: diff.added, removed: diff.removed })}
              </p>
              {diff.diff ? (
                <pre className="report-diff">
                  {diff.diff.split("\n").map((line, i) => (
                    <div
                      key={i}
                      className={
                        line.startsWith("+") && !line.startsWith("+++")
                          ? "diff-add"
                          : line.startsWith("-") && !line.startsWith("---")
                            ? "diff-del"
                            : undefined
                      }
                    >
                      {line || " "}
                    </div>
                  ))}
                </pre>
              ) : (
                <p className="muted">{t("reports.diffEmpty")}</p>
              )}
            </div>
          )}
        </div>
      )}

      {panel === "comments" && (
        <div data-testid="report-comments">
          {detail.comments.length === 0 && <p className="muted">{t("reports.noComments")}</p>}
          <ul className="history-list">
            {detail.comments.map((c: ReportComment) => (
              <li key={c.id} className={c.resolved ? "muted" : undefined}>
                <strong>{c.anchor ? headings.find((h) => h.id === c.anchor)?.title ?? c.anchor : t("reports.whole")}</strong>{" "}
                · {t(`reports.author.${c.author}`)} · {formatDateTime(c.created_at, i18n.language)}
                <br />
                {c.text}{" "}
                <button
                  type="button"
                  disabled={busy}
                  onClick={() => void act(() => api.resolveReportComment(id, c.id, !c.resolved))}
                >
                  {c.resolved ? t("reports.reopen") : t("reports.resolve")}
                </button>
              </li>
            ))}
          </ul>
          <p className="muted small">{t("reports.commentHint", { n: shownVersion })}</p>
          <div className="row-actions">
            <label>
              {t("reports.anchor")}{" "}
              <select value={anchor} onChange={(e) => setAnchor(e.target.value)}>
                <option value="">{t("reports.whole")}</option>
                {headings.map((h) => (
                  <option key={h.id} value={h.id}>
                    {h.title}
                  </option>
                ))}
              </select>
            </label>
          </div>
          <textarea
            aria-label={t("reports.commentText")}
            rows={3}
            value={text}
            onChange={(e) => setText(e.target.value)}
          />
          <div className="row-actions">
            <button type="button" className="primary" disabled={busy || !text.trim()} onClick={() => void comment()}>
              {t("reports.addComment")}
            </button>
          </div>
        </div>
      )}
    </article>
  );
}
