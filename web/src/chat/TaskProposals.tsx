import { useCallback, useEffect, useState } from "react";
import { useTranslation } from "react-i18next";
import type { ApiClient } from "../api/client";
import { ProposalCard } from "../agent/Proposals";
import type { AgentProposal } from "../types";

/** What a check of this chat proposed (file changes of the workshop, D99): one card per change
 *  with the diff and the one click. Nothing is written before it, and each change is decided on
 *  its own. The state is read from the hub, so a reload shows what was decided. */
export function TaskProposals({ api, taskId }: { api: ApiClient; taskId: string }) {
  const { t } = useTranslation();
  const [list, setList] = useState<AgentProposal[] | null>(null);

  const load = useCallback(() => {
    api.taskProposals(taskId).then(setList, () => setList([]));
  }, [api, taskId]);

  useEffect(() => {
    load();
  }, [load]);

  if (!list || list.length === 0) return null;
  return (
    <section className="report-proposals" data-testid="task-proposals">
      <h4>{t("report.proposalsHeading")}</h4>
      <p className="muted small">{t("report.proposalsIntro")}</p>
      {list.map((p) => (
        <ProposalCard key={p.id} api={api} proposal={p} onChanged={load} />
      ))}
    </section>
  );
}
