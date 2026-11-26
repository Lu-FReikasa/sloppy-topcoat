import { act, renderHook } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { ApiError, UnauthorizedError } from "../api/client";
import { makeDetail } from "../test/fixtures";
import type { ActionResult, NodeConfigResponse, NodeDetail, ResidentResponse } from "../types";
import { createHubEvents } from "./hubEvents";
import { useNode } from "./useNode";
import { useNodeActions } from "./useNodeActions";

interface Deferred<T> {
  readonly promise: Promise<T>;
  readonly resolve: (value: T) => void;
}

function deferred<T>(): Deferred<T> {
  let resolve: (value: T) => void = () => undefined;
  const promise = new Promise<T>((res) => {
    resolve = res;
  });
  return { promise, resolve };
}

const resident = (config: Record<string, unknown>, hash: string): ResidentResponse => ({
  config,
  config_hash: hash,
  changed: true,
});

/** A fake client: `getNode` waits for the test (see `loads`), the actions answer at once. */
function fakeClient() {
  const loads: Deferred<NodeDetail>[] = [];
  const api = {
    getNode: vi.fn((): Promise<NodeDetail> => {
      const answer = deferred<NodeDetail>();
      loads.push(answer);
      return answer.promise;
    }),
    runAction: vi.fn(
      (_id: string, _name: string, _args: Record<string, unknown>): Promise<ActionResult> =>
        Promise.resolve({ ok: true }),
    ),
    wakeNode: vi.fn(() => Promise.resolve({ sent: 1, broadcast: "10.0.0.255", port: 9 })),
    putResident: vi.fn(() => Promise.resolve(resident({ residents: [{ model: "qwen-7b" }] }, "h2"))),
    deleteResident: vi.fn(() => Promise.resolve(resident({ residents: [] }, "h3"))),
    putNodeConfig: vi.fn(
      (_id: string, config: unknown): Promise<NodeConfigResponse> =>
        Promise.resolve({ config: config as Record<string, unknown>, config_hash: "h4" }),
    ),
  };
  return { api, loads };
}

async function setup(detail: NodeDetail = makeDetail()) {
  const { api, loads } = fakeClient();
  const events = createHubEvents();
  const hook = renderHook(() => {
    const node = useNode(api, "n1", { events });
    return { node, actions: useNodeActions(api, "n1", node) };
  });
  await act(async () => {
    loads[0]?.resolve(detail);
    await Promise.resolve();
  });
  return { api, loads, hook };
}

