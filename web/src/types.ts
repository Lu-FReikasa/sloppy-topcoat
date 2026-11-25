// Types mirroring the hub REST API contract (see brief E1-01-foundation).

/**
 * "yielded": models were unloaded because other programs needed the memory (E1-06).
 * "paused": the node froze its models for the person using the machine (E2-05 B).
 */
export type NodeState =
  | "available"
  | "busy_gaming"
  | "degraded"
  | "yielded"
  | "paused"
  | "offline";

/** llama.cpp backend of a node-managed build. */
export type Backend = "cuda" | "vulkan" | "cpu";

export interface Gpu {
  index: number;
  name: string;
  vendor: "nvidia" | "amd" | "intel" | "other";
  vram_total_mb: number;
  vram_used_mb: number | null;
  /** iGPU / APU sharing system memory. */
  integrated?: boolean;
  /** System memory the GPU may use, reported separately from dedicated VRAM. */
  shared_total_mb?: number;
  shared_used_mb?: number;
  /** Utilisation of the whole GPU in percent, when the node can measure it. */
  gpu_util?: number;
  /** VRAM used by other programs, when the node can measure it. */
  foreign_vram_used_mb?: number;
  /** GPU utilization of other programs in percent, when known. */
  foreign_gpu_util?: number;
  /** Largest shared (system) memory use of one other program: it did not fit into VRAM. */
  foreign_shared_max_mb?: number;
}

export interface CpuInfo {
  model: string;
  /** Physical cores. */
  cores: number;
  /** Logical processors. */
  threads?: number;
  performance_cores?: number;
  efficiency_cores?: number;
}

export interface ModelInfo {
  name: string;
  size_bytes?: number;
  format?: string;
  loaded: boolean;
  vram_used_mb: number | null;
  /** false = the file lives in a configured model_dirs entry (cannot be moved); absent = unknown. */
  own?: boolean;
  // Present while loaded (llamaserver runner).
  ram_used_mb?: number;
  backend?: Backend;
  port?: number;
  context_length?: number;
  n_parallel?: number;
}

export interface LoadedModel {
  name: string;
  vram_used_mb?: number;
  context_length?: number;
  n_parallel?: number;
  pid?: number;
  port?: number;
  backend?: Backend;
  ram_used_mb?: number;
}

export interface RunnerInfo {
  type: "llamaserver" | "fake";
  available: boolean;
  detail?: string;
  backend?: Backend;
  llamacpp_release?: string;
  threads?: number;
}

export interface NetworkInterface {
  name: string;
  mac: string;
  kind: "ethernet" | "wifi" | "other";
}

/** The signed update channel as the hub sees one node (E2-06, D50). */
export interface NodeUpdate {
  /** The channel the node follows (config.update.channel, default "stable"). */
  channel: string;
  /** This build of the node carries an update key. */
  capable: boolean;
  /** The privileged updater service: "ok", "stale" (heartbeat too old) or "missing". */
  updater: "ok" | "stale" | "missing" | null;
  /** The version the running updater reports (node 0.2.6 and later). */
  updater_version?: string | null;
  /** The updater runs an older release than the node: it may not apply a new build. */
  updater_behind?: boolean;
  key_ids: string[];
  /** Newest release the hub holds for the node's channel and platform. */
  available: string | null;
  /** That release is newer than the installed version. */
  newer: boolean;
  /** config.allowed_actions contains node.update. */
  action_allowed: boolean;
}

export interface NodeSummary {
  id: string;
  name: string;
  os: string;
  arch: string;
  state: NodeState;
  connected: boolean;
  last_seen: string | null;
  agent_version: string | null;
  cpu: CpuInfo | null;
  ram_total_mb: number | null;
  ram_used_mb: number | null;
  /** CPU utilisation in percent (null / missing: the node cannot measure it). */
  cpu_util?: number | null;
  /** The drive that holds the model directory (null = not reported). */
  disk_total_mb?: number | null;
  disk_free_mb?: number | null;
  /** Physical network interfaces, wired first (Wake-on-LAN, E2-05 E); kept while the node is off. */
  network?: NetworkInterface[];
  /** Unicast IP addresses of the node (E2-07). */
  addresses?: string[];
  /** config.allowed_actions, when the hub exposes it in the summary. */
  allowed_actions?: string[];
  /** config.roles: "gpu" = compute node (holds models), "tools" = tool node, "dev" = dev workshop node. */
  roles?: string[];
  /** What a tool node offers (config.capabilities), e.g. "web.search". */
  capabilities?: string[];
  gpus: Gpu[];
  runner: RunnerInfo | null;
  models: ModelInfo[];
  loaded: LoadedModel[];
  /** Missing from hubs without the update channel. */
  update?: NodeUpdate;
}

export type ModelTransferStatus =
  | "pending"
  | "preparing"
  | "ready"
  | "transferring"
  | "done"
  | "failed"
  | "canceled";

/** A model copy or move between two nodes (brief E2-07-model-distribution). */
export interface ModelTransfer {
  id: string;
  user_id: string;
  source_node_id: string;
  target_node_id: string;
  model: string;
  target_name: string | null;
  size: number | null;
  sha256: string | null;
  move: boolean;
  status: ModelTransferStatus;
  error_code: string | null;
  error: string | null;
  created_at: string;
  updated_at: string;
}

export interface ModelTransferCreate {
  source_node_id: string;
  target_node_id: string;
  model: string;
  target_name?: string;
  move?: boolean;
}

export interface NodeEvent {
  id: number;
  kind: string;
  data: Record<string, unknown>;
  ts: string;
  received_at: string;
}

/** One llama-bench measurement (pp512 / tg128) stored by the hub. */
export interface BenchmarkRow {
  benchmark_id: string;
  model: string;
  backend: string;
  threads: number;
  n_gpu_layers: number | null;
  pp_tps: number;
  tg_tps: number;
  ts: string;
}

export interface NodeDetail extends NodeSummary {
  events: NodeEvent[];
  config: Record<string, unknown> | null;
  config_hash?: string;
  /** Latest measurements, newest first. */
  benchmarks: BenchmarkRow[];
}

export interface NodeConfigResponse {
  config: Record<string, unknown>;
  config_hash: string;
}

/** A `config.residents` entry: a model the node keeps loaded whenever possible (E1-06). */
export interface Resident {
  model: string;
  priority?: number;
  context_length?: number;
  auto_reload?: boolean;
}

/** Body of PUT /api/nodes/{id}/residents/{model}; the hub fills in defaults. */
export type ResidentUpdate = Omit<Resident, "model">;

export interface ResidentResponse extends NodeConfigResponse {
  changed: boolean;
}

export interface Me {
  id: string;
  name: string;
  role: "admin" | "user";
  /** GUI language chosen by the user; null = follow the browser. */
  language?: "en" | "de" | null;
  /** The IANA time zone the person chose; null = follow the hub's. */
  timezone?: string | null;
  /** The zone that applies to the person (their own, else the hub's). */
  effective_timezone?: string;
  hub_timezone?: string;
}

/** A gateway API key (the secret itself is only in the create response). */
export interface ApiKey {
  id: string;
  name: string;
  prefix: string;
  created_at: string | null;
  last_used_at: string | null;
  key?: string;
}

/** A chat conversation (E2-01). `model` / `node` null = automatic. */
/** How much effort an answer about the user's systems gets (E3-04). */
export type Quality = "fast" | "balanced" | "thorough";

/** Which servers and tool groups the assistant may use in a conversation, and whether it may use
 *  the internet (E3-04); null for servers or groups means all of them, for `internet` the user's
 *  default. It only narrows what the user's grants allow. The internet is its own switch: web
 *  search does not depend on the servers chosen. */
