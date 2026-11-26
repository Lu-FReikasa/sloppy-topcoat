import { render, screen, waitFor } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { ApiClient } from "../../api/client";
import { AGENT, bootstrap, workspace } from "../../test/layoutFixtures";
import { jsonResponse, mockFetch } from "../../test/mockFetch";
import type { ToolScope } from "../../types";
import { renderModule, type ModuleCtx } from "./render";

const CONV = {
  id: "c9",
  title: "",
  model: null,
  node: null,
  created_at: "2026-01-01T10:00:00Z",
  updated_at: "2026-01-01T10:00:00Z",
  message_count: 0,
};

function mount(agentScope: ToolScope, agentStatus = 200) {
  const mock = mockFetch({
    "GET /api/ui/workspaces/office/agent": () =>
      jsonResponse(
        agentStatus === 200
          ? {
              workspace: "office",
              ...AGENT,
              tool_scope: agentScope,
              denied: [],
              planned: [],
            }
          : { detail: "down" },
        agentStatus,
      ),
    "GET /api/conversations": [],
    "GET /api/chat/models": [],
    "GET /api/chat/settings": {
      user: {
        system_prompt: null,
        temperature: null,
        address: "du",
        memory_enabled: true,
        quality: "balanced",
        internet: true,
      },
      hub: {},
      effective_model: null,
    },
    "GET /api/chat/tools": { groups: [], nodes: [], internet: { node: null } },
    "POST /api/conversations": () => jsonResponse(CONV, 201),
  });
  const api = new ApiClient({ token: "t", fetchImpl: mock.fetch });
  const ctx = {
    api,
    me: null,
    isAdmin: false,
    nodes: [],
    nodesLoaded: true,
    nodesError: null,
    refreshNodes: () => undefined,
    tick: 0,
    inboxTick: 0,
    dispatch: () => undefined,
  } as ModuleCtx;
  const boot = bootstrap([workspace("office", ["page.chat"])]);
  render(
    <>{renderModule("page.chat", ctx, {}, { workspace: "office", boot })}</>,
  );
  return mock;
}

describe("the chat window", () => {
  it("starts the calm view with a conversation of its workspace, with what the workspace allows", async () => {
    const scope = {
      nodes: null,
      groups: ["docs", "mail"],
      internet: false,
      memory: null,
    };
    const mock = mount(scope);
    await waitFor(() =>
      expect(mock.calls.some((c) => c.method === "POST")).toBe(true),
    );
    // the scope is known before the first conversation is made: nothing is created without it
    expect(mock.calls.find((c) => c.method === "POST")?.body).toEqual({
      tool_scope: scope,
      application: "office",
    });
    expect(await screen.findByLabelText("Schreib mir …")).toBeTruthy();
    expect(screen.queryByRole("button", { name: "Neuer Chat" })).toBeNull(); // no list, no pickers
  });

  it("still opens a conversation when the hub cannot say what the workspace allows", async () => {
    const mock = mount(AGENT.tool_scope, 503);
    await waitFor(() =>
      expect(mock.calls.find((c) => c.method === "POST")?.body).toEqual({
        application: "office",
      }),
    );
  });
});
