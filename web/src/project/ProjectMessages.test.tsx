import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { ApiClient } from "../api/client";
import i18n from "../i18n";
import {
  CHAIN,
  fakeApi,
  MANDATE,
  node,
  project,
} from "../test/projectFixtures";
import { Conversations } from "./Conversations";
import { MessageBlock } from "./MessageBlock";
import { ProjectPage } from "./ProjectPage";
import type { MessageState, ProjectMessageInfo } from "./types";

afterEach(() => vi.restoreAllMocks());
beforeEach(async () => {
  await i18n.changeLanguage("en");
});

function info(overrides: Partial<ProjectMessageInfo> = {}): ProjectMessageInfo {
  return {
    type: "project.request",
    from: "project",
    to: "research",
    chain_id: "c1",
    in_reply_to: null,
    round: 1,
    state: "awaiting_click" as MessageState,
    task_id: null,
    payload: { topic: "Compare servers" },
    schema_version: 1,
    trust: "own",
    stats: null,
    verification: null,
    ...overrides,
  };
}

function block(
  message: ProjectMessageInfo,
  api = fakeApi({ project: project(), nodes: [] }),
) {
  const onChanged = vi.fn();
  const onError = vi.fn();
  const n = node({
    id: "n1",
    title: "Msg",
    kind: "report",
    report_kind: "request",
    message,
  });
  render(
    <MessageBlock
      api={api as unknown as ApiClient}
      node={n}
      onChanged={onChanged}
      onError={onError}
    />,
  );
  return { api, onChanged, onError };
}

describe("MessageBlock", () => {
  it("shows a request that waits and lets the person confirm or decline it", async () => {
    const { api, onChanged } = block(info());
    expect(screen.getByText("Request")).toBeTruthy();
    expect(screen.getByText(/waits for your click/)).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "Confirm" }));
    await waitFor(() =>
      expect(api.decideProjectRequest).toHaveBeenCalledWith("n1", true),
    );
    await waitFor(() => expect(onChanged).toHaveBeenCalled());
    fireEvent.click(screen.getByRole("button", { name: "Decline" }));
    await waitFor(() =>
      expect(api.decideProjectRequest).toHaveBeenCalledWith("n1", false),
    );
  });

  it("offers no click for a request that is on its way", () => {
    block(info({ state: "running", task_id: "abcdef1234" }));
    expect(screen.queryByRole("button", { name: "Confirm" })).toBeNull();
    expect(screen.getByText(/abcdef12/)).toBeTruthy();
  });

  it("shows what comes from outside as text and says so", () => {
    block(
      info({
        type: "research.result",
        from: "research",
        to: "project",
        trust: "foreign",
        state: "reviewing",
        payload: {
          summary: "<b>bold</b> <img src=x onerror=alert(1)>",
          findings: [{ claim: "c" }],
        },
        verification: {
          ok: false,
          issues: ["quote not found in the evidence: finding 0"],
        },
      }),
    );
    expect(screen.getByText(/nothing in it is carried out/)).toBeTruthy();
    expect(
      screen.getByText("<b>bold</b> <img src=x onerror=alert(1)>"),
    ).toBeTruthy();
    expect(document.querySelector("img")).toBeNull();
    expect(screen.getByRole("status").textContent).toContain("quote not found");
    expect(screen.getByText(/A reviewer reads it/)).toBeTruthy();
  });

  it("shows the code's good news plainly", () => {
    block(
      info({
        type: "dev.result",
        from: "dev",
        to: "project",
        state: "accepted",
        verification: { ok: true },
      }),
    );
    expect(screen.getByRole("status").textContent).toContain("all hold");
  });

  it("shows the scope of an order exactly and releases it once, without a mandate", async () => {
    const { api } = block(
      info({
        type: "project.order",
        to: "dev",
        payload: {
          action: "create_prototype",
          repo: "app",
          branch: "topcoat/paging",
          spec: { goal: "g" },
        },
      }),
    );
    expect(screen.getAllByText("topcoat/paging").length).toBeGreaterThan(0);
    expect(screen.getAllByText("app").length).toBeGreaterThan(0);
    expect(
      screen.getByText(
        /other branches, other repositories, merging, force pushes, deploying/,
      ),
    ).toBeTruthy();
    fireEvent.click(
      screen.getByRole("button", { name: "Release this order only" }),
    );
    await waitFor(() =>
      expect(api.decideProjectRequest).toHaveBeenCalledWith("n1", true),
    );
    expect(api.releaseProjectOrder).not.toHaveBeenCalled();
  });

  it("gives a mandate with the length, the words and the push choice of the person", async () => {
    const { api } = block(
      info({
        type: "project.order",
        to: "dev",
        payload: {
          action: "fix",
          repo: "app",
          branch: "topcoat/paging",
          spec: { goal: "g" },
        },
      }),
    );
    fireEvent.change(screen.getByLabelText("Runs for (days)"), {
      target: { value: "7" },
    });
    fireEvent.click(screen.getByLabelText(/may push this branch/));
    fireEvent.change(screen.getByLabelText("In your own words (optional)"), {
      target: { value: "Paging only" },
    });
    fireEvent.click(
      screen.getByRole("button", { name: "Release and give the mandate" }),
    );
    await waitFor(() =>
      expect(api.releaseProjectOrder).toHaveBeenCalledWith("n1", {
        days: 7,
        text: "Paging only",
        push: false,
      }),
    );
  });

  it("keeps the length of a mandate between one and ninety days", () => {
    block(
      info({
        type: "project.order",
        to: "dev",
        payload: { repo: "app", branch: "topcoat/x" },
      }),
    );
    const days = screen.getByLabelText("Runs for (days)") as HTMLInputElement;
    fireEvent.change(days, { target: { value: "400" } });
    expect(days.value).toBe("90");
    fireEvent.change(days, { target: { value: "0" } });
    expect(days.value).toBe("1");
  });

  it("lets the person turn a proposal into a task or decline it", async () => {
    const { api } = block(
      info({
        type: "research.proposal",
        from: "research",
        to: "project",
        trust: "foreign",
        payload: { title: "Check backups" },
      }),
    );
    fireEvent.click(screen.getByRole("button", { name: "Make a task of it" }));
    await waitFor(() =>
      expect(api.decideProjectProposal).toHaveBeenCalledWith("n1", true),
    );
    fireEvent.click(screen.getByRole("button", { name: "Decline" }));
    await waitFor(() =>
      expect(api.decideProjectProposal).toHaveBeenCalledWith("n1", false),
    );
  });

  it("passes a refusal of the hub on and does not claim a change", async () => {
    const api = fakeApi({ project: project(), nodes: [] });
    api.decideProjectRequest.mockRejectedValueOnce(new Error("refused"));
    const { onChanged, onError } = block(info(), api);
    fireEvent.click(screen.getByRole("button", { name: "Confirm" }));
    await waitFor(() => expect(onError).toHaveBeenCalled());
    expect(onChanged).not.toHaveBeenCalled();
  });

  it("renders nothing for a node that is not a message", () => {
    const api = fakeApi({ project: project(), nodes: [] });
    const { container } = render(
      <MessageBlock
        api={api as unknown as ApiClient}
        node={node({ id: "t", title: "Plain" })}
        onChanged={vi.fn()}
        onError={vi.fn()}
      />,
    );
    expect(container.textContent).toBe("");
  });
});