export interface ToolScope {
  nodes: string[] | null;
  groups: string[] | null;
  internet?: boolean | null;
  /** false = incognito: no notes are used or kept in this conversation (absent = the user's setting). */
  memory?: boolean | null;
}

/** What the user could choose from: the groups and servers of the capabilities they may run. */
export interface ChatToolsOverview {
  /** `via`: where the group's tools run: "node", "hub" (no server needed) or "hub+node". */
  groups: { key: string; label: string; via?: string }[];
  /** `capabilities`: the tool names the assistant can call on this server, per group. */
  nodes: {
    name: string;
    groups: string[];
    capabilities?: Record<string, string[]>;
    /** online | offline | …: shown so a server without tools (a model node) is still visible. */
    state?: string | null;
  }[];
  /** The node web search comes from (the hub's pick); null = no node is released for it. */
  internet: { node: string | null };
}

/** The big model's second look at a checked answer (thorough level). */
export interface Verification {
  ok: boolean;
  issues: string[];
  model: string;
  /** No other model was available: the answering model checked its own answer. */
  same_model?: boolean;
}

export interface Conversation {
  id: string;
  title: string;
  model: string | null;
  node: string | null;
  /** null = the user's default level. */
  quality?: Quality | null;
  /** null = everything the user may use. */
  tool_scope?: ToolScope | null;
  /** E3-06: the persona this conversation speaks with (null = none). */
  persona_ref?: string | null;
  /** The workspace (application) the conversation was started in; null = before applications. */
  application?: string | null;
  /** Start gathers the open ones (2026-11-13): pinned, or active within a day and not done. */
  open?: boolean;
  pinned?: boolean;
  done_at?: string | null;
  /** The chat this one continues (a section of the continuous conversation); null = a first chat. */
  continues_id?: string | null;
  /** E3-07: an answer is being generated in this conversation, whether or not a tab follows it. */
  running?: boolean;
  created_at: string;
  updated_at: string;
  message_count?: number;
}

export interface ChatMessage {
  /** Server id; negative numbers are messages that only exist in this browser so far. */
  id: number;
  role: "user" | "assistant";
  content: string;
  model: string | null;
  node: string | null;
  fallback_from: string | null;
  status:
    | "complete"
    | "stopped"
    | "error"
    | "truncated"
    | "running"
    | "interrupted";
  error: string | null;
  prompt_tokens: number | null;
  completion_tokens: number | null;
  first_token_ms: number | null;
  duration_ms: number | null;
  context_trimmed: number;
  /** Memory notes the model saw for this answer (E2-02). */
  memory_refs?: MemoryRef[];
  /** The server check (agent task) this answer is based on, if the chat ran one (E3-01d). */
  agent_task_id?: string | null;
  /** The thorough level's second look at this answer, if it was made (E3-04). */
  verification?: Verification | null;
  /** E3-06: the SHA-256 of the system prompt this answer was sent with, and the persona
   *  ("<id>@<version>") it came from; the user's verdict on the answer. */
  prompt_hash?: string | null;
  persona_ref?: string | null;
  feedback?: "helped" | "unfit" | null;
  /** E3-07 D3: a chat bubble (default), the line that shows which tool runs, or the report card
   *  the hub built from a server check (`report` holds its structure). */
  kind?: "say" | "progress" | "report" | "handoff" | "reaction";
  report?: ReportCard | null;
  /** E7-01: what the hub did to produce this answer, in order (thoughts, calls, checks …). */
  activity?: ActivityItem[];
  created_at: string | null;
}

/** One thing the hub did for an answer (`activity.py`): plain data, the browser words it. */
export type ActivityKind =
  | "thinking"
  | "lead_in"
  | "plan"
  | "tool"
  | "check"
  | "retry"
  | "steer"
  | "note"
  | "announce"
  | "step_check";
export type ActivityStatus = "running" | "ok" | "failed" | "info" | "stopped";
export interface ActivityItem {
  id: number;
  kind: ActivityKind;
  status: ActivityStatus;
  /** The technical name, such as `docker.ps @ node-a`; empty when the kind says it all. */
  title: string;
  /** Text a model or a tool produced (a thought, a reason, an error code). */
  detail: string | null;
  data: Record<string, unknown>;
  at: string;
  /** How long it took, once it is over. */
  ms: number | null;
}

export interface ReportFailure {
  capability: string | null;
  node: string | null;
  target: string;
  code: string;
  message: string;
}

/** The report card of a server check (E3-07 D3): built by the hub from the check's diagnosis and
 *  evidence, never written by the model. */
/** A chat in Research offered a research of its own (api/chat.py, research_mode): the question,
 *  and the run once the person started it. */
export interface ResearchOfferInfo {
  question: string;
  started?: string;
}

export interface ReportCard {
  /** On a plain answer, not a check's card: the offer to start a research (2026-11-13). */
  research_offer?: ResearchOfferInfo;
  version: number;
  task_id: string | null;
  status: string;
  incomplete: boolean;
  incomplete_reason: string | null;
  summary: string;
  findings: ReportFinding[];
  assumptions: ReportAssumption[];
  proposal: ReportProposal | null;
  evidence: ReportEvidence[];
  /** E3-07 D4: the machines the check asked, and those it could have asked and did not. */
  coverage?: { asked: string[]; not_asked: string[] };
  notes: string[];
  /** Calls that gave no result (a link that did not load, a refused tool). */
  failures?: ReportFailure[];
  /** The result form "table" of a check order: one row per checked item, as the check wrote it. */
  table?: Record<string, unknown>[];
  /** The summary leans on assumptions (listed on the card). */
  summary_has_assumptions?: boolean;
  /** The Projekte chat prepared changes in this check: the task they belong to (PreparedChanges). */
  changes_for?: string;
  /** The check made proposals (file changes of the workshop, D99): the task they belong to. */
  proposals_for?: string;
  /** The Projekte chat prepared the next iteration of a task (project_iterations.py). */
  iterate?: IterateOfferData;
  /** An issue from the running system: filed (with undo) or offered (project_issues.py). */
  issue?: IssueCardData;
  calls: number;
}

export interface IssueCardData {
  filed: boolean;
  task_id?: string;
  project_id?: string;
  number?: string;
  title?: string;
  similar?: { id: string; number: string; title: string }[];
  text?: string;
  application?: string | null;
  conversation?: string;
}

export interface IterateOfferData {
  task_id: string;
  task_number: string;
  task_title: string;
  iteration: number;
  based_on: string | null;
  based_on_number: string | null;
  feedback: string;
  requirements: string[];
}

export interface ReportFinding {
  claim: string;
  tag: "live" | "doc" | "web";
  /** The evidence marks (E1, E2 …) that show it; each is a link to the excerpt on the card. */
  evidence: string[];
  /** The notes (evidence refs) this finding contradicts. */
  contradicts?: string[];
  /** Words of the cited result that show it; the hub found them there. */
  quote?: string;
  /** The finding gave no quote (it keeps its tag). */
  unquoted?: boolean;
}

export interface ReportAssumption {
  claim: string;
  confirm_with: string;
  evidence: string[];
  downgraded?: boolean;
  /** The words the check said its result shows, and which the result does not contain. */
  misquoted?: string;
}

export interface ReportProposal {
  text: string;
  /** What the step would change / how to undo it; empty when the check did not say. */
  impact: string;
  rollback: string;
  advice_only: boolean;
}

export interface ReportEvidence {
  ref: string;
  tag: string;
  node: string | null;
  capability: string | null;
  input?: Record<string, unknown> | null;
  url?: string;
  excerpt: string;
  truncated: boolean;
}

/** Whether the chat checks the user's servers before it answers (E3-01d). */
export type ToolsMode = "auto" | "on" | "off";

