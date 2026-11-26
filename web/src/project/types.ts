// The Projekte application's data as the hub sends it (brief PPH-01-planning-app, 01b;
// rework 2026-10-27). Every write carries the revision it was made from; a stale one is refused
// with 409. A node's ref (the project's title and its number, such as "Topcoat 1.3.2") is how it
// is named in the chat.

import type { MessageState } from "./messages";

export type { MessageState };

export type ProjectStatus = "active" | "paused" | "done" | "dropped";
export type ProjectNodeKind = "topic" | "task" | "report";
export type ProjectTaskStatus =
  | "todo"
  | "in_progress"
  | "in_review"
  | "blocked"
  | "done"
  | "dropped";
export type ProjectReportKind =
  | "note"
  | "question"
  | "decision"
  | "document"
  | "fact"
  | "request"
  | "message";
export type ProjectGrant = "free" | "confirm";

// target application -> action -> grant. An absent action is not granted.
export type ProjectScope = Record<string, Record<string, ProjectGrant>>;

/** The grants a person can set, per target application and action. */
export const PROJECT_GRANTS: Record<string, readonly string[]> = {
  research: ["request_report"],
  dev: ["request_report", "create_branch", "create_prototype"],
  admin: ["read_released"],
  // the project itself: create and change the sub-tasks of a large task (D109); never delete
  project: ["decompose"],
};

/** A change the Projekte chat prepared (it waits for the person's click). */
export interface ProjectChange {
  id: string;
  task_id: string | null;
  capability: string;
  input: Record<string, unknown>;
  // parent: the entry a new one goes under, as the hub resolved it (number and title)
  preview: {
    target?: string;
    fields: Record<string, unknown>;
    parent?: { ref: string; title: string };
  };
  status: "pending" | "running" | "applied" | "refused" | "failed";
  result: Record<string, unknown> | null;
  created_at: string | null;
  decided_at: string | null;
}

export interface Project {
  id: string;
  title: string;
  description: string;
  status: ProjectStatus;
  scope: ProjectScope;
  // PPH-04 04b (the page does not show them yet)
  parent_id?: string | null;
  favorite?: boolean;
  color?: string | null;
  identifier?: string | null;
  trashed_at?: string | null;
  revision: number;
  created_at: string | null;
  updated_at: string | null;
}

/** One approval of several task nodes, run one after another (project_runs.py). */
export interface ProjectBatch {
  id: string;
  project_id: string;
  node_ids: string[];
  max_runs: number;
  runs: number;
  status: "active" | "done" | "stopped";
  stop_reason: string | null;
  created_at: string | null;
  stopped_at: string | null;
}

/** A batch with the state of each of its tasks, in the order the approval named them. */
export interface ProjectBatchState extends ProjectBatch {
  tasks: {
    node_id: string;
    number: string;
    title: string;
    status: "todo" | "in_progress" | "in_review" | "blocked" | "done" | "dropped";
    /** why a blocked task stopped: the reason of its last status change */
    reason: string | null;
    /** the tasks it waits for that are not finished yet */
    waits_for: { number: string; title: string; status: string }[];
    /** the class of the task result: public, sensitive or critical (report_class.py) */
    class?: "public" | "sensitive" | "critical" | null;
    application: string | null;
    mode: string;
  }[];
}

export interface ProjectNode {
  id: string;
  project_id: string;
  ref: string;
  number: string;
  parent_id: string | null;
  kind: ProjectNodeKind;
  report_kind: ProjectReportKind | null;
  title: string;
  body: string;
  status: ProjectTaskStatus | null;
  /** a task's application (research, dev, project, admin) and its mode (lesen, schreiben) */
  application?: string | null;
  mode?: string | null;
  depends_on: string[];
  // PPH-04 04b: dates carry an offset (ISO 8601); the page does not show them yet
  start_at?: string | null;
  due_at?: string | null;
  end_at?: string | null;
  priority?: number;
  percent_done?: number;
  progress?: number;
  done_at?: string | null;
  color?: string | null;
  estimate_minutes?: number | null;
  repeat_rule?: Record<string, unknown> | null;
  is_milestone?: boolean;
  favorite?: boolean;
  trashed_at?: string | null;
  as_of?: string | null;
  origin_app?: string | null;
  current?: boolean;
  supersedes_id?: string | null;
  answer: string | null;
  answered_at: string | null;
  message?: ProjectMessageInfo;
  revision: number;
  updated_at: string | null;
  /** Labels, when the answer carries them (the project and the search do). */
  labels?: ProjectLabel[];
  /** A result of a task: its iteration, the result it continued, the feedback it answers. */
  iteration?: number;
  based_on_id?: string | null;
  feedback?: string | null;
  /** A task: what every further iteration must hold; an iteration is being run. */
  requirements?: ProjectRequirement[];
  iterating?: boolean;
}

