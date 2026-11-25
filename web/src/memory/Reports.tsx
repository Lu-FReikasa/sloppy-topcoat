import { useCallback, useEffect, useState } from "react";
import { useTranslation } from "react-i18next";
import type { ApiClient } from "../api/client";
import { toActionError } from "../errors";
import { formatDateTime } from "../format";
import type { MemoryReport } from "../types";
import { responsibleLabel } from "./TagTree";

interface Props {
  api: ApiClient;
  /** A program, or "person" (Settings): whose reports these are. */
  recipient: string;
}

/** What the cross-check of the memories told this program (ARCHITECTURE D74): the discrepancies of
 *  topics it is responsible for and how it decided them, and the notices other programs sent about
 *  a note of its own. Another program's discrepancies are never shown here. */
export function Reports({ api, recipient }: Props) {
  const { t, i18n } = useTranslation();
  const [reports, setReports] = useState<MemoryReport[] | null>(null);
  const [message, setMessage] = useState<string | null>(null);

  const reload = useCallback(() => {
    api.memoryReports(recipient).then(
      (r) => setReports(r.reports),
      () => setReports([]),
    );
  }, [api, recipient]);

  useEffect(reload, [reload]);

  async function start() {
    setMessage(null);
    try {
      await api.crosscheckMemory();
      setMessage(t("memory.reports.started"));
    } catch (err) {
      const code = toActionError(err).code;
      setMessage(
        ["no_node_available", "memory_disabled"].includes(code)
          ? t(`memory.consolidateRefused.${code}`)
          : t("memory.reports.failed"),
      );
    }
  }

  async function seen(id: string) {
    try {
      await api.markReportSeen(id);
    } finally {
      reload();
    }
  }

  if (reports === null) return null;
  const notices = reports.filter(
    (r) => r.kind === "notice" && r.status === "open",
  );
  const decisions = reports.filter((r) => r.kind === "discrepancy");
  return (
    <section className="memory-reports" aria-label={t("memory.reports.title")}>
      <h2>{t("memory.reports.title")}</h2>
      <p className="muted small">
        {t(
          recipient === "person"
            ? "memory.reports.hintPerson"
            : "memory.reports.hint",
        )}
      </p>
      {notices.length > 0 && (
        <ul className="memory-list" data-testid="report-notices">
          {notices.map((r) => (
            <li key={r.id} className="memory-item" data-testid="report-notice">
              <p>
                {t("memory.reports.notice", {
                  sender: responsibleLabel(r.sender ?? "", t),
                  note: r.a?.text ?? "–",
                })}
              </p>
              <p>„{r.message}“</p>
              <button type="button" onClick={() => void seen(r.id)}>
                {t("memory.reports.seen")}
              </button>
            </li>
          ))}
        </ul>
      )}
      {decisions.length === 0 ? (
        <p className="muted">{t("memory.reports.none")}</p>
      ) : (
        <ul className="memory-list">
          {decisions.map((r) => (
            <li
              key={r.id}
              className="memory-item"
              data-testid="report-discrepancy"
            >
              <p>
                <strong>{r.topic || t("memory.reports.noTopic")}</strong>
                {" · "}
                <span className="muted small">
                  {formatDateTime(
                    r.decided_at ?? r.created_at,
                    i18n.language,
                  ) ?? "–"}
                </span>
              </p>
              <p className="small">
                {[r.a, r.b]
                  .filter((n) => n !== null)
                  .map(
                    (n) =>
                      `${responsibleLabel(n!.application ?? "person", t)}: ${n!.text}`,
                  )
                  .join(" ↔ ")}
              </p>
              <p className="muted small" data-testid="report-outcome">
                {t(`memory.reports.outcome.${r.status}`)}
                {r.decision.why ? ` — ${r.decision.why}` : ""}
              </p>
            </li>
          ))}
        </ul>
      )}
      <button type="button" onClick={() => void start()}>
        {t("memory.reports.start")}
      </button>
      {message && (
        <p className="notice" role="status">
          {message}
        </p>
      )}
    </section>
  );
}