/** A note as the model saw it (snapshot at answer time). */
export interface MemoryRef {
  id: string;
  text: string;
  evidence: MemoryEvidence;
  status: MemoryStatus;
  /** How far the note could be trusted when the model saw it (E3-05). */
  stage?: MemoryStage;
  kind?: MemoryKind;
}

export type MemoryStatus =
  | "active"
  | "superseded"
  | "disputed"
  | "deleted"
  | "archived";
export type MemoryEvidence = "live" | "doc" | "assumption";
/** What a note is about: the person, their preferences, their infrastructure (E3-05). */
export type MemoryKind = "person" | "preference" | "operational";
/** How far a note can be trusted: the only form of reliability the model sees (E3-05). */
export type MemoryStage = "sure" | "older" | "stale";

export interface MemoryItem {
  id: string;
  /** The program whose memory the note is in; null = the core (about the person, read by every
   *  program; D74). */
  application?: string | null;
  /** The names of its tags in the person's tag tree. */
  tags?: string[];
  /** Held in the core by the person: it stands in every answer, whatever the ranking says. */
  core?: boolean;
  text: string;
  status: MemoryStatus;
  evidence: MemoryEvidence;
  confidence: number;
  source_type: string;
  source_ref: {
    conversation_id?: string;
    message_id?: number;
    correction?: boolean;
  };
  version: number;
  /** How often the user said it again (no new version) and when the last time was. */
  confirmed_count: number;
  last_confirmed_at: string | null;
  disputed_with: string | null;
  superseded_by: string | null;
  embedder: string | null;
  created_at: string | null;
  updated_at: string | null;
  // --- memory care (E3-05); absent from an older hub ---
  kind?: MemoryKind;
  stage?: MemoryStage;
  /** 0..1, how far the note can be trusted now (the model never sees it). */
  reliability?: number;
  /** 0..1, how often the note helped (smoothed; 0.5 without data). */
  usefulness?: number;
  /** A live check showed it holds (counts twice as much as a statement). */
  last_verified_at?: string | null;
  verified_count?: number;
  /** Only for a volatile note: when it is dropped. */
  expires_at?: string | null;
  verify_with?: { capability: string; input: Record<string, unknown> } | null;
  recalled_count?: number;
  used_count?: number;
  last_used_at?: string | null;
  feedback_positive?: number;
  feedback_negative?: number;
  halflife_days?: number;
}

export interface MemoryVersion {
  version: number;
  text: string;
  status: MemoryStatus;
  evidence: MemoryEvidence;
  confidence: number;
  reason: string;
  actor: "chat" | "consolidation" | "user" | "care";
  /** What the change is based on: the conversation, the cue, the answered question, the care
   *  run's check … */
  source?: {
    conversation_id?: string;
    question_id?: string;
    evidence?: string;
    cue?: string;
    care?: { run: string; task: string; evidence: string[] };
  };
  created_at: string | null;
}

/** One note a clarification question is about, with what is known of it. */
export interface QuestionCandidate {
  id: string;
  text: string;
  at: string | null;
  confirmed_count: number;
  last_confirmed_at: string | null;
}

/** A question the memory could not settle itself (D19 extended). Per user, never shared. */
export interface MemoryQuestion {
  id: string;
  /** residence, birthday, age, name, work, pet, family, or "" */
  topic: string;
  origin: "chat" | "consolidation" | "care" | "crosscheck";
  status: "open" | "answered" | "dismissed";
  candidates: QuestionCandidate[];
  /** D74: where a question of the cross-check belongs (a program, or "person" for Settings). */
  program?: string | null;
  /** The concrete point the notes disagree on, worded as a question (when it could be named). */
  question?: string | null;
  /** What a care check saw instead (origin "care"). */
  observed?: string | null;
  resolution: string | null;
  created_at: string | null;
  answered_at: string | null;
}

/** How a question is answered: one of its notes, none of them, or what is true. */
export type QuestionAnswer =
  | { note_id: string }
  | { none: true }
  | { text: string };

export interface MemoryRun {
  id: string;
  kind?: "consolidation" | "care";
  trigger: "scheduled" | "manual" | "nightly" | "idle" | "answer";
  status: "running" | "ok" | "stopped_budget" | "no_model" | "error" | "paused";
  stats: Record<string, unknown>;
  started_at: string | null;
  finished_at: string | null;
}

/** One line of a care run's report: what was done to a note, why, and what undoes it. */
export interface CareChange {
  action:
    | "verified"
    | "changed"
    | "asked"
    | "unverifiable"
    | "expired"
    | "archived";
  item_id: string;
  text: string;
  kind: MemoryKind;
  /** The version the change made (none for what changed no content). */
  version: number | null;
  reason: string;
  /** `restore`: bring the struck / archived note back; `revert`: back to `from_version`. */
  undo: "restore" | "revert" | null;
  from_version?: number;
  new_text?: string;
  evidence?: string[];
  question_id?: string;
  observed?: string;
}

/** The numbers the care works with (environment settings, read-only on the page). */
export interface CareSettings {
  hour: number;
  interval_seconds: number;
  halflife_days: Record<MemoryKind | "volatile", number>;
  volatile_ttl_hours: number;
  check_threshold: number;
  archive: { usefulness: number; reliability: number; idle_days: number };
  max_notes?: number;
}

export interface MemoryCareOverview {
  running: boolean;
  runs: MemoryRun[];
  settings: CareSettings;
}

export interface MemoryOverview {
  items: MemoryItem[];
  /** Live notes per application; `shared` = about the person, in every application. */
  applications?: Record<string, number>;
  counts: Record<MemoryStatus, number>;
  blocked: number;
  enabled: boolean;
  /** `transitional`: the model-free hashing embedder (search by shared words). */
  embedder: { id: string; transitional: boolean };
  running: boolean;
  runs: MemoryRun[];
  questions: MemoryQuestion[];
  /** Notes per kind among the live ones (E3-05). */
  kinds?: Record<MemoryKind, number>;
  care?: MemoryCareOverview;
  /** Chats being summarised now / waiting for their rest or a retry (this user's). */
  digest?: DigestStatus;
  /** Hand-over notes written after chat answers. */
  reviews?: { runs: MemoryRun[] };
}

export interface ConversationDetail extends Conversation {
  messages: ChatMessage[];
  /** E3-06: the persona (name, version) and the prompt of the first answer, and where the prompt
   *  changed later (a persona switch, a changed core entry). */
  persona?: {
    id: string;
    name: string;
    key: string | null;
    version: number;
    status: string;
  } | null;
  prompt_hash?: string | null;
  prompt_changes?: PromptChange[];
}

export interface PromptChange {
  message_id: number;
  prompt_hash: string;
  persona_ref: string | null;
  first: boolean;
  at: string | null;
}

/** The system prompt as it was sent (read-only, the user's own). */
export interface PromptSnapshot {
  hash: string;
  text: string;
  persona_ref: string | null;
  persona: { id: string; name: string | null } | null;
  core_version: string | null;
  created_at: string | null;
}

/** A model the picker offers, with the nodes that have it. */
export interface ChatModel {
  id: string;
  topcoat: { nodes: { node: string; loaded: boolean; state: string }[] };
}

export interface ChatSettings {
  user: {
    system_prompt: string | null;
    temperature: number | null;
    /** Form of address in German (and languages with a formal register). */
    address: "du" | "sie";
    memory_enabled: boolean;
    /** The default quality level of the user's conversations (E3-04). */
    quality: Quality;
    /** The default of the per-conversation "Internet" switch (E3-04). */
    internet: boolean;
    /** E3-06: the persona new conversations start with (null = none). */
    persona_id?: string | null;
  };
  hub: {
    /** Model families whose quality level also sets how long the model thinks (settings of the hub). */
    effort_families?: string[];
  };
  effective_model: string;
}

