import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import type { ApiClient } from "../api/client";
import { IssuePanel } from "./IssuePanel";
import { isIssue, type IssueFix, type IssuePlan, type ProjectNode } from "./types";

const task = {
  id: "t1",
  kind: "task",
  number: "2.1",
  status: "in_review",
  revision: 3,
  project_id: "p1",
  body: "Zielanwendung: dev\nProgramm: issue_check\nErgebnisform: Diagnose\nArt: issue\n",
} as unknown as ProjectNode;

function fix(state: IssueFix["state"], extra: Partial<IssueFix> = {}): IssueFix {
  return {
    id: `f-${state}`,
    issue_id: "t1",
    summary: "Fenster folgen beim Ziehen",
    state,
    edits: [{ path: "hub/src/topcoat_hub/x.py", old: "a", new: "b" }],
    files: [],
    branch: null,
    base_commit: null,
    commit: null,
    diff: "",
    tests: null,
    rollout: null,
    error: null,
    created_at: null,
    updated_at: null,
    ...extra,
  };
}

function setup(fixes: IssueFix[], plan: IssuePlan | null = null, probe_mandate: unknown = null) {
  const api = {
    issueFixes: vi.fn().mockResolvedValue({ fixes, plan, probe_mandate, probe_scope: "Die Untersuchung darf …" }),
    allowProbes: vi.fn().mockResolvedValue({ mandate: {} }),
    revokeProjectMandate: vi.fn().mockResolvedValue({ mandate: {} }),
    iterateProjectNode: vi.fn().mockResolvedValue({ task: "t1", run: "r" }),
    applyFix: vi.fn().mockResolvedValue(fixes[0]),
    rolloutFix: vi.fn().mockResolvedValue(fixes[0]),
    discardFix: vi.fn(),
    confirmIssue: vi.fn().mockResolvedValue({ task, run: null }),
    giveIssuePlan: vi.fn().mockResolvedValue({ plan }),
  };
  render(<IssuePanel api={api as unknown as ApiClient} task={task} onChanged={vi.fn()} onError={vi.fn()} />);
  return api;
}