export type IssueFixState =
  | "draft"
  | "applying"
  | "testing"
  | "passed"
  | "failed"
  | "queued"
  | "live"
  | "rolled_back"
  | "refused"
  | "discarded";

export interface IssueFix {
  id: string;
  issue_id: string;
  summary: string;
  state: IssueFixState;
  edits: { path: string; old: string; new: string }[];
  files: { path: string; content: string }[];
  branch: string | null;
  base_commit: string | null;
  commit: string | null;
  diff: string;
  tests: { state?: string; output?: string; job_id?: string; notes?: string[] } | null;
  rollout: { state?: string; build?: string; message?: string; label?: string; output?: string } | null;
  error: string | null;
  /** of a failed fix (hub failure_print.py): equal prints in a row are the same failure */
  failure_print?: string | null;
  created_at: string | null;
  updated_at: string | null;
}

/** The settled plan of an issue's investigation (hub issue_investigation.py), carried out in steps. */
export interface IssuePlan {
  summary: string;
  /** a place is a symbol (Class.method, function, …) with where, or an anchor line (older plans) */
  steps: { path: string; symbol?: string; where?: string; anchor?: string; do: string }[];
  test: { path: string; do: string };
}

/** A task filed from the running system (hub project_issues.py). */
export function isIssue(node: Pick<ProjectNode, "kind" | "body">): boolean {
  return node.kind === "task" && /^Art: issue$/m.test(node.body ?? "");
}

export interface ProjectRequirement {
  id: string;
  text: string;
  /** the iteration whose feedback it came from */
  from?: number;
}

export interface ProjectIteration {
  id: string;
  number: string;
  iteration: number | null;
  based_on: string | null;
  feedback: string | null;
  theme: boolean;
  created_at: string | null;
}

export interface ProjectIterations {
  task: string;
  title?: string;
  number?: string;
  revision?: number;
  requirements: ProjectRequirement[];
  iterating: boolean;
  iterations: ProjectIteration[];
}

export interface ProjectDetail {
  project: Project;
  nodes: ProjectNode[];
}

export interface ProjectCreate {
  title: string;
  description: string;
}

export interface ProjectPatch {
  revision: number;
  title?: string;
  description?: string;
  status?: ProjectStatus;
}

export interface ProjectNodeCreate extends ProjectNodeFields {
  kind: ProjectNodeKind;
  title: string;
  body?: string;
  parent_id?: string | null;
  report_kind?: ProjectReportKind;
}

export interface ProjectNodePatch extends ProjectNodeFields {
  revision: number;
  title?: string;
  body?: string;
  status?: ProjectTaskStatus;
}

export interface ProjectMove {
  revision: number;
  parent_id: string | null;
}

// --- PPH-04 (04b to 04e): labels, relations, reminders, saved views, search --------------------

export interface ProjectLabel {
  id: string;
  title: string;
  color: string | null;
  description: string;
}

export type ProjectRelationKind =
  | "blocked_by"
  | "related"
  | "duplicate_of"
  | "precedes"
  | "copied_from"
  | "supersedes";

export const RELATION_KINDS: readonly ProjectRelationKind[] = [
  "blocked_by",
  "related",
  "duplicate_of",
  "precedes",
  "copied_from",
  "supersedes",
];

export interface ProjectRelation {
  id: string;
  kind: ProjectRelationKind;
  /** `out`: this node is the subject (it is blocked by the other); `in`: the other one is. */
  direction: "out" | "in";
  other: { id: string; ref: string; title: string; project_id: string };
  valid_from: string | null;
  valid_to: string | null;
}

export interface ProjectReminder {
  id: string;
  node_id: string;
  at: string | null;
  relative_to: "due" | "start" | "end" | null;
  offset_seconds: number | null;
  fire_at: string | null;
  fired_at: string | null;
}

export type ProjectViewType = "list" | "table" | "board" | "calendar" | "gantt";

/** A saved view: a filter, a sort rule and a layout under a title. */
export interface ProjectSavedView {
  id: string;
  project_id: string | null;
  title: string;
  type: ProjectViewType;
  filter: string;
  sort: string;
  config: Record<string, unknown>;
  favorite: boolean;
  position: number;
  builtin: boolean;
  revision: number;
}

/** A node as the search answers: the node, its project's title and its labels. */
export interface ProjectFound extends ProjectNode {
  project_title: string;
  labels?: ProjectLabel[];
}

export interface ProjectSearch {
  nodes: ProjectFound[];
  total: number;
  next_after: number | null;
}

export interface ProjectNodeFull extends ProjectNode {
  labels?: ProjectLabel[];
}

/** The fields of a node a person can change besides title, body and status. A date, a colour and
 *  a repeat rule are cleared by sending null. */
