// @vitest-environment node
import { describe, expect, it, vi } from "vitest";
import { jsonResponse, mockFetch } from "../test/mockFetch";
import { ApiClient, ApiError, UnauthorizedError } from "./client";

describe("ApiClient", () => {
  it("sends the bearer token on every request", async () => {
    const { fetch, calls } = mockFetch({
      "GET /api/me": { id: "u1", name: "Admin", role: "admin" },
      "GET /api/nodes": [],
    });
    const api = new ApiClient({ token: "secret-dev", fetchImpl: fetch });
    await expect(api.me()).resolves.toEqual({ id: "u1", name: "Admin", role: "admin" });
    await api.listNodes();
    expect(calls).toHaveLength(2);
    for (const c of calls) expect(c.headers.authorization).toBe("Bearer secret-dev");
  });

  it("uses the global fetch when none is injected", async () => {
    const { fetch, calls } = mockFetch({ "GET /api/nodes": [] });
    vi.stubGlobal("fetch", fetch);
    try {
      await new ApiClient({ token: "t" }).listNodes();
      expect(calls[0]?.url).toBe("/api/nodes");
    } finally {
      vi.unstubAllGlobals();
    }
  });

  it("calls onUnauthorized and throws on 401", async () => {
    const onUnauthorized = vi.fn();
    const { fetch } = mockFetch({
      "GET /api/nodes": () => jsonResponse({ detail: "invalid token" }, 401),
    });
    const api = new ApiClient({ token: "bad", onUnauthorized, fetchImpl: fetch });
    await expect(api.listNodes()).rejects.toBeInstanceOf(UnauthorizedError);
    expect(onUnauthorized).toHaveBeenCalledTimes(1);
  });

  it("encodes node ids in paths", async () => {
    const { fetch, calls } = mockFetch({});
    const api = new ApiClient({ token: "t", fetchImpl: fetch });
    await expect(api.getNode("a/b")).rejects.toMatchObject({ status: 404, code: "not_found" });
    expect(calls[0]?.url).toBe("/api/nodes/a%2Fb");
  });

  it("posts actions with name and args and returns the result", async () => {
    const { fetch, calls } = mockFetch({
      "POST /api/nodes/n1/actions": { ok: true, result: { loaded: true } },
    });
    const api = new ApiClient({ token: "t", fetchImpl: fetch });
    const res = await api.runAction("n1", "models.load", { model: "qwen-7b" });
    expect(res).toEqual({ ok: true, result: { loaded: true } });
    expect(calls[0]?.body).toEqual({ name: "models.load", args: { model: "qwen-7b" } });
    expect(calls[0]?.headers["content-type"]).toBe("application/json");
  });

  it("passes through ok:false results from the node", async () => {
    const { fetch } = mockFetch({
      "POST /api/nodes/n1/actions": {
        ok: false,
        error: { code: "load_failed", message: "out of memory" },
      },
    });
    const api = new ApiClient({ token: "t", fetchImpl: fetch });
    await expect(api.runAction("n1", "models.load", { model: "x" })).resolves.toEqual({
      ok: false,
      error: { code: "load_failed", message: "out of memory" },
    });
  });

  it.each([
    [409, { detail: { code: "node_offline", message: "node is offline" } }, "node_offline", "node is offline"],
    [409, { detail: { code: "upgrade_required", message: "too old" } }, "upgrade_required", "too old"],
    [504, { detail: { code: "timeout", message: "no reply" } }, "timeout", "no reply"],
    [404, { detail: "Node not found" }, "not_found", "Node not found"],
    [500, {}, "http_500", "Internal Server Error"],
  ])("maps HTTP %i action errors to {ok:false,error}", async (status, body, code, message) => {
    const { fetch } = mockFetch({
      "POST /api/nodes/n1/actions": () =>
        new Response(JSON.stringify(body), { status, statusText: status === 500 ? "Internal Server Error" : "" }),
    });
    const api = new ApiClient({ token: "t", fetchImpl: fetch });
    const res = await api.runAction("n1", "models.unload", { model: "x" });
    expect(res).toEqual({ ok: false, error: { code, message } });
  });

  it("maps network failures to code 'network'", async () => {
    const failing = vi.fn(() => Promise.reject(new TypeError("Failed to fetch")));
    const api = new ApiClient({ token: "t", fetchImpl: failing as unknown as typeof fetch });
    await expect(api.runAction("n1", "models.load", { model: "x" })).resolves.toEqual({
      ok: false,
      error: { code: "network", message: "Failed to fetch" },
    });
    await expect(api.listNodes()).rejects.toBeInstanceOf(ApiError);
  });

  it("still throws on 401 from the actions endpoint", async () => {
    const onUnauthorized = vi.fn();
    const { fetch } = mockFetch({
      "POST /api/nodes/n1/actions": () => new Response("", { status: 401 }),
    });
    const api = new ApiClient({ token: "t", onUnauthorized, fetchImpl: fetch });
    await expect(api.runAction("n1", "models.load", { model: "x" })).rejects.toBeInstanceOf(
      UnauthorizedError,
    );
    expect(onUnauthorized).toHaveBeenCalled();
  });

  it("creates pairing codes via POST (201)", async () => {
    const { fetch, calls } = mockFetch({
      "POST /api/pairing-codes": () =>
        jsonResponse({ code: "ABCD-1234", expires_at: "2026-01-01T12:10:00Z" }, 201),
    });
    const api = new ApiClient({ token: "t", fetchImpl: fetch });
    await expect(api.createPairingCode()).resolves.toEqual({
      code: "ABCD-1234",
      expires_at: "2026-01-01T12:10:00Z",
    });
    expect(calls[0]?.method).toBe("POST");
  });

  it("puts a node config and surfaces invalid_config", async () => {
    const { fetch, calls } = mockFetch({
      "PUT /api/nodes/n%201/config": { config: { a: 1 }, config_hash: "h" },
      "PUT /api/nodes/n2/config": () =>
        jsonResponse({ detail: { code: "invalid_config", message: "node_config: <root>: bad" } }, 400),
    });
    const api = new ApiClient({ token: "t", fetchImpl: fetch });
    await expect(api.putNodeConfig("n 1", { a: 1 })).resolves.toEqual({
      config: { a: 1 },
      config_hash: "h",
    });
    expect(calls[0]?.body).toEqual({ a: 1 });
    expect(calls[0]?.headers["content-type"]).toBe("application/json");
    const err = await api.putNodeConfig("n2", {}).catch((e: unknown) => e);
    expect(err).toBeInstanceOf(ApiError);
    expect((err as ApiError).code).toBe("invalid_config");
    expect((err as ApiError).message).toBe("node_config: <root>: bad");
  });
  it("puts and deletes resident models with encoded names", async () => {
    const body = { config: { residents: [] }, config_hash: "h", changed: true };
    const { fetch, calls } = mockFetch({
      "PUT /api/nodes/n%201/residents/qwen%2F7b": body,
      "DELETE /api/nodes/n%201/residents/qwen%2F7b": { ...body, changed: false },
    });
    const api = new ApiClient({ token: "t", fetchImpl: fetch });
    await expect(api.putResident("n 1", "qwen/7b", { priority: 70 })).resolves.toEqual(body);
    expect(calls[0]?.body).toEqual({ priority: 70 });
    await expect(api.deleteResident("n 1", "qwen/7b")).resolves.toMatchObject({ changed: false });
    expect(calls[1]?.method).toBe("DELETE");
    expect(calls[1]?.body).toBeUndefined();
  });
  it("creates, lists, reads and cancels model transfers", async () => {
    const row = { id: "t 1", status: "pending", model: "m" };
    const { fetch, calls } = mockFetch({
      "POST /api/model-transfers": row,
      "GET /api/model-transfers?node_id=n%201": { transfers: [row] },
      "GET /api/model-transfers": { transfers: [row] },
      "GET /api/model-transfers/t%201": row,
      "POST /api/model-transfers/t%201/cancel": { ...row, status: "canceled" },
    });
    const api = new ApiClient({ token: "t", fetchImpl: fetch });
    await api.createModelTransfer({ source_node_id: "a", target_node_id: "b", model: "m", move: true });
    expect(calls[0]?.body).toEqual({ source_node_id: "a", target_node_id: "b", model: "m", move: true });
    await expect(api.listModelTransfers("n 1")).resolves.toEqual([row]);
    await expect(api.listModelTransfers()).resolves.toEqual([row]);
    await expect(api.getModelTransfer("t 1")).resolves.toEqual(row);
    await expect(api.cancelModelTransfer("t 1")).resolves.toMatchObject({ status: "canceled" });
    expect(calls[4]?.method).toBe("POST");
  });
});