export interface ActionError {
  code: string;
  message: string;
}

export interface ActionResult {
  ok: boolean;
  result?: unknown;
  error?: ActionError;
}

export interface PairingCode {
  code: string;
  expires_at: string;
  /** URL nodes should use (reverse proxy); null: the address the GUI was opened with. */
  hub_url?: string | null;
  /** Hub CA to pin ("sha256:<hex>"); null: nodes use their system trust store. */
  ca_fingerprint?: string | null;
}

// --- tool nodes (E2-04) ---------------------------------------------------------------

/** A capability a node build offers (inventory.capabilities). */
export interface ToolCapability {
  name: string;
  description: string;
  /** True = changes nothing on the machine; only those run without a confirmation (D15). */
  read_only: boolean;
  /** JSON Schema of the input. */
  input_schema: {
    required?: string[];
    properties?: Record<string, { type?: string }>;
  };
}

export interface NodeCapabilities {
  node: { id: string; name: string; roles: string[] };
  offered: ToolCapability[];
  /** Switched on in the node's config. */
  enabled: string[];
  /** What the signed-in user may run right now. */
  usable: string[];
  grants: string[];
}

export interface ToolResult {
  ok: boolean;
  call_id?: number | null;
  capability?: string;
  output?: unknown;
  bytes?: number;
  duration_ms?: number;
  error?: { code: string; message: string };
}

export interface ToolCallRow {
  id: number;
  user_id: string;
  user: string | null;
  node: string;
  capability: string;
  input: Record<string, unknown>;
  via: string;
  ok: boolean;
  code: string | null;
  error: string | null;
  output_bytes: number;
  output_excerpt: string | null;
  duration_ms: number | null;
  created_at: string | null;
}

export interface GrantRow {
  user_id: string;
  user: string;
  capabilities: string[];
}

export interface UserRow {
  id: string;
  name: string;
  role: string;
}

// --- the agent (E3-01a) -----------------------------------------------------------------

export type AgentStatus = "queued" | "running" | "done" | "failed" | "canceled";

/** A claim of the diagnosis: `live` = seen on a machine (cites evidence), `doc` = from the
 * knowledge base, `assumption` = not seen, says how to check. */
export interface Finding {
  claim: string;
  tag: "live" | "doc" | "web" | "assumption";
  evidence: string[];
  confirm_with: string;
  downgraded?: boolean;
  /** The machine a coordinator's merged finding comes from (E3-08). */
  node?: string;
  /** References of notes (`doc` evidence) that this live finding says are out of date. */
  contradicts?: string[];
}

export interface Diagnosis {
  summary: string;
  findings: Finding[];
  next: string;
  notes: string[];
}

/** `GET /api/agent/settings`: the tool mode per model (native | json | auto). */
export interface AgentSettings {
  tool_modes: Record<string, string>;
  default_tool_mode: string;
  modes: string[];
}

export interface AgentTask {
  id: string;
  title: string;
  prompt: string;
  model: string | null;
  via: string;
  /** `coordinator`: splits the question into one read-only check per machine (E3-08). */
  kind?: "single" | "coordinator";
  /** The chat this task was handed over from, and until when it waits for a fast node. */
  conversation_id?: string | null;
  deadline_at?: string | null;
  status: AgentStatus;
  stop_reason: string | null;
  error: string | null;
  summary: string | null;
  diagnosis: Diagnosis | null;
  step_count: number;
  tool_calls: number;
  runs: number;
  created_at: string | null;
  started_at: string | null;
  finished_at: string | null;
  updated_at: string | null;
}

export interface AgentStep {
  seq: number;
  kind: "assistant" | "observation" | "note";
  data: {
    content?: string;
    tokens?: number;
    model?: string;
    kind?: "tool" | "error" | "note";
    text?: string;
    evidence?: string | null;
    tool?: {
      node: string;
      capability: string;
      input: Record<string, unknown>;
      ok: boolean;
      code?: string;
    } | null;
  };
  created_at: string | null;
}

export interface AgentEvidence {
  ref: string;
  tag: "live" | "doc" | "web";
  source: {
    node?: string;
    capability?: string;
    input?: Record<string, unknown>;
    at?: string;
    /** The page a web.fetch result came from (after redirects). */
    url?: string;
  };
  excerpt: string;
  bytes: number;
  created_at: string | null;
}

export interface AgentTaskDetail extends AgentTask {
  steps: AgentStep[];
  evidence: AgentEvidence[];
  /** What the agent proposes to change (E3-01b); each runs only after its own click. */
  proposals?: AgentProposal[];
  /** The checks a coordinator started, one per machine. */
  children?: AgentChild[];
}

export interface AgentChild {
  id: string;
  node: string | null;
  question: string;
  status: AgentStatus;
  stop_reason: string | null;
  summary: string | null;
  step_count: number;
  tool_calls: number;
}

export type ProposalStatus =
  | "pending"
  | "running"
  | "executed"
  | "failed"
  | "refused"
  | "expired";

/** A change the agent proposes (E3-01b): exactly what would run, and why. */
export interface AgentProposal {
  id: string;
  /** null for a proposal the person made themselves in the inventory */
  task_id: string | null;
  origin?: "agent" | "manual";
  node: string;
  capability: string;
  input: Record<string, unknown>;
  reason: string;
  effect: string;
  who_notices: string;
  way_back: string;
  evidence: string[];
  /** shown with the card; a push: the diff that will be committed */
  preview?: string | null;
  hash: string;
  status: ProposalStatus;
  expires_at: string | null;
  decided_at: string | null;
  result: {
    ok: boolean;
    output?: unknown;
    error?: { code?: string; message?: string };
  } | null;
  created_at: string | null;
}

/** A folder of notes the knowledge base indexes through a tool node (E2-03). */
export interface KnowledgeSource {
  id: string;
  name: string;
  visibility: "shared" | "private";
  /** `node`: a folder read through a tool node; `upload`: files the admin put in by hand. */
  kind: "node" | "upload";
  /** True for the admin who created it; others see neither the path nor the error. */
  own: boolean;
  node: string;
  root: string | null;
  status: "new" | "indexing" | "ready" | "error";
  error: string | null;
  files: number;
  chunks: number;
  /** Chunks with a vector of the embedder in use; `null` for sources of someone else. */
  embedded: number | null;
  /** Entries the exclude list kept out (counted, never named). */
  excluded: number;
  /** Chunks the secret filter held back. */
  blocked: number;
  indexed_at: string | null;
  created_at: string | null;
  blocked_files?: { path: string; rules: string[] }[];
}

export interface KnowledgeHit {
  source: string;
  path: string;
  heading: string;
  text: string;
  mtime: number;
  score: number;
}

export interface KnowledgeStatus {
  embedder: string;
  /** True while the word-hashing stand-in is used instead of an embedding model. */
  transitional: boolean;
  exclude: string[];
}

// --- the core, personas, the first-start questionnaire (E3-06) ---------------------------------------

export type InstructionKind = "style" | "rule";

/** A note of the core: an instruction, a pinned fact, a household note. */
export interface CoreItem extends Omit<MemoryItem, "kind"> {
  kind?: MemoryKind | InstructionKind;
  core?: boolean;
  scope?: "user" | "household" | "system";
  example?: string | null;
  /** A style of adjectives only ("freundlich, knapp"): kept as a weak hint. */
  weak?: boolean;
  /** It stands in the core as the assistant gets it now (not cut for room). */
  in_core?: boolean;
}

export interface CoreBudgetPart {
  budget: number;
  used: number;
}

export type CorePartName =
  | "system"
  | "persona"
  | "rules"
  | "profile"
  | "household"
  | "recent";

