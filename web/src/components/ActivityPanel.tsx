import { useEffect, useState } from "react";
import { useTranslation } from "react-i18next";
import type { ApiClient } from "../api/client";
import { schedulingHref } from "../route";
import type { ActivityView } from "../types";

const POLL_MS = 4000;

/** Standing tasks in the order the page shows them. */
const ROLE_KEYS = ["chat", "digest", "checker", "care"] as const;

export function duration(seconds: number): string {
  return seconds < 90 ? `${seconds} s` : `${Math.round(seconds / 60)} min`;
}

/** Reads GET /api/activity every few seconds: what every node does for the hub right now. The
 *  Machines page shows it inside its rows; `failed` is set while the last read failed. */
export function useActivity(api: ApiClient | undefined): {
  view: ActivityView | null;
  failed: boolean;
} {
  const [view, setView] = useState<ActivityView | null>(null);
  const [failed, setFailed] = useState(false);

  useEffect(() => {
    if (!api) return;
    let alive = true;
    const load = () =>
      api.getActivity().then(
        (v) => {
          if (!alive) return;
          setView(v);
          setFailed(false);
        },
        () => {
          if (alive) setFailed(true);
        },
      );
    void load();
    const timer = setInterval(load, POLL_MS);
    return () => {
      alive = false;
      clearInterval(timer);
    };
  }, [api]);

  return { view, failed };
}

/** The standing tasks (default model and node per task) and the background note.
 *  These move to the Models page with UI-02; until then they stay here as a collapsed section
 *  and a one-line link. */
export function ActivityPanel({ view }: { view: ActivityView }) {
  const { t } = useTranslation();
  const background =
    view.background_nodes.length > 0 ? view.background_nodes.join(", ") : t("activity.backgroundAny");
  return (
    <>
      <details className="card activity-standing" data-testid="activity-panel">
      <summary>{t("activity.roles")}</summary>
      <table className="activity-table">
        <tbody>
          {ROLE_KEYS.map((key) => {
            const role = view.roles.find((r) => r.key === key);
            return (
              <tr key={key} data-testid={`activity-role-${key}`}>
                <td>{t(`activity.role.${key}`)}</td>
                <td>
                  {role?.model ? (
                    <>
                      {role.model}
                      {role.node ? ` @ ${role.node}` : ""}
                    </>
                  ) : (
                    <span className="muted">{t("activity.automatic")}</span>
                  )}
                </td>
              </tr>
            );
          })}
        </tbody>
      </table>
      <p className="muted">{t("activity.backgroundNodes", { nodes: background })}</p>
      </details>
      <p>
        <a href={schedulingHref}>{t("activity.changeLink")}</a>
      </p>
    </>
  );
}