export interface ProjectNodeFields {
  start_at?: string | null;
  due_at?: string | null;
  end_at?: string | null;
  priority?: number;
  percent_done?: number;
  color?: string | null;
  estimate_minutes?: number | null;
  repeat_rule?: ProjectRepeatRule | null;
  is_milestone?: boolean;
  favorite?: boolean;
}

export interface ProjectRepeatRule {
  every: number;
  unit: "minute" | "hour" | "day" | "week" | "month" | "year";
  mode?: "after" | "monthly" | "from_done";
  tz?: string;
  anchor_day?: number;
}

export interface ProjectCompleted extends ProjectNode {
  /** A repeating task comes back as `todo` with its dates moved. */
  reopened: boolean;
}

// --- PPH-04 04f: the Akte, versions, trash, change sets -----------------------------------------

/** A line of a project's Akte (the timeline). `label` names the kind in both languages. */
export interface ProjectEvent {
  id: string;
  seq: number;
  at: string;
  kind: string;
  label: { en?: string; de?: string };
  project_id: string;
  project_title: string;
  node_id: string | null;
  node_ref: string | null;
  node_title: string | null;
  actor: {
    kind: "person" | "agent" | "system" | string;
    app: string | null;
    agent_task_id: string | null;
    chain_id: string | null;
    change_set_id: string | null;
  };
  group_id: string | null;
  payload: Record<string, unknown> | null;
  reason: string | null;
}

export interface ProjectTimeline {
  events: ProjectEvent[];
  next_after: number | null;
}

/** A version of a node's title and body; `rev` is the node's revision after the change. */
export interface ProjectVersion {
  rev: number;
  kind: string;
  event_id: string | null;
  at: string | null;
}

export interface ProjectDiffRun {
  tag: "equal" | "replace" | "insert" | "delete";
  old: string[];
  new: string[];
}

export interface ProjectDiff {
  from: number;
  to: number;
  title: [string, string];
  body: ProjectDiffRun[];
}

export interface ProjectTrash {
  projects: Project[];
  nodes: (ProjectNode & { project_title: string })[];
}

export type ProjectPurgeMode = "keep_timeline" | "remove_everything";

export interface ProjectChangeSet {
  id: string;
  project_id: string;
  task_id: string | null;
  title: string;
  summary: string;
  operations: Record<string, unknown>[];
  /** What each operation would write: the lines of the Akte, before to after. */
  preview: {
    index: number;
    op: string;
    target: string | null;
    /** The node's reference ("Topcoat 1.5"); sets older than 2026-11-14 have none. */
    ref?: string | null;
    /** On an existing node: field (title, body, status) -> [before, after]; empty = no change. */
    change?: Record<string, [unknown, unknown]>;
    lines: { kind: string; node: string | null; payload: Record<string, unknown> | null }[];
  }[];
  /** Operation index -> the indexes that need what it creates. */
  dependants: Record<string, number[]>;
  status: "pending" | "running" | "applied" | "refused" | "failed";
  applied: number[];
  result: Record<string, unknown> | null;
  created_at: string | null;
  decided_at: string | null;
}

// --- PPH-04 04h and 04j: reminders' quiet hours, templates --------------------------------------

/** When reminders and the daily list may reach the inbox. Times are "HH:MM" in the person's zone. */
export interface ProjectNotificationSettings {
  quiet_start: string | null;
  quiet_end: string | null;
  /** The hour (0 to 23) of the daily list of overdue tasks; null = no list. */
  digest_hour: number | null;
}

export interface ProjectTemplate {
  id: string;
  name: string;
  description: string;
  nodes: number;
  has_dates: boolean;
  created_at: string | null;
}

/** What a message adds to its node (PPH-05); present on a node that is a message. */
export interface ProjectMessageInfo {
  type: string;
  from: string;
  to: string;
  chain_id: string | null;
  in_reply_to: string | null;
  round: number;
  state: MessageState;
  task_id: string | null;
  payload: Record<string, unknown> | null;
  schema_version: number | null;
  trust: "own" | "system" | "foreign" | null;
  stats: Record<string, unknown> | null;
  verification: { ok?: boolean; issues?: string[] } | null;
}

export interface ProjectChain {
  id: string;
  project_id: string;
  title: string;
  status: "active" | "done" | "stopped";
  rounds: number;
  flagged_at: string | null;
  flag_reason: string | null;
  stopped_at: string | null;
  created_at: string | null;
  updated_at: string | null;
}

export interface ProjectMandate {
  id: string;
  project_id: string;
  chain_id: string | null;
  target: string;
  actions: string[];
  repo: string;
  branch: string;
  push: boolean;
  text: string;
  expires_at: string;
  created_at: string | null;
  revoked_at: string | null;
  uses: number;
  last_used_at: string | null;
  state: "active" | "expired" | "revoked";
  not_covered: string[];
}

export interface OrderRelease {
  days: number;
  text: string;
  push: boolean;
}