describe("Conversations", () => {
  function open(chains = [CHAIN], mandates = [MANDATE]) {
    const api = fakeApi({ project: project(), nodes: [] });
    api.projectChains.mockResolvedValue({ chains });
    api.projectMandates.mockResolvedValue({ mandates });
    const onChanged = vi.fn();
    render(
      <Conversations
        api={api as unknown as ApiClient}
        projectId="p1"
        tick={0}
        onError={vi.fn()}
        onChanged={onChanged}
      />,
    );
    return { api, onChanged };
  }

  it("lists the conversations with their rounds and a hint when they run long", async () => {
    open();
    expect(await screen.findByText("Paging prototype")).toBeTruthy();
    expect(screen.getByText(/Rounds: 4/)).toBeTruthy();
    expect(screen.getByRole("note").textContent).toContain("Many rounds");
  });

  it("stops a running conversation and nothing else", async () => {
    const { api, onChanged } = open([
      CHAIN,
      { ...CHAIN, id: "c2", title: "Old", status: "done", flag_reason: null },
    ]);
    await screen.findByText("Paging prototype");
    expect(screen.getAllByRole("button", { name: "Stop" })).toHaveLength(1);
    fireEvent.click(screen.getByRole("button", { name: "Stop" }));
    await waitFor(() =>
      expect(api.stopProjectChain).toHaveBeenCalledWith("c1"),
    );
    await waitFor(() => expect(onChanged).toHaveBeenCalled());
  });

  it("lists the mandates and revokes an active one", async () => {
    const { api } = open(
      [],
      [MANDATE, { ...MANDATE, id: "m2", state: "revoked", revoked_at: "x" }],
    );
    expect(await screen.findAllByText("topcoat/paging")).toHaveLength(2);
    expect(screen.getAllByRole("button", { name: "Revoke" })).toHaveLength(1);
    fireEvent.click(screen.getByRole("button", { name: "Revoke" }));
    await waitFor(() =>
      expect(api.revokeProjectMandate).toHaveBeenCalledWith("m1"),
    );
  });

  it("says so when there is nothing yet", async () => {
    open([], []);
    expect(await screen.findByText("No conversations yet.")).toBeTruthy();
    expect(screen.getByText("No mandates given.")).toBeTruthy();
  });
});

describe("ProjectPage conversations tab", () => {
  it("opens the conversations of the project from its own tab", async () => {
    const api = fakeApi({ project: project(), nodes: [] });
    api.projectChains.mockResolvedValue({ chains: [CHAIN] });
    render(<ProjectPage api={api as unknown as ApiClient} />);
    fireEvent.click(await screen.findByRole("button", { name: /Topcoat/ }));
    await screen.findByRole("heading", { name: "Topcoat", level: 2 });
    fireEvent.click(screen.getByRole("tab", { name: "Conversations" }));
    expect(await screen.findByText("Paging prototype")).toBeTruthy();
    expect(api.projectChains).toHaveBeenCalledWith("p1");
  });
});