export interface CoreRecentChat {
  id: string;
  title: string;
  day: string;
  statements: string[];
}

export interface CoreProposal {
  id: string;
  action: "add" | "change" | "remove" | "pin" | "unpin" | "kb_edit";
  kind: MemoryKind | InstructionKind;
  text: string;
  example: string | null;
  core: boolean;
  weak: boolean;
  reason: string;
  /** Where it came from: the quote, the check, the Markdown to copy for a file edit … */
  evidence: {
    quote?: string;
    markdown?: string;
    path?: string;
    review?: boolean;
  } & Record<string, unknown>;
  diff: { before: Record<string, unknown>; after: Record<string, unknown> };
  target_id: string | null;
  origin: "chat" | "care";
  status: "proposed" | "accepted" | "rejected";
  created_at: string | null;
  decided_at: string | null;
}

export interface CoreSystemRules {
  id: string;
  lang: "en" | "de";
  text: string;
  version: number;
}

export interface CoreOverview {
  budget: {
    total: number;
    used: number;
    context: number;
    parts: Record<CorePartName, CoreBudgetPart>;
    cut: string[];
    examples_dropped: boolean;
  };
  rules: CoreItem[];
  profile: CoreItem[];
  household: CoreItem[];
  system: CoreSystemRules[];
  /** The latest chats of the program as a new chat reads them: key terms, oldest first. */
  recent: CoreRecentChat[];
  proposals: CoreProposal[];
  /** The core exactly as the assistant gets it in a new conversation. */
  preview: string;
  admin: boolean;
}

export interface PersonaComponents {
  role?: { de?: string; en?: string };
  address?: "du" | "sie";
  humor?: 0 | 1 | 2;
  length?: "short" | "long";
  transparency?: boolean;
  ask?: "ask_first" | "read_first";
  emojis?: boolean;
  evidence?: boolean;
  /** A persona that is just a text (the old system prompt). */
  free_text?: string;
}

export interface Persona {
  id: string;
  key: string | null;
  name: string;
  version: number;
  scope: "household" | "user";
  status: "active" | "archived";
  components: PersonaComponents;
  example: { de?: string; en?: string };
  defaults: Record<string, unknown>;
  /** The text as it stands in the core (in the user's language), within 400 tokens. */
  text: string;
  tokens: number;
  /** Something was left out to stay within the tokens. */
  truncated: boolean;
  ref: string;
}

export type WizardState = "later" | "skipped" | "done" | null;

export interface PersonaLibrary {
  templates: Persona[];
  own: Persona[];
  default_id: string | null;
  max_tokens: number;
  wizard: { state: WizardState; show: boolean; banner: boolean };
  blocks: {
    humor: number[];
    length: string[];
    ask: string[];
    address: string[];
  };
}

export interface PersonaStat {
  persona_id: string;
  name: string | null;
  key: string | null;
  answers: number;
  helped: number;
  unfit: number;
}

type Localised = { de: string; en: string };

export interface Questionnaire {
  version: number;
  roles: string[];
  none_role: string;
  scenes: {
    id: string;
    question: Localised;
    answers: { id: string; role: string; text: Localised }[];
  }[];
  questions: {
    id: string;
    default: string;
    text: Localised;
    options: { value: string; label: Localised }[];
  }[];
}

export interface QuestionnaireAnswers {
  scenes: Record<string, string>;
  [question: string]: string | Record<string, string>;
}

export interface QuestionnairePreview {
  role: string;
  components: PersonaComponents;
  text: string;
  tokens: number;
  samples: string[];
}

// --- inventory (E3-10) ------------------------------------------------------------------------

export interface InventorySection {
  state: "ok" | "n/a" | "failed";
  error: string | null;
  read_at: string | null;
  age_seconds: number | null;
  truncated: boolean;
  current: boolean;
}

export interface InventoryItem {
  kind: "container" | "service" | "compose";
  name: string;
  state: string;
  since?: string;
  gone?: boolean;
  gone_at?: string | null;
  detail?: Record<string, unknown>;
  services?: number;
  running?: number;
  containers?: string[];
}

export interface InventoryNode {
  node: { id: string; name: string };
  online: boolean;
  status: "ok" | "partial" | "error" | "unavailable" | "never";
  error: string | null;
  attempted_at: string | null;
  collected_at: string | null;
  age_seconds: number | null;
  stale: boolean;
  current: boolean;
  sections: Record<string, InventorySection>;
  host: { os?: string; kernel?: string; hostname?: string } | null;
  containers: InventoryItem[];
  services: InventoryItem[];
  compose: InventoryItem[];
  /** what the signed-in user may do next to this node: read logs, ask for a change */
  can?: { read: string[]; change: string[] };
}

export interface InventoryList {
  nodes: InventoryNode[];
  stale_after_seconds: number;
}

// --- identity (E3-12) -------------------------------------------------------------------------

export interface IdentityVia {
  group: string;
  member_of: string;
  kind: "role" | "configured";
}

export interface IdentityPersonApp {
  app: string;
  name: string;
  roles: string[];
  via: IdentityVia[];
}

export interface IdentityPerson {
  id: string;
  username: string;
  name: string;
  enabled: boolean;
  source: "directory" | "local" | "unknown";
  groups: string[];
  apps: IdentityPersonApp[];
}

export interface IdentityGroup {
  id: string;
  path: string;
  name: string;
  members: number;
  members_ok: boolean;
  realm_roles: string[];
  opens: {
    app: string;
    roles: string[];
    via_group: string;
    kind: "role" | "configured";
  }[];
}

export interface IdentityApp {
  client_id: string;
  name: string;
  enabled: boolean;
  builtin: boolean;
  url: string;
  roles: string[];
  known_app: boolean;
  people: number;
  people_enabled: number;
  groups: string[];
  configured_groups: string[];
}

/** One source node's look; on a failed read only `node` and `error` are set. */
export interface IdentitySource {
  node: { id: string; name: string };
  error?: { code: string; message: string };
  realm?: string;
  node_taken_at?: string | null;
  people?: IdentityPerson[];
  groups?: IdentityGroup[];
  apps?: IdentityApp[];
  notes?: string[];
  read_at?: number;
  age_seconds?: number;
  cached?: boolean;
}

export interface IdentityView {
  sources: IdentitySource[];
  app_groups: Record<string, string[]>;
}

/** A report or document the agent made, kept apart from chats. */
export interface Artifact {
  id: string;
  kind: string;
  title: string;
  /** The application of the task that made it (null: not known). */
  application?: string | null;
  /** Made by the hub's own work (a project run, a block program), not asked for by the person. */
  auto?: boolean;
  task_id: string | null;
  conversation_id: string | null;
  unread: boolean;
  created_at: string | null;
  updated_at: string | null;
  /** Only on the single-artifact call. */
  body?: string;
}

/** A thorough research as a document of the Ablage (D102). */
export type ReportStatus = "draft" | "reviewed" | "released";

export interface Report {
  id: string;
  application: string;
  /** Made by the hub's own work (a project run, a block program), not asked for by the person. */
  auto?: boolean;
  title: string;
  question: string;
  language: "de" | "en";
  depth: string;
  status: ReportStatus;
  current_version: number;
  released_version: number | null;
  task_id: string | null;
  conversation_id: string | null;
  unread: boolean;
  created_at: string | null;
  updated_at: string | null;
  released_at: string | null;
}

/** What the code wrote about the run, kept with each version. */
export interface ReportMeta {
  complete?: boolean;
  end_reason?: string | null;
  last_repeated?: string;
  seconds?: number | null;
  author_model?: string | null;
  author_node?: string | null;
  steps?: number;
  tool_calls?: number;
  searches?: number;
  pages?: number;
  sources?: number;
  hosts?: string[];
  claims_total?: number;
  claims_backed?: number;
  limits?: string[];
  models?: { model: string; node: string; work: string; calls: number; ms: number; tokens: number | null }[];
  build?: string;
}

