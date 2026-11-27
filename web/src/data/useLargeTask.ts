/**
 * A large task with its sub-tasks (D109, D110, issue #33), for the sub-task view: the one call
 * `api.largeTask` loads it (hub api/vorhaben.py, issue #32), and the actions next to it put the
 * hub's answer in place. No JSX here (see useResource.ts).
 *
 * Live: the hub sends `project_changed` after every change to a project (a sub-task filed, its
 * status moved); a fix's own steps (testing, passed) send none, so the view also looks again
 * every few seconds while a sub-task runs.
 */
import { useCallback, useEffect, useState } from "react";
import { ApiError, UnauthorizedError, type ApiClient } from "../api/client";
import type { SseEvent } from "../api/sse";
import { toActionError } from "../errors";
import type { LargeTaskTick, LargeTaskView } from "../project/types";
import type { ActionError } from "../types";
import type { HubEvents } from "./hubEvents";
import { RELOAD, useResource, type Resource } from "./useResource";

export type LargeTaskApi = Pick<
  ApiClient,
  "largeTask" | "decomposeLargeTask" | "giveWorkshopScope" | "tickLargeTask" | "revokeProjectMandate"
>;

export const POLL_MS = 5000;

function projectOf(ev: SseEvent): string | null {
  try {
    const parsed = JSON.parse(ev.data) as { project_id?: unknown };
    return typeof parsed.project_id === "string" ? parsed.project_id : null;
  } catch {
    return null;
  }
}

export function useLargeTask(
  api: Pick<LargeTaskApi, "largeTask">,
  nodeId: string,
  options: { events?: HubEvents; pollMs?: number } = {},
): Resource<LargeTaskView> {
  const resource = useResource(`large-task:${nodeId}`, () => api.largeTask(nodeId), {
    events: options.events,
    live: {
      events: ["project_changed"],
      apply: (current, ev) =>
        current && projectOf(ev) === current.large_task.project_id ? RELOAD : undefined,
    },
  });
  const running = resource.data?.subtasks.some((s) => s.state === "running") ?? false;
  const { reload } = resource;
  const pollMs = options.pollMs ?? POLL_MS;
  useEffect(() => {
    if (!running) return;
    const timer = window.setInterval(() => void reload(), pollMs);
    return () => window.clearInterval(timer);
  }, [running, reload, pollMs]);
  return resource;
}

/** What the last action reports when it went through (the view says it in words). */
export type LargeTaskNotice =
  | { kind: "filed"; count: number }
  | { kind: "changeSet" }
  | { kind: "tick"; tick: LargeTaskTick }
  | { kind: "scope" }
  | { kind: "revoked" };

export type LargeTaskAction = "decompose" | "scope" | "revoke" | "tick";

export interface LargeTaskActions {
  readonly pending: LargeTaskAction | null;
  readonly error: ActionError | null;
  /** The hub's check refused a decomposition: all its problems at once (D109). */
  readonly problems: string[];
  readonly notice: LargeTaskNotice | null;
  readonly decompose: () => Promise<void>;
  /** `language`: the one the card was shown in, so the hub stores that text (#190). */
  readonly giveScope: (language?: "en" | "de") => Promise<void>;
  readonly revokeScope: (mandateId: string) => Promise<void>;
  readonly tick: () => Promise<void>;
}

export function useLargeTaskActions(
  api: LargeTaskApi,
  nodeId: string,
  task: Pick<Resource<LargeTaskView>, "mutate" | "reload">,
  onChanged: () => void = () => undefined,
): LargeTaskActions {
  const [pending, setPending] = useState<LargeTaskAction | null>(null);
  const [error, setError] = useState<ActionError | null>(null);
  const [problems, setProblems] = useState<string[]>([]);
  const [notice, setNotice] = useState<LargeTaskNotice | null>(null);
  const { mutate, reload } = task;

  const run = useCallback(
    async (what: LargeTaskAction, body: () => Promise<LargeTaskNotice>): Promise<void> => {
      setPending(what);
      setError(null);
      setProblems([]);
      setNotice(null);
      try {
        setNotice(await body());
        onChanged();
      } catch (err) {
        if (err instanceof UnauthorizedError) return;
        const listed = err instanceof ApiError ? err.detail?.problems : undefined;
        if (Array.isArray(listed) && listed.length > 0) setProblems(listed.map(String));
        else setError(toActionError(err));
      } finally {
        setPending(null);
      }
    },
    [onChanged],
  );

  const decompose = useCallback(
    () =>
      run("decompose", async () => {
        const out = await api.decomposeLargeTask(nodeId);
        mutate(out.large_task);
        return out.change_set ? { kind: "changeSet" } : { kind: "filed", count: out.filed.length };
      }),
    [api, nodeId, mutate, run],
  );
  const giveScope = useCallback(
    (language?: "en" | "de") =>
      run("scope", async () => {
        await api.giveWorkshopScope(nodeId, language);
        await reload();
        return { kind: "scope" };
      }),
    [api, nodeId, reload, run],
  );
  const revokeScope = useCallback(
    (mandateId: string) =>
      run("revoke", async () => {
        await api.revokeProjectMandate(mandateId);
        await reload();
        return { kind: "revoked" };
      }),
    [api, reload, run],
  );
  const tick = useCallback(
    () =>
      run("tick", async () => {
        const out = await api.tickLargeTask(nodeId);
        mutate(out.large_task);
        return { kind: "tick", tick: out.tick };
      }),
    [api, nodeId, mutate, run],
  );

  return { pending, error, problems, notice, decompose, giveScope, revokeScope, tick };
}
