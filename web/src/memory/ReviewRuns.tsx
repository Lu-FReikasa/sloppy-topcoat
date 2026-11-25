import { useTranslation } from "react-i18next";
import { formatDateTime } from "../format";
import type { MemoryRun } from "../types";

/** The runs of the second look at recent chats: when, how many answers, which model reviewed. */
export function ReviewRuns({
  runs,
  locale,
}: {
  runs: MemoryRun[];
  locale: string;
}) {
  const { t } = useTranslation();
  if (runs.length === 0) return null;
  return (
    <section className="review-runs" data-testid="review-runs">
      <h2>{t("memory.reviews.heading")}</h2>
      <p className="muted small">{t("memory.reviews.intro")}</p>
      <ul>
        {runs.map((run) => {
          const s = run.stats as {
            reviewed?: number;
            errors?: number;
            hints?: number;
            reviewers?: string[];
          };
          return (
            <li key={run.id} data-testid="review-run">
              {formatDateTime(run.finished_at ?? run.started_at, locale) ?? "–"}{" "}
              ·{" "}
              {t("memory.reviews.counts", {
                reviewed: s.reviewed ?? 0,
                errors: s.errors ?? 0,
                hints: s.hints ?? 0,
              })}
              {s.reviewers && s.reviewers.length > 0
                ? ` · ${s.reviewers.join(", ")}`
                : ""}
              {run.status === "ok"
                ? ""
                : ` · ${t(`memory.reviews.status.${run.status}`, { defaultValue: run.status })}`}
            </li>
          );
        })}
      </ul>
    </section>
  );
}
