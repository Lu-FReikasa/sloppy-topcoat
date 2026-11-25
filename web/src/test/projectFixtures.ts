// Test data for the Projects page: a project, nodes, and a fake of the API it uses whose answers are
// set per test. Not shipped (tests import it).
import { vi } from "vitest";
import type {
  Project,
  ProjectChain,
  ProjectMandate,
  ProjectChangeSet,
  ProjectDetail,
  ProjectDiffRun,
  ProjectEvent,
  ProjectVersion,
  ProjectFound,
  ProjectLabel,
  ProjectNode,
  ProjectNodeCreate,
  ProjectNotificationSettings,
  ProjectNodePatch,
  ProjectRelation,
  ProjectSavedView,
  ProjectTemplate,
} from "../project/types";

export const CHAIN: ProjectChain = {
  id: "c1",
  project_id: "p1",
  title: "Paging prototype",
  status: "active",
  rounds: 4,
  flagged_at: "2026-10-29T10:00:00+00:00",
  flag_reason: "many_rounds",
  stopped_at: null,
  created_at: null,
  updated_at: null,
};

export const MANDATE: ProjectMandate = {
  id: "m1",
  project_id: "p1",
  chain_id: "c1",
  target: "dev",
  actions: ["create_prototype", "fix"],
  repo: "app",
  branch: "topcoat/paging",
  push: true,
  text: "Go ahead",
  expires_at: "2026-11-12T10:00:00+00:00",
  created_at: null,
  revoked_at: null,
  uses: 2,
  last_used_at: null,
  state: "active",
  not_covered: [],
};

export function project(overrides: Partial<Project> = {}): Project {
  return {
    id: "p1",
    title: "Topcoat",
    description: "Development of Topcoat",
    status: "active",
    scope: {},
    revision: 1,
    created_at: null,
    updated_at: null,
    ...overrides,
  };
}

export function node(
  overrides: Partial<ProjectNode> & { id: string; title: string },
): ProjectNode {
  return {
    project_id: "p1",
    ref: `Topcoat ${overrides.number ?? "1"}`,
    number: "1",
    parent_id: null,
    kind: "task",
    report_kind: null,
    body: "",
    status: "todo",
    depends_on: [],
    answer: null,
    answered_at: null,
    revision: 1,
    updated_at: null,
    ...overrides,
  };
}

