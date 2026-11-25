import { useEffect, useState } from "react";
import { useTranslation } from "react-i18next";
import type { ApiClient } from "../api/client";
import { describeError, toActionError } from "../errors";
import type { ModelSwitches } from "../types";

/** The four kinds of work of the plan, with the label the hub records for them (job records,
 *  column `work`). A kind without its own label shows dashes, not a guess. */
export const TASK_KINDS: { key: string; label: string | null }[] = [
  { key: "answer", label: "chat" },
  { key: "work", label: null },
  { key: "second_look", label: "review" },
  { key: "background", label: "background" },
];

/** The loads of one kind of work, or null when the kind has no label of its own yet. */
export function loadsOf(view: ModelSwitches, label: string | null): number | null {
  if (label === null) return null;
  return view.totals.loads_by_work[label] ?? 0;
}

/** Administration: the kinds of work the hub distinguishes (plan P1, first slice). Only the
 *  loads per kind are figures here; choosing models by kind is P2b and is not active yet. */
export function TaskKindsPage({ api }: { api: ApiClient }) {
  const { t, i18n } = useTranslation();
  const [view, setView] = useState<ModelSwitches | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let live = true;
    api.getModelSwitches(7).then(
      (v) => live && setView(v),
      (err: unknown) => live && setError(describeError(toActionError(err), t, i18n)),
    );
    return () => {
      live = false;
    };
  }, [api, t, i18n]);

  return (
    <section data-testid="task-kinds">
      <h2>{t("taskKinds.title")}</h2>
      <p className="muted small">{t("taskKinds.hint")}</p>
      {error && (
        <p className="bad" role="alert">
          {error}
        </p>
      )}
      <div
        style={{
          display: "grid",
          gap: "12px",
          gridTemplateColumns: "repeat(auto-fill, minmax(240px, 1fr))",
        }}
      >
        {TASK_KINDS.map((kind) => {
          const loads = view ? loadsOf(view, kind.label) : null;
          return (
            <article key={kind.key} className="card" data-testid="task-kind">
              <h3>{t(`taskKinds.kind.${kind.key}`)}</h3>
              <p className="small muted">{t(`taskKinds.kindHint.${kind.key}`)}</p>
              <p className="small">
                {t("taskKinds.loads")}:{" "}
                {loads === null ? t("taskKinds.noLabel") : loads.toLocaleString(i18n.language)}
              </p>
            </article>
          );
        })}
      </div>
    </section>
  );
}
