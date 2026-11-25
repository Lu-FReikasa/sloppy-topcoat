import { useTranslation } from "react-i18next";
import type { NodeState } from "../types";

export function StateBadge({ state }: { state: NodeState }) {
  const { t } = useTranslation();
  const labels: Record<NodeState, string> = {
    available: t("state.available"),
    busy_gaming: t("state.busy_gaming"),
    degraded: t("state.degraded"),
    yielded: t("state.yielded"),
    paused: t("state.paused"),
    offline: t("state.offline"),
  };
  return <span className={`badge badge-${state}`}>{labels[state] ?? state}</span>;
}
