import { useTranslation } from "react-i18next";
import type { CoreOverview, CorePartName } from "../types";

const PARTS: CorePartName[] = ["system", "persona", "rules", "profile", "household", "recent"];
export const CONTEXTS = [4096, 8192, 16384, 32768] as const;

interface Props {
  budget: CoreOverview["budget"];
  context: number;
  onContext: (n: number) => void;
}

/** How much of the core's token budget each slice uses; the budget follows the model's context
 *  (below 8k the small core, 500 tokens). */
export function BudgetBars({ budget, context, onContext }: Props) {
  const { t } = useTranslation();
  return (
    <section className="core-budget" aria-label={t("instructions.budget.heading")}>
      <div className="core-budget-head">
        <h2>{t("instructions.budget.heading")}</h2>
        <label className="picker">
          <span className="fact-label">{t("instructions.budget.context")}</span>
          <select value={context} onChange={(e) => onContext(Number(e.target.value))}>
            {CONTEXTS.map((c) => (
              <option key={c} value={c}>
                {c.toLocaleString()}
              </option>
            ))}
          </select>
        </label>
      </div>
      <p className="small" data-testid="budget-total">
        {t("instructions.budget.total", { used: budget.used, total: budget.total })}
        {budget.total < 1000 ? ` ${t("instructions.budget.small")}` : ""}
      </p>
      <ul className="budget-parts">
        {PARTS.map((name) => {
          const part = budget.parts[name];
          if (!part) return null;
          const pct =
            part.budget > 0 ? Math.min(100, Math.round((part.used / part.budget) * 100)) : 0;
          return (
            <li key={name} data-testid={`budget-${name}`}>
              <span className="budget-label">{t(`instructions.budget.part.${name}`)}</span>
              <span
                className="budget-bar"
                role="progressbar"
                aria-valuemin={0}
                aria-valuemax={part.budget}
                aria-valuenow={part.used}
                aria-label={t(`instructions.budget.part.${name}`)}
              >
                <span className={pct >= 100 ? "full" : undefined} style={{ width: `${pct}%` }} />
              </span>
              <span className="budget-num small">
                {part.used} / {part.budget}
              </span>
            </li>
          );
        })}
      </ul>
      {(budget.cut.length > 0 || budget.examples_dropped) && (
        <p className="notice-line small" role="status" data-testid="budget-cut">
          {budget.examples_dropped
            ? t("instructions.budget.examplesDropped")
            : t("instructions.budget.cut", { count: budget.cut.length })}
        </p>
      )}
    </section>
  );
}
