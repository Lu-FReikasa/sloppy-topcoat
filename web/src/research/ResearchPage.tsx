import { useCallback, useEffect, useState, type FormEvent } from "react";
import { useTranslation } from "react-i18next";
import type { ApiClient, ResearchRun } from "../api/client";
import { describeError, toActionError } from "../errors";
import { requestOpen } from "../layout/intent";
import { REPORT_PREFIX } from "../library/ReportsPage";

const POLL_MS = 3000;

/** Research › Recherchieren (the maintainer, 2026-11-13): the main task of the application in one
 *  place. A question starts a research of its own (a thorough run, a check, a revision; minutes,
 *  not seconds); while it runs it shows how far it is; its report lands in the shelf
 *  (Ablage › Recherchen), one click away. Before, a research only started from a chat set to
 *  "thorough". */
export function ResearchPage({ api }: { api: ApiClient }) {
  const { t, i18n } = useTranslation();
  const [question, setQuestion] = useState("");
  const [runs, setRuns] = useState<ResearchRun[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const load = useCallback(() => {
    api.researchRuns().then(
      (r) => setRuns(r.runs),
      () => undefined,
    );
  }, [api]);

  const running = runs.some((r) => r.status === "running");
  useEffect(() => {
    load();
    if (!running) return;
    const timer = setInterval(load, POLL_MS);
    return () => clearInterval(timer);
  }, [load, running]);

  async function start(e: FormEvent) {
    e.preventDefault();
    const q = question.trim();
    if (q.length < 3 || busy) return;
    setBusy(true);
    setError(null);
    try {
      const run = await api.startResearch(q);
      setQuestion("");
      setRuns((all) => [run, ...all.filter((r) => r.id !== run.id)]);
    } catch (err) {
      setError(describeError(toActionError(err), t, i18n));
    } finally {
      setBusy(false);
    }
  }

  const openReport = (id: string) =>
    requestOpen({ module: "page.library", props: { id: `${REPORT_PREFIX}${id}` }, program: "research" });

  return (
    <section className="research-start">
      <h1>{t("research.heading")}</h1>
      <p className="muted">{t("research.intro")}</p>
      <form className="form card" onSubmit={(e) => void start(e)}>
        <label>
          <span>{t("research.question")}</span>
          <textarea
            rows={4}
            maxLength={2000}
            value={question}
            placeholder={t("research.placeholder")}
            onChange={(e) => setQuestion(e.target.value)}
          />
        </label>
        <p className="muted small">{t("research.howLong")}</p>
        <div>
          <button type="submit" className="primary" disabled={busy || question.trim().length < 3}>
            {t("research.start")}
          </button>
        </div>
        {error && (
          <p className="error" role="alert">
            {error}
          </p>
        )}
      </form>
      {runs.length > 0 && (
        <section>
          <h2>{t("research.runs")}</h2>
          <ul className="research-runs">
            {runs.map((r) => (
              <li key={r.id} className="card" data-testid="research-run">
                <strong>{r.question}</strong>
                <p className="small">
                  {r.status === "running"
                    ? (r.progress ?? t("research.running"))
                    : r.status === "done"
                      ? t("research.done")
                      : t("research.failed", { why: r.error ?? r.status })}
                </p>
                <div className="row-actions">
                  {r.report_id && (
                    <button type="button" className="primary" onClick={() => openReport(r.report_id as string)}>
                      {t("research.openReport")}
                    </button>
                  )}
                  {r.status === "running" && (
                    <button type="button" onClick={() => void api.stopResearch(r.id).then(load, load)}>
                      {t("research.stop")}
                    </button>
                  )}
                </div>
              </li>
            ))}
          </ul>
        </section>
      )}
      <p className="muted small">{t("research.shelfHint")}</p>
    </section>
  );
}