export interface ReportVersionInfo {
  version: number;
  parent_version: number | null;
  kind: "agent" | "revision" | "person";
  note: string;
  content_hash: string;
  meta: ReportMeta;
  created_at: string | null;
  body?: string;
}

export interface ReportComment {
  id: string;
  version: number;
  anchor: string;
  text: string;
  author: "person" | "agent";
  resolved: boolean;
  created_at: string | null;
}

export interface ReportDetail extends Report {
  shown: ReportVersionInfo & { body: string };
  comments: ReportComment[];
}

export interface ReportDiff {
  from: number;
  to: number;
  added: number;
  removed: number;
  diff: string;
}

/** A page of Topcoat's own wiki (hub wiki_store.py): a report of the application `wiki` with a
 *  place in the tree. Every take-over from a project is the page from then on. */
export interface WikiPage {
  id: string;
  section: string;
  /** The folder: names with "/" between them (the same as `section`). */
  folder?: string;
  categories?: string[];
  title: string;
  path: string;
  current_version: number;
  source_node_id: string | null;
  created_at: string | null;
  updated_at: string | null;
}

export interface WikiTree {
  sections: { name: string; pages: WikiPage[] }[];
  /** Every category in use. */
  categories?: string[];
}

/** The person's order of a page: only the fields given change. */
export interface WikiPagePatch {
  folder?: string;
  title?: string;
  categories?: string[];
}

export interface WikiPageDetail extends WikiPage {
  shown: ReportVersionInfo & { body: string };
  versions: ReportVersionInfo[];
}

/** What "take into the wiki" would do with a project's document; nothing happens before the click. */
export interface WikiTakeOver {
  /** A project's document (the Ablage's documents answer with `kind` and `id` instead). */
  node_id?: string;
  section: string;
  title: string;
  path: string;
  page_id: string | null;
  new_page: boolean;
  version: number;
  unchanged: boolean;
  chars: number;
}

export interface ArtifactList {
  artifacts: Artifact[];
  unread: number;
}

/** What the second model found in an answer of a recent chat. `error` has evidence (a note of the
 *  memory contradicts the claim), `hint` has none. */
export interface ChatReview {
  id: string;
  conversation_id: string;
  message_id: number;
  verdict: "hint" | "error";
  findings: {
    claim: string;
    reason: string;
    /** What backs the finding: a note, a tool result of the check, or words of the conversation. */
    evidence: {
      note_id?: string;
      result?: string;
      chat?: boolean;
      text: string;
    } | null;
  }[];
  reviewer_model: string | null;
  reviewer_node?: string | null;
  same_model?: boolean;
  author_model: string | null;
  author_node: string | null;
  seen: boolean;
  created_at: string | null;
}

export interface DigestStatus {
  running: number;
  waiting: number;
}

/** One request or model load a node works on for the hub. `kind` names the task. */
export interface NodeWork {
  node: string;
  model: string;
  kind: string;
  seconds: number;
  /** SCH-03: what the caller said about the request; only what it set */
  application?: string;
  conversation_id?: string;
  message_id?: string;
  task_id?: string;
}

/** A request that waits for a place on a node (SCH-03). `place`: 0 chat, 1 after an answer, 2 other. */
export interface WaitingJob {
  place: number;
  seconds: number;
  kind: string;
  model?: string;
  application?: string;
  conversation_id?: string;
  task_id?: string;
}

/** A request in the queue of the pool: it takes the first suitable node that is free. */
export interface PoolWaitingJob extends WaitingJob {
  /** how many requests are served before it */
  ahead: number;
  /** who asked: chat, agent, background or bench */
  source: string;
  /** the nodes that may take it */
  nodes: string[];
}

/** GET /api/activity: what the nodes do now and which model each standing task uses. */
export interface ActivityView {
  nodes: {
    name: string;
    state: NodeState;
    work: NodeWork[];
    /** requests waiting for a free place on the node, by place in its queue */
    queued?: { chat: number; answer: number; idle: number };
    /** the single waiting requests, in the order they will be served (SCH-03) */
    waiting?: WaitingJob[];
  }[];
  roles: { key: string; model: string | null; node: string | null }[];
  background_nodes: string[];
  digest: DigestStatus;
  /** requests waiting for a node of their pool, in the order they will be served */
  pool_waiting?: PoolWaitingJob[];
}

/** One node in the scheduling view (GET /api/scheduling). */
export interface SchedulingNode {
  id: string;
  name: string;
  state: NodeState;
  connected: boolean;
  /** compute = holds and serves models, tools = tool node */
  kind: "compute" | "tools" | "dev";
  loaded: string[];
  /** the models on the node's disk: what a row of the table can name */
  models: string[];
}

export interface SchedulingView {
  nodes: SchedulingNode[];
  paused_until: string | null;
  embedding_model: string;
  embedding_choices: string[];
  customised: boolean;
  /** What the admin did here, newest first (kept in the hub's memory only). */
  history: SchedulingEvent[];
}

/** One row of the table of applications (SCH-04, D85): what an application may use, in order. An
 *  empty field means "whatever is there": all nodes, the model that is fast enough. */
export interface ApplicationRow {
  priority?: "high" | "normal" | "low";
  /** model names, best first; `same_as_answer` only in the rows `check` and `background` */
  models?: string[];
  /** node names in order; `all` (the rest) comes last */
  nodes?: string[];
}

/** What a row would use right now, and why (the reason is the hub's English text). */
export interface RowPreview {
  model: string | null;
  node: string | null;
  reason: string;
}

/** GET /api/scheduling/applications. */
export interface ApplicationsView {
  /** the applications, then the system rows */
  keys: string[];
  system_keys: string[];
  priorities: ("high" | "normal" | "low")[];
  /** what is stored; PUT replaces all of it, so a row left out is cleared */
  rows: Record<string, ApplicationRow>;
  preview: Record<string, RowPreview>;
}

export interface SchedulingEvent {
  ts: string;
  kind: "policy" | "table" | "reset" | "pause" | "resume" | "run";
  actor: string;
  detail: string;
  ok: boolean;
}

export type BackgroundTask = "digest" | "care" | "consolidate";

export interface SchedulingUpdate {
  /** missing = keep; "" = the first served bge-m3 */
  embedding_model?: string;
}

/** One heartbeat sample of a connected node (GET /api/nodes/metrics). */
export interface NodeMetricPoint {
  /** Unix seconds. */
  t: number;
  ram_used_mb: number | null;
  /** All processors together in percent; null where the node cannot measure it. */
  cpu_util?: number | null;
  gpu_util: number | null;
  vram_used_mb: number | null;
}

/** A machine the nodes run on (docs/OVERVIEW.md): a physical one, or a VM on a physical host. */
export interface Machine {
  key: string;
  name: string;
  kind: "physical" | "vm";
  host: string | null;
  /** Set by an admin (false: the machine only exists because node names point at it). */
  configured: boolean;
  nodes: string[];
}

export interface MachinesView {
  machines: Machine[];
  /** For every node the machine it runs on. */
  node_machine: Record<string, string>;
  /** What an admin assigned by hand (the rest follows the node names). */
  assign?: Record<string, string>;
}

export interface NodeMetrics {
  window_seconds: number;
  /** node id -> points, oldest first; a node that is not connected is missing. */
  nodes: Record<string, NodeMetricPoint[]>;
}

/** What the last look at a registered repository saw (E6-01 S0). */
export interface DevRepoState {
  default_branch?: string;
  private?: boolean;
  pushed_at?: string | null;
  /** null = the token may not read pull requests (optional right) */
  open_prs?: number | null;
  branches?: number;
  test_branches?: string[];
  can_push?: boolean;
  /** Set instead of the rest when the look failed. */
  error?: string;
  message?: string;
}