describe("useNodeActions", () => {
  it("shows the config the hub confirmed for a resident at once, without a reload", async () => {
    const { api, hook } = await setup();
    await act(() => hook.result.current.actions.changeResident("qwen-7b", {}));
    expect(api.putResident).toHaveBeenCalledWith("n1", "qwen-7b", {});
    expect(hook.result.current.node.data?.config).toEqual({ residents: [{ model: "qwen-7b" }] });
    expect(hook.result.current.node.data?.config_hash).toBe("h2");
    expect(hook.result.current.node.data?.events).toHaveLength(1);
    await act(() => hook.result.current.actions.changeResident("qwen-7b", null));
    expect(api.deleteResident).toHaveBeenCalledWith("n1", "qwen-7b");
    expect(hook.result.current.node.data?.config).toEqual({ residents: [] });
    expect(api.getNode).toHaveBeenCalledTimes(1);
  });

  it("keeps a resident change when an older load answers after it", async () => {
    const { hook, loads } = await setup();
    act(() => void hook.result.current.node.reload());
    await act(() => hook.result.current.actions.changeResident("qwen-7b", {}));
    // the load was read before the change: its config is older, its other fields are newer
    await act(async () => {
      loads[1]?.resolve(makeDetail({ name: "renamed", config: null }));
      await Promise.resolve();
    });
    expect(hook.result.current.node.data?.name).toBe("renamed");
    expect(hook.result.current.node.data?.config).toEqual({ residents: [{ model: "qwen-7b" }] });
  });

  it("marks the model pending while an action runs and reloads after it", async () => {
    const { api, hook } = await setup();
    const done = deferred<ActionResult>();
    api.runAction.mockReturnValueOnce(done.promise);
    let running: Promise<void> = Promise.resolve();
    act(() => {
      running = hook.result.current.actions.runModelAction("models.load", "qwen-7b");
    });
    expect(hook.result.current.actions.pending).toBe("qwen-7b");
    await act(async () => {
      done.resolve({ ok: true });
      await running;
    });
    expect(hook.result.current.actions.pending).toBeNull();
    expect(api.runAction).toHaveBeenCalledWith("n1", "models.load", { model: "qwen-7b" });
    expect(api.getNode).toHaveBeenCalledTimes(2);
  });

  it("sends the confirmation with a delete", async () => {
    const { api, hook } = await setup();
    await act(() => hook.result.current.actions.runModelAction("models.delete", "qwen-7b"));
    expect(api.runAction).toHaveBeenCalledWith("n1", "models.delete", {
      model: "qwen-7b",
      confirm: true,
    });
  });

  it("keeps the error of a failed action until the next one starts", async () => {
    const { api, hook } = await setup();
    const error = { code: "load_failed", message: "out of memory" };
    api.runAction.mockResolvedValueOnce({ ok: false, error });
    await act(() => hook.result.current.actions.runModelAction("models.load", "qwen-7b"));
    expect(hook.result.current.actions.error).toEqual(error);
    // no reload for a failed action
    expect(api.getNode).toHaveBeenCalledTimes(1);

    api.putResident.mockRejectedValueOnce(new ApiError(409, "node_offline", "offline"));
    await act(() => hook.result.current.actions.changeResident("qwen-7b", {}));
    expect(hook.result.current.actions.error?.code).toBe("node_offline");

    await act(() => hook.result.current.actions.wake());
    expect(hook.result.current.actions.error).toBeNull();
  });

  it("reports no error for a 401: the client signs the person out", async () => {
    const { api, hook } = await setup();
    api.wakeNode.mockRejectedValueOnce(new UnauthorizedError());
    await act(() => hook.result.current.actions.wake());
    expect(hook.result.current.actions.error).toBeNull();
    expect(hook.result.current.actions.notice).toBeNull();
  });

  it("says when a wake was sent or sleep started, and why sleep failed", async () => {
    const { api, hook } = await setup();
    await act(() => hook.result.current.actions.wake());
    expect(api.wakeNode).toHaveBeenCalledWith("n1");
    expect(hook.result.current.actions.notice).toBe("wakeSent");

    await act(() => hook.result.current.actions.sleep());
    expect(api.runAction).toHaveBeenCalledWith("n1", "node.sleep", {});
    expect(hook.result.current.actions.notice).toBe("sleepStarted");

    api.runAction.mockResolvedValueOnce({ ok: false, error: { code: "timeout", message: "t" } });
    await act(() => hook.result.current.actions.sleep());
    expect(hook.result.current.actions.notice).toBeNull();
    expect(hook.result.current.actions.error?.code).toBe("timeout");
  });

  it("switches compute on and off through config.roles, with its own error", async () => {
    const config = { roles: ["tools"], allowed_actions: [] };
    const { api, hook } = await setup(makeDetail({ config }));
    await act(() => hook.result.current.actions.compute.set(true));
    expect(api.putNodeConfig).toHaveBeenLastCalledWith("n1", { ...config, roles: ["tools", "gpu"] });
    expect(api.getNode).toHaveBeenCalledTimes(2);

    api.putNodeConfig.mockRejectedValueOnce(new ApiError(400, "invalid_config", "bad"));
    await act(() => hook.result.current.actions.compute.set(false));
    // the last role goes: the node stays a tool node
    expect(api.putNodeConfig).toHaveBeenLastCalledWith("n1", { ...config, roles: ["tools"] });
    expect(hook.result.current.actions.compute.error?.code).toBe("invalid_config");
    expect(hook.result.current.actions.compute.busy).toBe(false);
    expect(hook.result.current.actions.error).toBeNull();
  });
});
