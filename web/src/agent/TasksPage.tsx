import { useTranslation } from "react-i18next";
import { TaskView } from "./TaskView";
import type { ApiClient } from "../api/client";

interface Props {
  api: ApiClient;
  /** The task to show: a link from the inbox, the chat or a check opens one. */
  taskId?: string;
  /** Changes whenever the live stream reports agent progress (refetch). */
  tick: number;
}

/** One task of the agent (E3-01a): its diagnosis, evidence and steps, and the proposals it made
 *  with their buttons. There is no list of tasks any more: the inbox leads here when a task needs
 *  a look or a click, and a check in a chat links to its own task. */
export function TasksPage({ api, taskId, tick }: Props) {
  const { t } = useTranslation();
  // `#/tasks` without a task: say where tasks come from instead of a blank page
  if (!taskId)
    return (
      <section className="empty">
        <h2>{t("agent.noTask")}</h2>
        <p>{t("agent.noTaskHint")}</p>
      </section>
    );
  return (
    <TaskView api={api} id={taskId} tick={tick} onChanged={() => undefined} />
  );
}