/** A repository the assistant may look at. The token is never part of it. */
export interface DevRepo {
  id: string;
  /** D76: external = it lives elsewhere (GitHub) and is only read; local = it lives in the
   *  workshop of a dev node, where the assistant edits and tests it. */
  kind?: "external" | "local";
  /** The dev node a local repository lives on (its id and name). */
  node_id?: string | null;
  node?: string | null;
  /** A local repository's external one: where it came from and is published to. */
  upstream_id?: string | null;
  /** An external repository's local clones (ids). */
  clones?: string[];
  name: string;
  full_name: string;
  default_branch: string;
  mode: string;
  test_template: string;
  token_set: boolean;
  state: DevRepoState | null;
  checked_at: string | null;
  created_at: string | null;
}

export interface DevRepoList {
  repos: DevRepo[];
  modes: string[];
  templates: string[];
  /** The dev nodes that can hold local repositories. */
  nodes?: { id: string; name: string; online: boolean }[];
}

/** A branch of a local repository, as its dev node sees it (D76). */
export interface DevBranch {
  name: string;
  commit?: string;
  date?: string;
  subject?: string;
  ahead: number;
  behind: number;
  /** a work branch of the assistant (topcoat/*) */
  work: boolean;
  default: boolean;
}

export interface EditDevRepo {
  mode?: string;
  test_template?: string;
  /** a new token; left out, the sealed one stays */
  token?: string;
}

export interface AddDevRepo {
  name: string;
  repo: string;
  token: string;
  mode: string;
  test_template: string;
}

/** One parameter an admin can change while the hub runs (E7-03, `live_settings.py`). */
export interface ParamView {
  key: string;
  group: string;
  kind: "int" | "float" | "bool" | "str" | "enum" | "json";
  /** One English sentence from the hub; the browser has its own words (`params.text.<key>`). */
  text: string;
  unit: string;
  low: number | null;
  high: number | null;
  choices: string[];
  default: unknown;
  value: unknown;
  overridden: boolean;
}

export interface ParamsView {
  groups: string[];
  params: ParamView[];
}

export interface ParamChange {
  id: number;
  old: unknown;
  new: unknown;
  reset: boolean;
  by: string | null;
  at: string | null;
}

export interface ParamPreset {
  version: number;
  values: Record<string, unknown>;
}

/** How much trouble a run takes (docs/PLATFORM.md, 3.6). */
export type Effort = "fast" | "balanced" | "thorough";
export const EFFORTS: readonly Effort[] = ["fast", "balanced", "thorough"];

/** A parameter value per effort level instead of one value. */
export interface EffortTable {
  effort: Partial<Record<Effort, unknown>>;
}

export function isEffortTable(value: unknown): value is EffortTable {
  if (typeof value !== "object" || value === null) return false;
  const keys = Object.keys(value);
  const inner = (value as { effort?: unknown }).effort;
  return (
    keys.length === 1 && keys[0] === "effort" && typeof inner === "object" && inner !== null
  );
}


/** One section of a continuous conversation (the user area): cut by a rest or by "new section". */
export interface ConversationSegment {
  first_message_id: number;
  last_message_id: number;
  started_at: string;
  ended_at: string;
  messages: number;
  current: boolean;
  /** What the hub wrote down when the section closed; null while there is none. */
  summary: string | null;
}

export interface ConversationSegments {
  enabled: boolean;
  idle_seconds: number;
  segments: ConversationSegment[];
}

/** One chat of the continuous conversation, as the user sees it above the open one. */
export interface ThreadSection {
  id: string;
  title: string;
  messages: number;
  started_at: string;
  ended_at: string;
  /** What the hub wrote down when the chat was left; null while there is none (and for the open one). */
  summary: string | null;
  current: boolean;
}

export interface ConversationThread {
  enabled: boolean;
  idle_seconds: number;
  /** Oldest first, the open chat last. */
  sections: ThreadSection[];
  /** Older chats exist beyond the ones listed. */
  more: boolean;
}


// --- the inbox (docs/PLATFORM.md, 3.7) ------------------------------------------------------------

export type InboxView = "inbox" | "board" | "filed";

/** Something that happened and wants to be read. `ref` says which window shows the matter. */
export interface InboxItem {
  id: string;
  application: string;
  kind: string;
  title: string;
  summary: string;
  ref: { module?: string; props?: Record<string, string> };
  urgent: boolean;
  /** Urgent and not yet settled: reading it does not put it away. */
  unresolved: boolean;
  read: boolean;
  pinned: boolean;
  filed: boolean;
  created_at: string | null;
  read_at: string | null;
  resolved_at: string | null;
}

export const MARK_KINDS = ["comment", "postit", "sticker", "highlight"] as const;
export type MarkKind = (typeof MARK_KINDS)[number];

/** A mark on an item (docs/PLATFORM.md, 3.7): data on the item, never a change of its text. */
export interface ItemMark {
  id: string;
  item_id: string;
  /** "person", an application key, or "recheck". */
  author: string;
  kind: MarkKind;
  text: string;
  line: number | null;
  created_at: string | null;
  resolved_at: string | null;
}

export interface InboxCounts {
  unread: number;
  urgent: number;
  /** Unread items by application. */
  applications: Record<string, number>;
}

export interface InboxListing {
  items: InboxItem[];
  counts: InboxCounts;
}

/** A note learned before applications existed that looks as if it belongs somewhere else. */
export interface MemorySortSuggestion {
  id: string;
  text: string;
  kind: string;
  current: string | null;
  suggested: string;
}

/** A note as a report of the cross-check names it. */
export interface MemoryReportNote {
  id: string;
  text: string;
  status: MemoryStatus;
  application: string | null;
}

/** What the cross-check of the memories told a program or the person (D74): a discrepancy of a
 *  topic it is responsible for, or a notice another program sent about a note of its own. */
export interface MemoryReport {
  id: string;
  kind: "discrepancy" | "notice";
  recipient: string;
  sender: string | null;
  topic: string;
  status: "open" | "decided" | "asked" | "seen";
  reason: string;
  message: string;
  decision: {
    outcome?: "decided" | "asked" | "gone";
    changes?: { note: "A" | "B"; id: string; action: "correct" | "retire" }[];
    notified?: string[];
    why?: string;
  };
  created_at: string | null;
  decided_at: string | null;
  a: MemoryReportNote | null;
  b: MemoryReportNote | null;
}

/** One tag of the person's tag tree (D74). `responsible`, `sensitive` and `released` are null
 *  when inherited; `effective` is what holds after inheritance. */
export interface MemoryTag {
  id: string;
  parent_id: string | null;
  name: string;
  description: string;
  responsible: string | null;
  sensitive: boolean | null;
  released: string[] | null;
  effective: { responsible: string; sensitive: boolean; released: string[] };
  created_by: "seed" | "user" | "agent" | "migration";
  /** False for a tag an assistant made and the person has not looked at yet. */
  reviewed: boolean;
  root: boolean;
  /** Live notes that carry it directly. */
  notes: number;
}

export interface MemoryTagTree {
  tags: MemoryTag[];
  programs: string[];
  /** Who may be responsible for a branch: `person` or a program. */
  responsible: string[];
  max_depth: number;
}

// --- the incognito folder (docs/PLATFORM.md, section 3.9) ----------------------------------------

export interface IncognitoMessage {
  role: "user" | "assistant";
  content: string;
}

/** A conversation that lives in the memory of the hub only. */
export interface IncognitoTab {
  id: string;
  title: string;
  /** The web (search and reading a page) may be used in this tab. */
  internet: boolean;
  count: number;
  created_at: number;
  updated_at: number;
  messages?: IncognitoMessage[];
}

