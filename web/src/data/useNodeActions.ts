/**
 * What the node detail page can do to a node (issue #9), next to `useNode`: each action calls the
 * hub, then puts the answer in place (`mutate`) or loads the detail again (`reload`), and keeps
 * its error for the page to show. Questions to the person (a delete, sleep) stay with the page.
 */
import { useCallback, useMemo, useState } from "react";
import { UnauthorizedError, type ApiClient } from "../api/client";
import { toActionError } from "../errors";
import type { ActionError, NodeDetail, ResidentUpdate } from "../types";
import type { Resource } from "./useResource";

export type ModelAction = "models.load" | "models.unload" | "models.delete";

/** What a power action reports when it went through (the page says it in words). */
export type NodeNotice = "wakeSent" | "sleepStarted";

type NodeApi = Pick<
  ApiClient,
  "runAction" | "wakeNode" | "putResident" | "deleteResident" | "putNodeConfig"
>;

export interface NodeActions {
  /** What runs now: a model's name (load, unload, delete, resident) or "wake" / "sleep". The
   *  model and power buttons wait while one runs. */
  readonly pending: string | null;
  /** Why the last model, resident or power action failed; cleared when the next one starts. */
  readonly error: ActionError | null;
  readonly notice: NodeNotice | null;
  /** Keep a model loaded (an entry) or stop it (null); the hub's config answer is shown at once. */
  readonly changeResident: (model: string, entry: ResidentUpdate | null) => Promise<void>;
  /** Load, unload or delete a model (ask before a delete); the detail reloads after it. */
  readonly runModelAction: (action: ModelAction, model: string) => Promise<void>;
  /** Wake-on-LAN: the hub sends the magic packet to the addresses it knows from the inventory. */
  readonly wake: () => Promise<void>;
  /** Put the node to sleep (ask first). */
  readonly sleep: () => Promise<void>;
  /** Compute node (holds and serves models) or tool node: `config.roles`. Its own busy and error,
   *  so the switch does not hold up the other buttons. */
  readonly compute: {
    readonly busy: boolean;
    readonly error: ActionError | null;
    readonly set: (on: boolean) => Promise<void>;
  };
}

/** A failure to show: everything but a 401 (the client already signs the person out). */
function shown(err: unknown): ActionError | null {
  return err instanceof UnauthorizedError ? null : toActionError(err);
}

const UNKNOWN: ActionError = { code: "unknown", message: "" };

export function useNodeActions(
  api: NodeApi,
  nodeId: string,
  node: Pick<Resource<NodeDetail>, "data" | "mutate" | "reload">,
): NodeActions {
  const [pending, setPending] = useState<string | null>(null);
  const [error, setError] = useState<ActionError | null>(null);
  const [notice, setNotice] = useState<NodeNotice | null>(null);
  const [computeBusy, setComputeBusy] = useState(false);
  const [computeError, setComputeError] = useState<ActionError | null>(null);
  const { data, mutate, reload } = node;

  /** Runs one model / resident / power action: marks it pending, keeps its error. */
  const run = useCallback(async (what: string, body: () => Promise<void>): Promise<void> => {
    setPending(what);
    setError(null);
    try {
      await body();
    } catch (err) {
      const e = shown(err);
      if (e) setError(e);
    } finally {
      setPending(null);
    }
  }, []);

  const changeResident = useCallback(
    (model: string, entry: ResidentUpdate | null) =>
      run(model, async () => {
        const before = data;
        if (before === undefined) return;
        const res =
          entry === null
            ? await api.deleteResident(nodeId, model)
            : await api.putResident(nodeId, model, entry);
        // the config the hub confirmed; node_updated carries none. A function, so it is put on
        // top of a load that is still on its way.
        mutate((cur) => ({ ...(cur ?? before), config: res.config, config_hash: res.config_hash }));
      }),
    [api, nodeId, run, mutate, data],
  );

  const runModelAction = useCallback(
    (action: ModelAction, model: string) =>
      run(model, async () => {
        const args: Record<string, unknown> = { model };
        if (action === "models.delete") args.confirm = true;
        const res = await api.runAction(nodeId, action, args);
        if (!res.ok) {
          setError(res.error ?? UNKNOWN);
          return;
        }
        // the stream will push node_updated; a reload in case it does not
        void reload();
      }),
    [api, nodeId, run, reload],
  );

  const wake = useCallback(
    () =>
      run("wake", async () => {
        setNotice(null);
        await api.wakeNode(nodeId);
        setNotice("wakeSent");
      }),
    [api, nodeId, run],
  );

  const sleep = useCallback(
    () =>
      run("sleep", async () => {
        setNotice(null);
        const res = await api.runAction(nodeId, "node.sleep", {});
        if (res.ok) setNotice("sleepStarted");
        else setError(res.error ?? UNKNOWN);
      }),
    [api, nodeId, run],
  );

  const setCompute = useCallback(
    async (on: boolean): Promise<void> => {
      const config = data?.config;
      const roles = config?.roles;
      if (!config || !Array.isArray(roles)) return;
      const list = roles.filter((r): r is string => typeof r === "string");
      const next = on ? [...list.filter((r) => r !== "gpu"), "gpu"] : list.filter((r) => r !== "gpu");
      setComputeBusy(true);
      setComputeError(null);
      try {
        await api.putNodeConfig(nodeId, { ...config, roles: next.length > 0 ? next : ["tools"] });
        void reload();
      } catch (err) {
        const e = shown(err);
        if (e) setComputeError(e);
      } finally {
        setComputeBusy(false);
      }
    },
    [api, nodeId, data, reload],
  );

  return useMemo(
    () => ({
      pending,
      error,
      notice,
      changeResident,
      runModelAction,
      wake,
      sleep,
      compute: { busy: computeBusy, error: computeError, set: setCompute },
    }),
    [pending, error, notice, changeResident, runModelAction, wake, sleep, computeBusy, computeError, setCompute],
  );
}
