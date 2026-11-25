import { useEffect, useState } from "react";
import { useTranslation } from "react-i18next";
import type { ApiClient } from "../api/client";
import { libraryHref, tasksHref } from "../route";
import type { AgentTask, ChatMessage } from "../types";

interface Props {
  message: ChatMessage;
  api?: ApiClient;
}

/** A chat request that went to the task queue: the hub's own sentence, where the task is and how
 *  it is going. The task waits in the queue until a node is free; there is nothing to start by hand. */
export function HandoffCard({ message, api }: Props) {
  const { t } = useTranslation();
  const taskId = message.agent_task_id ?? null;
  const [task, setTask] = useState<AgentTask | null>(null);

  useEffect(() => {
    if (!api || !taskId) return;
    let live = true;
    api.agentTask(taskId).then(
      (r) => live && setTask(r),
      () => undefined,
    );
    return () => {
      live = false;
    };
  }, [api, taskId]);

  return (
    <article
      className="message message-assistant message-handoff"
      data-testid="message-handoff"
    >
      <div className="message-body">
        <p>{message.content}</p>
        {task?.status === "running" && (
          <p className="muted small">{t("handoff.running")}</p>
        )}
        {task?.status === "done" && (
          <p className="muted small">
            {t("handoff.done")} <a href={libraryHref()}>{t("header.library")}</a>
          </p>
        )}
        {task?.status === "failed" && (
          <p className="muted small">{t("handoff.failed")}</p>
        )}
        <p className="actions">
          {taskId && <a href={tasksHref(taskId)}>{t("handoff.open")}</a>}
        </p>
      </div>
    </article>
  );
}