// --- hand-over between applications (docs/PLATFORM.md, section 3.8) --------------------------------

export interface HandoverDossier {
  question: string;
  context: { role: "user" | "assistant"; text: string }[];
  asked: string;
  expected: string;
  /** What was kept back from the other side, and why. */
  withheld: string[];
}

export type HandoverStatus =
  | "proposed"
  | "sent"
  | "done"
  | "failed"
  | "cancelled";

export interface Handover {
  id: string;
  from_application: string;
  to_application: string;
  conversation_id: string | null;
  target_conversation_id: string | null;
  task_id: string | null;
  dossier: HandoverDossier;
  /** What the person saw: sending is bound to it. */
  hash: string;
  /** public, sensitive (infrastructure facts) or critical (secrets: needs an approval) */
  class?: "public" | "sensitive" | "critical";
  status: HandoverStatus;
  result_summary: string | null;
  error: string | null;
  /** The hand-over has its Akte (request, report with sources, steps) and where a finished one is
   *  filed in the knowledge base. */
  has_file?: boolean;
  knowledge_path?: string | null;
  created_at: string | null;
  sent_at: string | null;
  finished_at: string | null;
}

/** One model of a node as the pool page shows it (PRG-00 R1). */
export interface PoolModel {
  name: string;
  loaded: boolean;
  /** programs may use it (false = outside the node's pool) */
  available: boolean;
  kinds: string[];
  kinds_source: "detected" | "manual" | "proven" | "gguf";
  /** what the probes proved: true = proven, false = tested and not proven, null = never tested */
  capabilities?: { tools: boolean | null; embedding: boolean | null };
  /** the setting taken from the bench per kind of work, e.g. { tools: "nothink" } */
  profile?: Record<string, string>;
  size_mb: number | null;
  /** tokens per second measured in real answers (else a benchmark); null = not measured */
  tps?: number | null;
  /** what the model is, read from its name and file header: kinds, family, size, moe, quant */
  tags?: string[];
}

export interface PoolNode {
  node: string;
  node_id?: string;
  /** the model the node keeps loaded for all text work (null = none) */
  house?: string | null;
  /** Place of the node's house model in the house order (1 = serves work no row steers). */
  house_rank?: number | null;
  /** the node takes background work first (summaries, notes) with a small model */
  worker?: boolean;
  connected: boolean;
  /** the admin set a pool; otherwise every model is available */
  restricted: boolean;
  /** most models the node holds at once (null = no limit) */
  max_loaded: number | null;
  models: PoolModel[];
}

export interface PoolsView {
  nodes: PoolNode[];
  /** what work gets when no row of the table decides: the best answering model the pools offer */
  default_model: string;
}

// --- the bench (PRG-00 R6, `hub/src/topcoat_hub/bench`) --------------------------------------

export interface BenchSuite {
  key: string;
  title: string;
  function: string;
  version: number;
  cases: number;
}

export interface BenchFailure {
  case: string;
  why: string[];
}

export interface BenchVariant {
  key: string;
  name: string;
  extra: Record<string, unknown>;
}

export interface BenchRating {
  model: string;
  node: string;
  suite: string;
  /** a setting that was tried ("" = the model as it is asked every day; only that one ranks) */
  variant: string;
  function: string;
  version: number;
  /** false = the suite changed since: shown, but it does not rank */
  current: boolean;
  /** share of the cases that passed, 0 to 1 */
  score: number;
  passed: number;
  total: number;
  tps: number | null;
  failed: BenchFailure[];
  at: string | null;
  run_id: string;
}

export interface BenchSuggestion {
  model: string;
  node: string;
  /** mean score over the general suites, 0 to 1 */
  score: number;
  default_model: string;
  /** null = the current default has no complete rating */
  default_score: number | null;
  suites: string[];
}

export interface BenchRun {
  run_id: string;
  model: string;
  node: string;
  suites: string[];
  variant: string;
  status: "running" | "done" | "stopped" | "failed";
  started_at: string;
  finished_at: string | null;
  progress: { suite?: string; case?: number; of?: number };
  results: { suite: string; score: number; passed: number; total: number; tps: number | null }[];
  error: string | null;
}

// --- block programs (PRG-00 R3/R4, `hub/src/topcoat_hub/blocks`) -------------------------------

export interface BlockProgramInfo {
  name: string;
  title: string;
  inputs: Record<string, string>;
  program: Record<string, unknown>;
  /** ships with the hub: read only, copy it to change it */
  builtin: boolean;
}

export interface BlockRunStep {
  event: string; // step | pick | call | activity | branch | loop_end | run
  seq: number;
  at?: number; // the hub's clock, seconds
  path?: string;
  b?: string;
  state?: string;
  out?: string;
  preview?: string;
  model?: string;
  node?: string | null;
  reason?: string;
  rounds?: number;
  what?: string; // activity: tools_start | tool_step | tools_wait
  data?: Record<string, unknown>; // activity: the task's own event (a plan, a call, a note)
}

export interface BlockRunView {
  id: string;
  program: string;
  status: "running" | "ok" | "failed" | "stopped";
  error: string;
  reason: string;
  notes: string[];
  filed: string[];
  variables: Record<string, unknown>;
  authors: Record<string, string>;
  steps?: BlockRunStep[];
  step_count?: number;
}

/** GET /api/scheduling/measurements (SCH-06): everyday use per application, model and node. */
export interface Measurements {
  days: number;
  rows: {
    application: string;
    model: string;
    node: string;
    requests: number;
    failed: number;
    cancelled: number;
    cut_off: number;
    waited_seconds: number;
    generated_tps: number | null;
    prompt_tps: number | null;
    cached_share: number | null;
  }[];
}

/** GET /api/scheduling/model-switches (P2a): loads and model changes per day and machine. */
export interface ModelSwitches {
  days: number;
  /** "alt" (old order, default) or "neu" (the profile of the work picks the first node) */
  choice_mode: "alt" | "neu";
  totals: {
    requests: number;
    loads: number;
    switches: number;
    review_requests: number;
    review_loads: number;
    waited_seconds_on_loads: number;
    loads_by_work: Record<string, number>;
    loads_by_reason: Record<string, number>;
  };
  review_load_share: number | null;
  cells: {
    date: string;
    node: string;
    requests: number;
    loads: number;
    switches: number;
    review_loads: number;
    loads_by_work: Record<string, number>;
  }[];
}

/** GET /api/scheduling/quality (QLT-01): how the second look judged each model in each application. */
export interface QualityCounters {
  days: number;
  /** fewer looks than this say too little to mark a model */
  min_looks: number;
  /** application -> lowest share of green looks, 0 to 1 */
  floors: Record<string, number>;
  rows: {
    application: string;
    model: string;
    looks: number;
    ok: number;
    hint: number;
    error: number;
    ok_share: number | null;
    floor: number | null;
    below_floor: boolean;
  }[];
}

/** GET /api/blocks/catalogue: the lists the typed block editor offers. */
export interface BlockCatalogue {
  types: string[];
  blocks: Record<string, { required: string[]; optional: string[] }>;
  compare_modes: string[];
  ranks: string[];
  suites: string[];
  hooks: string[];
  hook_booleans: string[];
  hook_numbers: Record<string, [number, number]>;
  applications: string[];
  depths: string[];
}

/** GET /api/scheduling/chat-policy: how the chat answers by effort (the stored document and what it
 *  comes to at each level; a missing key is the hub's own rule). */
export interface ChatPolicyView {
  policy: Record<string, unknown> & { node?: unknown };
  levels: Record<string, Record<string, unknown>>;
  numbers: Record<string, [number, number]>;
}