/** A fake of the API the page uses: its answers are set per test. */
export function fakeApi(detail: ProjectDetail) {
  return {
    projectProjects: vi.fn(async () => ({ projects: [detail.project] })),
    projectProject: vi.fn(async () => detail),
    createProject: vi.fn(async () => detail.project),
    updateProject: vi.fn(async () => detail.project),
    setProjectScope: vi.fn(async () => detail.project),
    createProjectNode: vi.fn(
      async (_project: string, _body: ProjectNodeCreate) =>
        node({ id: "n-new", title: "Second", number: "2" }),
    ),
    updateProjectNode: vi.fn(async (_id: string, _body: ProjectNodePatch) =>
      node({ id: "n1", title: "First" }),
    ),
    moveProjectNode: vi.fn(async () => node({ id: "n1", title: "First" })),
    answerProjectQuestion: vi.fn(async () => node({ id: "q1", title: "Q" })),
    decideProjectRequest: vi.fn(async () => node({ id: "r1", title: "R" })),
    decideProjectProposal: vi.fn(async () => node({ id: "r1", title: "R" })),
    releaseProjectOrder: vi.fn(async () => ({
      order: node({ id: "r1", title: "R" }),
      mandate: MANDATE,
      queue_note: "",
    })),
    projectChains: vi.fn(async () => ({ chains: [] as ProjectChain[] })),
    stopProjectChain: vi.fn(async () => ({
      chain: CHAIN,
      stopped: [] as string[],
    })),
    projectMandates: vi.fn(async () => ({ mandates: [] as ProjectMandate[] })),
    revokeProjectMandate: vi.fn(async () => ({ mandate: MANDATE })),
    startProjectRun: vi.fn(async () => ({ id: "run-1", title: "Plan" })),
    // PPH-04 04e
    projectViews: vi.fn(async () => ({ views: [] as ProjectSavedView[] })),
    projectLabels: vi.fn(async () => ({ labels: [] as ProjectLabel[] })),
    createProjectLabel: vi.fn(async (b: { title: string }) => ({
      id: `l-${b.title}`,
      title: b.title,
      color: null,
      description: "",
    })),
    setProjectNodeLabels: vi.fn(async () => ({ labels: [] as ProjectLabel[] })),
    projectRelations: vi.fn(async () => ({
      relations: [] as ProjectRelation[],
    })),
    projectReminders: vi.fn(async () => ({ reminders: [] })),
    searchProjectNodes: vi.fn(async (_o: object) => ({
      nodes: [] as ProjectFound[],
      total: 0,
      next_after: null as number | null,
    })),
    completeProjectNode: vi.fn(async () => ({
      ...node({ id: "n2", title: "Check", number: "1.1" }),
      reopened: false,
    })),
    createProjectView: vi.fn(async () => ({}) as ProjectSavedView),
    deleteProjectView: vi.fn(async () => undefined),
    projectFilterHelp: vi.fn(async () => ({
      help: "Filter syntax: field operator value",
    })),
    // PPH-04 04h and 04j
    me: vi.fn(async () => ({
      id: "u1",
      name: "Lu",
      role: "admin" as const,
      timezone: null as string | null,
      effective_timezone: "Europe/Berlin",
      hub_timezone: "Europe/Berlin",
    })),
    projectNotificationSettings: vi.fn(
      async () =>
        ({
          quiet_start: null,
          quiet_end: null,
          digest_hour: null,
        }) as ProjectNotificationSettings,
    ),
    putProjectNotificationSettings: vi.fn(
      async (b: ProjectNotificationSettings) => b,
    ),
    putMyTimezone: vi.fn(async (tz: string | null) => ({
      id: "u1",
      name: "Lu",
      role: "admin" as const,
      timezone: tz,
      effective_timezone: tz ?? "Europe/Berlin",
      hub_timezone: "Europe/Berlin",
    })),
    downloadProjectExport: vi.fn(
      async (_id: string, format: "json" | "markdown") => ({
        blob: new Blob(["{}"]),
        filename:
          format === "json" ? "project-topcoat.json" : "project-topcoat.zip",
      }),
    ),
    projectTemplates: vi.fn(async () => ({
      templates: [] as ProjectTemplate[],
    })),
    saveProjectTemplate: vi.fn(async () => ({}) as ProjectTemplate),
    deleteProjectTemplate: vi.fn(async () => undefined),
    createProjectFromTemplate: vi.fn(async () =>
      project({ id: "p2", title: "From template" }),
    ),
    // PPH-04 04f
    projectTimeline: vi.fn(async (_id: string, _o?: object) => ({
      events: [] as ProjectEvent[],
      next_after: null as number | null,
    })),
    verifyProjectTimeline: vi.fn(async () => ({
      ok: true,
      problems: [] as string[],
    })),
    projectNodeHistory: vi.fn(async () => ({
      versions: [] as ProjectVersion[],
    })),
    projectNodeDiff: vi.fn(async (_id: string, from: number, to: number) => ({
      from,
      to,
      title: ["a", "a"] as [string, string],
      body: [] as ProjectDiffRun[],
    })),
    revertProjectNode: vi.fn(
      async (_id: string, _b: { revision: number; to_rev: number }) =>
        node({ id: "n1", title: "First" }),
    ),
    projectTrash: vi.fn(async () => ({
      projects: [] as Project[],
      nodes: [] as (ProjectNode & { project_title: string })[],
    })),
    trashProject: vi.fn(async () => project()),
    restoreProject: vi.fn(async () => project()),
    purgeProject: vi.fn(async () => undefined),
    trashProjectNode: vi.fn(async () => node({ id: "n1", title: "First" })),
    restoreProjectNode: vi.fn(async () => node({ id: "n1", title: "First" })),
    purgeProjectNode: vi.fn(async () => undefined),
    projectChangeSets: vi.fn(async () => ({
      change_sets: [] as ProjectChangeSet[],
    })),
    applyProjectChangeSet: vi.fn(
      async (_id: string, _s?: number[]) => ({}) as ProjectChangeSet,
    ),
    refuseProjectChangeSet: vi.fn(async () => ({}) as ProjectChangeSet),
  };
}

export const TREE: ProjectNode[] = [
  node({ id: "n1", title: "Repo", number: "1", kind: "topic", status: null }),
  node({ id: "n2", title: "Check", number: "1.1", parent_id: "n1" }),
  node({
    id: "q1",
    title: "Which node?",
    number: "2",
    kind: "report",
    report_kind: "question",
    status: null,
  }),
];
