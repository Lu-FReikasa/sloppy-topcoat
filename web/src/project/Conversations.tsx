import { useCallback, useEffect, useState } from "react";
import { useTranslation } from "react-i18next";
import type { ApiClient } from "../api/client";
import type { ProjectChain, ProjectMandate } from "./types";

/** The conversations between the applications about this project, with the one thing the person
 *  does to them (stop), and the mandates they gave, with the one thing they do to those (revoke).
 *  A flag on a chain is a hint, never a limit. */
export function Conversations({
  api,
  projectId,
  tick,
  onError,
  onChanged,
}: {
  api: ApiClient;
  projectId: string;
  tick: number;
  onError: (err: unknown) => void;
  onChanged: () => void;
}) {
  const { t } = useTranslation();
  const [chains, setChains] = useState<ProjectChain[] | null>(null);
  const [mandates, setMandates] = useState<ProjectMandate[] | null>(null);

  const load = useCallback(() => {
    void api.projectChains(projectId).then((r) => setChains(r.chains), onError);
    void api
      .projectMandates(projectId)
      .then((r) => setMandates(r.mandates), onError);
  }, [api, projectId, onError]);

  useEffect(() => {
    load();
  }, [load, tick]);

  const act = (call: () => Promise<unknown>) =>
    void call().then(() => {
      load();
      onChanged();
    }, onError);

  return (
    <div className="project-conversations">
      <h3>{t("project.chains")}</h3>
      {chains && chains.length === 0 && (
        <p className="project-muted">{t("project.noChains")}</p>
      )}
      <ul className="project-list">
        {(chains ?? []).map((c) => (
          <li key={c.id} data-chain={c.id}>
            <strong>{c.title || c.id.slice(0, 8)}</strong>{" "}
            <span className="project-muted">
              {t(`project.chainStatus.${c.status}`)} ·{" "}
              {t("project.chainRounds", { count: c.rounds })}
            </span>
            {c.flag_reason && (
              <span role="note" className="project-flag">
                {" "}
                {t(`project.chainFlag.${c.flag_reason}`, {
                  defaultValue: c.flag_reason,
                })}
              </span>
            )}
            {c.status === "active" && (
              <button
                type="button"
                onClick={() => act(() => api.stopProjectChain(c.id))}
              >
                {t("project.chainStop")}
              </button>
            )}
          </li>
        ))}
      </ul>
      <h3>{t("project.mandates")}</h3>
      {mandates && mandates.length === 0 && (
        <p className="project-muted">{t("project.noMandates")}</p>
      )}
      <ul className="project-list">
        {(mandates ?? []).map((m) => (
          <li key={m.id} data-mandate={m.id}>
            <code>{m.branch}</code> ({m.repo}) · {m.actions.join(", ")} ·{" "}
            {m.push ? t("project.mandatePushOn") : t("project.mandatePushOff")}{" "}
            · {t(`project.mandateState.${m.state}`)} ·{" "}
            {t("project.mandateUses", { count: m.uses })}
            {m.text && <q> {m.text}</q>}
            {m.state === "active" && (
              <button
                type="button"
                onClick={() => act(() => api.revokeProjectMandate(m.id))}
              >
                {t("project.mandateRevoke")}
              </button>
            )}
          </li>
        ))}
      </ul>
    </div>
  );
}