describe("the person gives the plan (Plan vorgeben)", () => {
  it("starts from the issue's plan, sends the edited JSON, and closes the form", async () => {
    const plan: IssuePlan = {
      summary: "Einklappen",
      steps: [{ path: "web/src/project/ListView.tsx", anchor: "hasChildren: boolean;", do: "childCount" }],
      test: { path: "web/src/project/collapse.test.tsx", do: "visibleNodes" },
    };
    const api = setup([], plan);
    fireEvent.click(await screen.findByRole("button", { name: "Plan vorgeben" }));
    const box = await screen.findByRole("textbox", { name: /Der Plan als JSON/ });
    expect(JSON.parse((box as HTMLTextAreaElement).value)).toEqual(plan);
    const edited = { ...plan, summary: "Einklappen pro Projekt" };
    fireEvent.change(box, { target: { value: JSON.stringify(edited) } });
    fireEvent.click(screen.getByRole("button", { name: "Plan prüfen und ablegen" }));
    await waitFor(() => expect(api.giveIssuePlan).toHaveBeenCalledWith("t1", edited));
    await waitFor(() => expect(screen.queryByTestId("issue-own-plan")).toBeNull());
  });

  it("does not send a plan that is no JSON", async () => {
    const api = setup([]);
    fireEvent.click(await screen.findByRole("button", { name: "Plan vorgeben" }));
    fireEvent.change(await screen.findByRole("textbox", { name: /Der Plan als JSON/ }), {
      target: { value: "{ kein json" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Plan prüfen und ablegen" }));
    expect(api.giveIssuePlan).not.toHaveBeenCalled();
    expect(screen.getByTestId("issue-own-plan")).toBeTruthy();
  });
});

describe("an issue's way to the fix", () => {
  it("is recognised by its line Art: issue", () => {
    expect(isIssue(task)).toBe(true);
    expect(isIssue({ kind: "task", body: "Zielanwendung: dev" })).toBe(false);
  });

  it("drafting a fix is an investigation, with the person's hint; no plan, no button to carry it out", async () => {
    const api = setup([]);
    expect(screen.queryByRole("button", { name: "Ursache suchen" })).toBeNull();
    fireEvent.change(await screen.findByRole("textbox", { name: "Hinweis für die Untersuchung (optional)" }), {
      target: { value: "nur beim Ziehen" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Fix entwerfen" }));
    await waitFor(() =>
      expect(api.iterateProjectNode).toHaveBeenCalledWith("t1", {
        feedback: "nur beim Ziehen",
        keep: false,
        program: "issue_investigate",
      }),
    );
    expect(screen.queryByRole("button", { name: "Plan umsetzen" })).toBeNull();
  });

  it("tries need the person's click on a card that shows their whole scope, and can be revoked", async () => {
    const api = setup([]);
    expect(await screen.findByText("Die Untersuchung darf …")).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "Versuche erlauben" }));
    await waitFor(() => expect(api.allowProbes).toHaveBeenCalledWith("t1"));
  });

  it("an active probe mandate is shown with its uses and revoked on a click", async () => {
    const api = setup([], null, { id: "m1", expires_at: "2026-11-20T12:00:00Z", uses: 2, state: "active" });
    expect(await screen.findByText(/bisher 2 Versuche/)).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "Versuche widerrufen" }));
    await waitFor(() => expect(api.revokeProjectMandate).toHaveBeenCalledWith("m1"));
    expect(api.allowProbes).not.toHaveBeenCalled();
  });

  it("a settled plan is shown and carried out on the person's click", async () => {
    const plan = {
      summary: "Fenster folgen beim Ziehen",
      steps: [{ path: "hub/src/topcoat_hub/x.py", anchor: "def drag(", do: "follow the pointer" }],
      test: { path: "hub/tests/test_drag.py", do: "a drag moves the window" },
    };
    const api = setup([], plan);
    expect(await screen.findByText("follow the pointer", { exact: false })).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "Plan umsetzen" }));
    await waitFor(() =>
      expect(api.iterateProjectNode).toHaveBeenCalledWith("t1", { feedback: "", keep: false, program: "issue_plan" }),
    );
  });

  it("a draft is applied, a passed fix rolled out, each on its own click", async () => {
    const api = setup([fix("draft"), fix("passed", { id: "f-2", tests: { state: "passed", output: "12 passed" } })]);
    fireEvent.click(await screen.findByRole("button", { name: "Übernehmen und testen" }));
    await waitFor(() => expect(api.applyFix).toHaveBeenCalledWith("f-draft"));
    expect(api.rolloutFix).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole("button", { name: "Auf den Hub einspielen" }));
    await waitFor(() => expect(api.rolloutFix).toHaveBeenCalledWith("f-2"));
    expect(screen.getByText("Tests: passed")).toBeTruthy();
  });

  it("a test run opens with its counts and first failure, the whole log one click away", async () => {
    const output = [
      "E       assert 3 == 2",
      "FAILED hub/tests/test_x.py::test_x - assert 3 == 2",
      "===== 1 failed, 1204 passed, 2 skipped in 61.20s =====",
    ].join("\n");
    setup([fix("failed", { id: "f-1", tests: { state: "failed", output } })]);
    const run = await screen.findByTestId("test-run");
    expect(run.textContent).toContain("Tests: failed · 1.204 bestanden, 1 fehlgeschlagen, 2 übersprungen");
    expect(run.textContent).toContain("Erster Fehler: hub/tests/test_x.py::test_x – assert 3 == 2");
    expect(run.textContent).not.toContain("Kein fehlgeschlagener Test");
    const log = run.querySelector("details") as HTMLDetailsElement;
    expect(log.open).toBe(false);
    expect(log.querySelector("summary")?.textContent).toBe("Vollständige Testausgabe");
    expect(log.querySelector("pre")?.textContent).toBe(output);
  });

  it("a red run without a failing test in its log says the failure is elsewhere", async () => {
    // the hub's tests passed, the web part failed at lint: no test line says what broke
    const output = [
      "===== 3545 passed in 121.30s =====",
      "=== web: lint, typecheck, tests",
      "Cannot find module 'mermaid'",
    ].join("\n");
    setup([fix("failed", { id: "f-1", tests: { state: "failed", output } })]);
    const run = await screen.findByTestId("test-run");
    expect(run.textContent).toContain("3.545 bestanden, 0 fehlgeschlagen");
    expect(run.textContent).toContain("Kein fehlgeschlagener Test im Log");
  });

  it("fixes that failed the same way in a row are one row that says how often", async () => {
    const same = { failure_print: "AssertionError | test_x.py::test_x | assert # == #" };
    const key = { failure_print: "KeyError | test_x.py::test_x | 'family'" };
    const api = setup([
      fix("failed", { id: "f-1", summary: "erster", ...same }),
      fix("failed", { id: "f-2", summary: "zweiter", ...same }),
      fix("failed", { id: "f-3", summary: "dritter", ...key }),
      fix("failed", { id: "f-4", summary: "vierter", ...key }),
      fix("failed", { id: "f-5", summary: "fünfter", ...key, error: "Tests rot. Der Rest des Logs.\nTraceback" }),
    ]);
    expect(await screen.findByText("3× derselbe Fehler")).toBeTruthy();
    expect(screen.getByText("2× derselbe Fehler")).toBeTruthy();
    expect(screen.getAllByTestId("fix-failed")).toHaveLength(2);
    // the row shows the newest attempt, its fingerprint and the reason in one sentence
    const row = screen.getAllByTestId("fix-failed")[1]!;
    expect(row.textContent).toContain("fünfter");
    expect(within(row).getByTestId("fix-why").textContent).toBe("KeyError | test_x.py::test_x | 'family' Tests rot.");
    // the older attempts are folded in the row and can still be discarded one by one
    const earlier = within(row).getByTestId("fix-earlier");
    expect(earlier.textContent).toContain("Frühere Versuche mit demselben Ende: 2");
    fireEvent.click(within(earlier).getAllByRole("button", { name: "Verwerfen" })[0]!);
    await waitFor(() => expect(api.discardFix).toHaveBeenCalledWith("f-3"));
  });

  it("a shortened fingerprint is whole in the fold, not only in a hover title", async () => {
    const long = `AssertionError | tests/${"deep/".repeat(12)}test_x.py::test_x | assert # == #`;
    setup([fix("failed", { id: "f-1", failure_print: long }), fix("failed", { id: "f-2", failure_print: "KeyError | x" })]);
    const rows = await screen.findAllByTestId("fix-failed");
    expect(within(rows[0]!).getByTestId("fix-why").textContent).not.toContain(long);
    expect(within(rows[0]!).getByTestId("fix-print").textContent).toBe(long);
    // a fingerprint that fits the row is not repeated in the fold
    expect(within(rows[1]!).queryByTestId("fix-print")).toBeNull();
  });

  it("after a fix runs: 'does not work' starts the next fix with what still fails", async () => {
    const api = setup([fix("live", { rollout: { message: "the hub runs x", build: "abc+fix-2.1" } })]);
    // a live fix's reason in view is the roll-out's message; the build is in the fold
    expect((await screen.findByTestId("fix-why")).textContent).toBe("the hub runs x");
    expect(screen.getByText(/abc\+fix-2\.1/)).toBeTruthy();
    fireEvent.change(screen.getByRole("textbox", { name: "Was geht noch nicht?" }), { target: { value: "springt noch" } });
    fireEvent.click(screen.getByRole("button", { name: "Geht nicht: nächster Fix" }));
    await waitFor(() => expect(api.confirmIssue).toHaveBeenCalledWith("t1", { works: false, feedback: "springt noch" }));
  });
});
