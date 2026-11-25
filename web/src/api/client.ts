import type {
  Instrument,
  SheetComparison,
  SheetDetail,
  SheetPatch,
  SheetResearchJob,
  SheetSummary,
  SheetViewParams,
} from "../sheets/types";
import type { ThemeManifest } from "../desktop/theme";
import type {
  BuildStart,
  ReleaseBuild,
  ReleasePreview,
  ReleasesOverview,
} from "../releases/types";
import type {
  UiAgentDefaults,
  UiBootstrap,
  UiPrefs,
  UiProfileView,
  UiTemplateView,
  UiUserView,
} from "../layout/types";
import { SseParser } from "./sse";
import type {
  ProjectChange,
  ProjectChangeSet,
  ProjectCompleted,
  ProjectDetail,
  ProjectDiff,
  ProjectLabel,
  ProjectNotificationSettings,
  ProjectPurgeMode,
  ProjectRelation,
  ProjectRelationKind,
  ProjectReminder,
  ProjectSavedView,
  ProjectSearch,
  ProjectTemplate,
  ProjectTimeline,
  ProjectTrash,
  ProjectVersion,
  ProjectViewType,
  ProjectNode,
  ProjectNodeCreate,
  ProjectNodePatch,
  ProjectMove,
  Project,
  ProjectCreate,
  OrderRelease,
  ProjectChain,
  ProjectMandate,
  ProjectPatch,
  ProjectScope,
} from "../project/types";
import type {
  MachinesView,
  ActivityItem,
  ActivityView,
  InboxCounts,
  InboxItem,
  InboxListing,
  Handover,
  ItemMark,
  MarkKind,
  IncognitoMessage,
  IncognitoTab,
  InboxView,
  MemorySortSuggestion,
  MemoryTagTree,
  MemoryReport,
  ParamChange,
  ParamPreset,
  ParamsView,
  BackgroundTask,
  SchedulingUpdate,
  SchedulingView,
  ApplicationRow,
  ApplicationsView,
  BlockCatalogue,
  ChatPolicyView,
  BlockProgramInfo,
  BlockRunView,
  BenchRating,
  BenchRun,
  BenchSuggestion,
  BenchVariant,
  BenchSuite,
  Measurements,
  ModelSwitches,
  PoolsView,
  QualityCounters,
  Report,
  ReportComment,
  ReportDetail,
  ReportDiff,
  ReportStatus,
  ReportVersionInfo,
  NodeMetrics,
  Artifact,
  ArtifactList,
  ChatReview,
  IdentityView,
  InventoryList,
  InventoryNode,
  ToolsMode,
  Quality,
  ToolScope,
  ChatToolsOverview,
  ConversationSegments,
  ConversationThread,
  Verification,
  ActionResult,
  AgentSettings,
  AgentTask,
  AgentProposal,
  AgentTaskDetail,
  AddDevRepo,
  EditDevRepo,
  ApiKey,
  DevRepo,
  DevBranch,
  DevRepoList,
  ChatMessage,
  ChatModel,
  MemoryRef,
  ChatSettings,
  CoreItem,
  CoreOverview,
  CoreProposal,
  Persona,
  PersonaComponents,
  PersonaLibrary,
  PersonaStat,
  PromptSnapshot,
  Questionnaire,
  QuestionnaireAnswers,
  QuestionnairePreview,
  WikiPage,
  WikiPageDetail,
  WikiPagePatch,
  WikiTakeOver,
  WikiTree,
  WizardState,
  KnowledgeHit,
  KnowledgeSource,
  KnowledgeStatus,
  Conversation,
  ConversationDetail,
  GrantRow,
  MemoryItem,
  MemoryOverview,
  MemoryQuestion,
  MemoryVersion,
  QuestionAnswer,
  Me,
  NodeCapabilities,
  NodeConfigResponse,
  NodeDetail,
  ModelTransfer,
  ModelTransferCreate,
  NodeSummary,
  PairingCode,
  ResidentResponse,
  ResidentUpdate,
  ToolCallRow,
  ToolResult,
  UserRow,
} from "../types";

/** Error raised for any non-2xx hub response or network failure. */
export class ApiError extends Error {
  readonly status: number;
  readonly code: string;
  /** Seconds from a Retry-After hint (503 model_unavailable), if the hub sent one. */
  readonly retryAfter?: number;
  /** The hub's `detail` object when it sent one (a 409 on a layout carries the current document). */
  readonly detail?: Record<string, unknown>;

  constructor(
    status: number,
    code: string,
    message: string,
    retryAfter?: number,
    detail?: Record<string, unknown>,
  ) {
    super(message);
    this.name = "ApiError";
    this.status = status;
    this.code = code;
    this.retryAfter = retryAfter;
    this.detail = detail;
  }
}

/** Raised on HTTP 401; the client also invokes its onUnauthorized callback. */
export class UnauthorizedError extends ApiError {
  constructor(message = "unauthorized") {
    super(401, "unauthorized", message);
    this.name = "UnauthorizedError";
  }
}

export type FetchFn = typeof fetch;

export interface ApiClientOptions {
  token: string;
  onUnauthorized?: () => void;
  /** Base URL prefix; empty means same origin. */
  baseUrl?: string;
  /** Injectable for tests; defaults to the global fetch at call time. */
  fetchImpl?: FetchFn;
}

/**
 * Extract `{code, message}` from a FastAPI-style error body. The hub sends
 * either `{detail: {code, message}}` or `{detail: "text"}`.
 */
function errorFromBody(
  status: number,
  body: unknown,
  fallback: string,
): ApiError {
  let code = status === 404 ? "not_found" : `http_${status}`;
  let message = fallback;
  let retryAfter: number | undefined;
  let extra: Record<string, unknown> | undefined;
  if (body && typeof body === "object" && "detail" in body) {
    const detail = (body as { detail: unknown }).detail;
    if (typeof detail === "string") {
      message = detail;
    } else if (detail && typeof detail === "object") {
      const d = detail as {
        code?: unknown;
        message?: unknown;
        retry_after?: unknown;
      };
      if (typeof d.code === "string") code = d.code;
      if (typeof d.message === "string") message = d.message;
      if (typeof d.retry_after === "number") retryAfter = d.retry_after;
      extra = detail as Record<string, unknown>;
    }
  }
  return new ApiError(status, code, message, retryAfter, extra);
}

/** A research the hub runs for the person (api/research.py). */
export interface ResearchRun {
  id: string;
  question: string;
  status: "running" | "done" | "failed" | "stopped" | string;
  /** While it runs: how many blocks are done and what runs now. */
  progress: string | null;
  error: string | null;
  /** The report it filed in the shelf, once there is one. */
  report_id: string | null;
}

export class ApiClient {
  private readonly token: string;
  private readonly onUnauthorized?: () => void;
  private readonly baseUrl: string;
  private readonly fetchImpl?: FetchFn;

  constructor(opts: ApiClientOptions) {
    this.token = opts.token;
    this.onUnauthorized = opts.onUnauthorized;
    this.baseUrl = opts.baseUrl ?? "";
    this.fetchImpl = opts.fetchImpl;
  }

  private async request<T>(
    method: string,
    path: string,
    body?: unknown,
  ): Promise<T> {
    const headers: Record<string, string> = {
      Authorization: `Bearer ${this.token}`,
      Accept: "application/json",
    };
    const init: RequestInit = { method, headers };
    if (body !== undefined) {
      headers["Content-Type"] = "application/json";
      init.body = JSON.stringify(body);
    }
    const doFetch = this.fetchImpl ?? globalThis.fetch;
    let res: Response;
    try {
      res = await doFetch(this.baseUrl + path, init);
    } catch (err) {
      throw new ApiError(
        0,
        "network",
        err instanceof Error ? err.message : String(err),
      );
    }
    if (res.status === 401) {
      this.onUnauthorized?.();
      throw new UnauthorizedError();
    }
    const text = await res.text();
    let parsed: unknown = undefined;
    if (text) {
      try {
        parsed = JSON.parse(text);
      } catch {
        if (res.ok)
          throw new ApiError(
            res.status,
            "bad_response",
            "invalid JSON from hub",
          );
      }
    }
    if (!res.ok) {
      throw errorFromBody(
        res.status,
        parsed,
        res.statusText || `HTTP ${res.status}`,
      );
    }
    return parsed as T;
  }

  releases(): Promise<ReleasesOverview> {
    return this.request("GET", "/api/releases");
  }

  startReleaseBuild(body: BuildStart): Promise<ReleaseBuild> {
    return this.request("POST", "/api/releases/builds", body);
  }

  /** Roll the hub out to a built image: its own click; the dry run is the default. */
  rolloutRelease(id: string, dryRun: boolean): Promise<ReleaseBuild> {
    return this.request("POST", `/api/releases/builds/${encodeURIComponent(id)}/rollout`, {
      dry_run: dryRun,
    });
  }

  releaseBuild(id: string): Promise<ReleaseBuild> {
    return this.request("GET", `/api/releases/builds/${encodeURIComponent(id)}`);
  }

  releaseManifests(id: string, channel: string): Promise<ReleasePreview> {
    return this.request(
      "GET",
      `/api/releases/builds/${encodeURIComponent(id)}/manifests?channel=${encodeURIComponent(channel)}`,
    );
  }

  /** Publish with the signatures the browser made; the key never goes along. */
  publishRelease(
    id: string,
    body: { channel: string; created: string; signatures: Record<string, string> },
  ): Promise<ReleaseBuild> {
    return this.request("POST", `/api/releases/builds/${encodeURIComponent(id)}/publish`, body);
  }

  me(): Promise<Me> {
    return this.request<Me>("GET", "/api/me");
  }

  // --- chat (E2-01) ----------------------------------------------------------------------

  listConversations(q?: string): Promise<Conversation[]> {
    const query = q?.trim() ? `?q=${encodeURIComponent(q.trim())}` : "";
    return this.request<Conversation[]>("GET", `/api/conversations${query}`);
  }

  createConversation(
    body: {
      title?: string;
      model?: string | null;
      node?: string | null;
      persona_ref?: string | null;
      /** What the assistant may use here; a workspace starts a conversation with its own (docs/DESKTOP.md). */
      tool_scope?: ToolScope | null;
      /** The workspace (application) the conversation is started in. */
      application?: string | null;
      /** Where it was started: a desktop window's view, ids and title (Clipper at a window). */
      context?: { module: string; props: Record<string, string | number | boolean>; title: string };
    } = {},
  ) {
    return this.request<Conversation>("POST", "/api/conversations", body);
  }

  getConversation(id: string): Promise<ConversationDetail> {
    return this.request<ConversationDetail>(
      "GET",
      `/api/conversations/${encodeURIComponent(id)}`,
    );
  }

  /** Only the given fields change; null resets model / node to automatic. */
  updateConversation(
    id: string,
    patch: {
      title?: string;
      model?: string | null;
      node?: string | null;
      quality?: Quality | null;
      tool_scope?: ToolScope | null;
      /** E3-06: null = no persona. */
      persona_ref?: string | null;
      /** Kept open in Start; done (false opens it again). */
      pinned?: boolean;
      done?: boolean;
    },
  ): Promise<Conversation> {
    return this.request<Conversation>(
      "PATCH",
      `/api/conversations/${encodeURIComponent(id)}`,
      patch,
    );
  }

  /** The chat to write in next: the same one, or (after a rest, or with `force`) a new chat that
   *  continues it. */
  nextSection(
    id: string,
    force = false,
  ): Promise<{ created: boolean; conversation: Conversation }> {
    return this.request<{ created: boolean; conversation: Conversation }>(
      "POST",
      `/api/conversations/${encodeURIComponent(id)}/section`,
      { force },
    );
  }

  /** The chats the open one continues, oldest first, the open one last. */
  conversationThread(id: string): Promise<ConversationThread> {
    return this.request<ConversationThread>(
      "GET",
      `/api/conversations/${encodeURIComponent(id)}/thread`,
    );
  }

  /** The sections of a continuous conversation, newest last. */
  conversationSegments(id: string): Promise<ConversationSegments> {
    return this.request<ConversationSegments>(
      "GET",
      `/api/conversations/${encodeURIComponent(id)}/segments`,
    );
  }

  /** "New section": the next message starts a new section. `created` is false when nothing
   *  has been said since the last one. */
  newSegment(id: string): Promise<{ created: boolean; enabled?: boolean }> {
    return this.request<{ created: boolean; enabled?: boolean }>(
      "POST",
      `/api/conversations/${encodeURIComponent(id)}/segments/break`,
    );
  }

  deleteConversation(id: string): Promise<void> {
    return this.request<void>(
      "DELETE",
      `/api/conversations/${encodeURIComponent(id)}`,
    );
  }

  // --- the library: reports apart from chats ------------------------------------------------

  /** Findings of the second model the user has not looked at. */
  reviews(): Promise<{ errors: ChatReview[]; hints: ChatReview[] }> {
    return this.request("GET", "/api/reviews");
  }

  /** A signed pointer at one message, for the development chat (`trace.read`). */
  messageReference(
    conversationId: string,
    messageId: number,
  ): Promise<{ reference: string; short: string }> {
    return this.request<{ reference: string; short: string }>(
      "POST",
      `/api/conversations/${encodeURIComponent(conversationId)}/messages/${messageId}/reference`,
    );
  }

  /** What the hub still does in the background for a chat (the running summary, the second
   *  look). Writing never waits for it. */
  conversationBackground(
    conversationId: string,
  ): Promise<{ state: "idle" | "waiting" | "running" }> {
    return this.request<{ state: "idle" | "waiting" | "running" }>(
      "GET",
      `/api/conversations/${encodeURIComponent(conversationId)}/background`,
    );
  }

  /** The traffic light of each answer: the verdict of its second look by message id, and the
   *  answers that still wait for it. */
  reviewLights(
    conversationId: string,
  ): Promise<{ lights: Record<string, "ok" | "hint" | "error">; waiting: number[] }> {
    return this.request(
      "GET",
      `/api/conversations/${encodeURIComponent(conversationId)}/review-lights`,
    );
  }

  conversationReviews(conversationId: string): Promise<ChatReview[]> {
    return this.request<ChatReview[]>(
      "GET",
      `/api/conversations/${encodeURIComponent(conversationId)}/reviews`,
    );
  }

  markReviewSeen(id: string): Promise<void> {
    return this.request<void>(
      "POST",
      `/api/reviews/${encodeURIComponent(id)}/seen`,
    );
  }

  artifacts(): Promise<ArtifactList> {
    return this.request<ArtifactList>("GET", "/api/artifacts");
  }

  artifact(id: string): Promise<Artifact> {
    return this.request<Artifact>(
      "GET",
      `/api/artifacts/${encodeURIComponent(id)}`,
    );
  }

  markArtifactRead(id: string): Promise<void> {
    return this.request<void>(
      "POST",
      `/api/artifacts/${encodeURIComponent(id)}/read`,
    );
  }

  deleteArtifact(id: string): Promise<void> {
    return this.request<void>(
      "DELETE",
      `/api/artifacts/${encodeURIComponent(id)}`,
    );
  }

  reports(): Promise<{ reports: Report[] }> {
    return this.request<{ reports: Report[] }>("GET", "/api/reports");
  }

  report(id: string, version?: number): Promise<ReportDetail> {
    const q = version ? `?version=${version}` : "";
    return this.request<ReportDetail>(
      "GET",
      `/api/reports/${encodeURIComponent(id)}${q}`,
    );
  }

  reportVersions(id: string): Promise<{ versions: ReportVersionInfo[] }> {
    return this.request<{ versions: ReportVersionInfo[] }>(
      "GET",
      `/api/reports/${encodeURIComponent(id)}/versions`,
    );
  }

  reportDiff(id: string, from: number, to: number): Promise<ReportDiff> {
    return this.request<ReportDiff>(
      "GET",
      `/api/reports/${encodeURIComponent(id)}/diff?from=${from}&to=${to}`,
    );
  }

  addReportComment(
    id: string,
    body: { text: string; anchor?: string; version?: number },
  ): Promise<ReportComment> {
    return this.request<ReportComment>(
      "POST",
      `/api/reports/${encodeURIComponent(id)}/comments`,
      body,
    );
  }

  resolveReportComment(
    id: string,
    commentId: string,
    resolved: boolean,
  ): Promise<ReportComment> {
    return this.request<ReportComment>(
      "PATCH",
      `/api/reports/${encodeURIComponent(id)}/comments/${encodeURIComponent(commentId)}`,
      { resolved },
    );
  }

  setReportStatus(id: string, status: ReportStatus): Promise<Report> {
    return this.request<Report>(
      "POST",
      `/api/reports/${encodeURIComponent(id)}/status`,
      { status },
    );
  }

  /** Have the report revised: a research task gets the current version and the open comments. */
  reviseReport(
    id: string,
    note = "",
  ): Promise<{ task_id: string; from_version: number; open_comments: number }> {
    return this.request(
      "POST",
      `/api/reports/${encodeURIComponent(id)}/revise`,
      { note },
    );
  }

  markReportRead(id: string): Promise<void> {
    return this.request<void>(
      "POST",
      `/api/reports/${encodeURIComponent(id)}/read`,
    );
  }

  deleteReport(id: string): Promise<void> {
    return this.request<void>(
      "DELETE",
      `/api/reports/${encodeURIComponent(id)}`,
    );
  }

  // --- Sheets: chord sheets, their views per instrument, the research profile "sheets" ---------
  sheets(): Promise<{ sheets: SheetSummary[] }> {
    return this.request<{ sheets: SheetSummary[] }>("GET", "/api/sheets");
  }

  sheet(id: string, params: SheetViewParams = {}): Promise<SheetDetail> {
    const q = new URLSearchParams();
    if (params.instrument) q.set("instrument", params.instrument);
    if (params.capo_view !== undefined) q.set("capo_view", String(params.capo_view));
    if (params.transpose) q.set("transpose", String(params.transpose));
    const qs = q.toString();
    return this.request<SheetDetail>("GET", `/api/sheets/${encodeURIComponent(id)}${qs ? `?${qs}` : ""}`);
  }

  sheetCreate(body: {
    text: string;
    format: "chordpro" | "plain";
    instrument: Instrument;
    title?: string;
    artist?: string;
  }): Promise<SheetDetail> {
    return this.request<SheetDetail>("POST", "/api/sheets", body);
  }

  sheetUpdate(id: string, patch: SheetPatch): Promise<SheetDetail> {
    return this.request<SheetDetail>("PATCH", `/api/sheets/${encodeURIComponent(id)}`, patch);
  }

  sheetDelete(id: string): Promise<void> {
    return this.request<void>("DELETE", `/api/sheets/${encodeURIComponent(id)}`);
  }

  sheetCompare(id: string, source: number): Promise<SheetComparison> {
    return this.request<SheetComparison>(
      "GET",
      `/api/sheets/${encodeURIComponent(id)}/compare?source=${source}`,
    );
  }

  sheetResearchStart(body: {
    song: string;
    artist: string;
    instrument: Instrument;
    wish: string;
  }): Promise<SheetResearchJob> {
    return this.request<SheetResearchJob>("POST", "/api/sheets/research", body);
  }

  sheetResearchJobs(): Promise<{ jobs: SheetResearchJob[] }> {
    return this.request<{ jobs: SheetResearchJob[] }>("GET", "/api/sheets/research");
  }

  // --- the wiki: pages taken over from projects, released by a click ---------------------------

  wikiPages(): Promise<WikiTree> {
    return this.request<WikiTree>("GET", "/api/wiki/pages");
  }

  wikiPage(id: string, version?: number): Promise<WikiPageDetail> {
    const q = version ? `?version=${version}` : "";
    return this.request<WikiPageDetail>("GET", `/api/wiki/pages/${encodeURIComponent(id)}${q}`);
  }

  wikiUpdatePage(id: string, patch: WikiPagePatch): Promise<WikiPage> {
    return this.request<WikiPage>("PATCH", `/api/wiki/pages/${encodeURIComponent(id)}`, patch);
  }

  wikiRenameFolder(old: string, next: string): Promise<{ moved: number }> {
    return this.request<{ moved: number }>("POST", "/api/wiki/folders/rename", { old, new: next });
  }

  wikiDiff(id: string, from: number, to: number): Promise<ReportDiff> {
    return this.request<ReportDiff>(
      "GET",
      `/api/wiki/pages/${encodeURIComponent(id)}/diff?old=${from}&new=${to}`,
    );
  }

  /** What taking a document of the Ablage into the wiki would do (changes nothing). */
  wikiAblagePreview(kind: string, id: string, lang: string): Promise<WikiTakeOver> {
    return this.request<WikiTakeOver>(
      "GET",
      `/api/wiki/ablage/${encodeURIComponent(kind)}/${encodeURIComponent(id)}?lang=${encodeURIComponent(lang)}`,
    );
  }

  /** The person's click: a document of the Ablage becomes a page in its application's folder. */
  wikiAblageTakeOver(kind: string, id: string, lang: string): Promise<WikiPage & { version: number }> {
    return this.request<WikiPage & { version: number }>(
      "POST",
      `/api/wiki/ablage/${encodeURIComponent(kind)}/${encodeURIComponent(id)}?lang=${encodeURIComponent(lang)}`,
    );
  }

  /** What taking this document into the wiki would do (changes nothing). */
  wikiTakeOverPreview(nodeId: string): Promise<WikiTakeOver> {
    return this.request<WikiTakeOver>("GET", `/api/project/nodes/${encodeURIComponent(nodeId)}/wiki`);
  }

  /** The person's click on the preview card: a new page or the next version of it. */
  wikiTakeOver(nodeId: string): Promise<WikiPage & { version: number }> {
    return this.request("POST", `/api/project/nodes/${encodeURIComponent(nodeId)}/wiki`);
  }

  /** With ``application``: only the tools that application allows (its ceiling). */
  chatTools(application?: string): Promise<ChatToolsOverview> {
    const q = application ? `?application=${encodeURIComponent(application)}` : "";
    return this.request<ChatToolsOverview>("GET", `/api/chat/tools${q}`);
  }

  chatModels(): Promise<ChatModel[]> {
    return this.request<ChatModel[]>("GET", "/api/chat/models");
  }

  chatSettings(): Promise<ChatSettings> {
    return this.request<ChatSettings>("GET", "/api/chat/settings");
  }

  putChatSettings(body: {
    system_prompt: string | null;
    temperature: number | null;
    address: "du" | "sie";
    memory_enabled: boolean;
    quality?: Quality;
    internet?: boolean;
  }): Promise<ChatSettings> {
    return this.request<ChatSettings>("PUT", "/api/chat/settings", body);
  }

  // --- the core, personas and prompt snapshots (E3-06) --------------------------------------

  core(context?: number): Promise<CoreOverview> {
    return this.request<CoreOverview>(
      "GET",
      `/api/core${context ? `?context=${context}` : ""}`,
    );
  }

  addCoreItem(body: {
    kind: "style" | "rule";
    text: string;
    example?: string | null;
    core?: boolean;
  }): Promise<CoreItem> {
    return this.request<CoreItem>("POST", "/api/core/items", body);
  }

  editCoreItem(
    id: string,
    patch: { text?: string; example?: string | null; core?: boolean },
  ): Promise<CoreItem> {
    return this.request<CoreItem>(
      "PATCH",
      `/api/core/items/${encodeURIComponent(id)}`,
      patch,
    );
  }

  deleteCoreItem(id: string): Promise<CoreItem> {
    return this.request<CoreItem>(
      "DELETE",
      `/api/core/items/${encodeURIComponent(id)}`,
    );
  }

  addHouseholdItem(body: { text: string; core?: boolean }): Promise<CoreItem> {
    return this.request<CoreItem>("POST", "/api/core/household/items", body);
  }

  editHouseholdItem(
    id: string,
    patch: { text?: string; core?: boolean },
  ): Promise<CoreItem> {
    return this.request<CoreItem>(
      "PATCH",
      `/api/core/household/items/${encodeURIComponent(id)}`,
      patch,
    );
  }

  deleteHouseholdItem(id: string): Promise<CoreItem> {
    return this.request<CoreItem>(
      "DELETE",
      `/api/core/household/items/${encodeURIComponent(id)}`,
    );
  }

  putSystemRules(
    lang: "en" | "de",
    text: string,
  ): Promise<{ version: number; text: string }> {
    return this.request("PUT", `/api/core/system/${lang}`, { text });
  }

  coreProposals(): Promise<CoreProposal[]> {
    return this.request<CoreProposal[]>("GET", "/api/core/proposals");
  }

  acceptProposal(
    id: string,
    edit?: { text?: string; example?: string | null },
  ): Promise<{ proposal: CoreProposal; item: CoreItem | null }> {
    return this.request(
      "POST",
      `/api/core/proposals/${encodeURIComponent(id)}/accept`,
      edit ?? undefined,
    );
  }

  rejectProposal(id: string): Promise<CoreProposal> {
    return this.request<CoreProposal>(
      "POST",
      `/api/core/proposals/${encodeURIComponent(id)}/reject`,
    );
  }

  personas(): Promise<PersonaLibrary> {
    return this.request<PersonaLibrary>("GET", "/api/personas");
  }

  createPersona(body: {
    name: string;
    components: PersonaComponents;
    example?: { de?: string; en?: string } | string | null;
    scope?: "user" | "household";
  }): Promise<Persona> {
    return this.request<Persona>("POST", "/api/personas", body);
  }

  updatePersona(
    id: string,
    patch: {
      name?: string;
      components?: PersonaComponents;
      example?: { de?: string; en?: string } | string | null;
    },
  ): Promise<Persona> {
    return this.request<Persona>(
      "PUT",
      `/api/personas/${encodeURIComponent(id)}`,
      patch,
    );
  }

  archivePersona(id: string): Promise<void> {
    return this.request<void>(
      "DELETE",
      `/api/personas/${encodeURIComponent(id)}`,
    );
  }

  setDefaultPersona(
    personaId: string | null,
  ): Promise<{ default_id: string | null }> {
    return this.request("PUT", "/api/personas/default", {
      persona_id: personaId,
    });
  }

  personaStats(): Promise<PersonaStat[]> {
    return this.request<PersonaStat[]>("GET", "/api/personas/stats");
  }

  setWizardState(state: "later" | "skipped"): Promise<{ state: WizardState }> {
    return this.request("PUT", "/api/personas/wizard", { state });
  }

  questionnaire(): Promise<Questionnaire> {
    return this.request<Questionnaire>("GET", "/api/personas/questionnaire");
  }

  previewQuestionnaire(
    answers: QuestionnaireAnswers,
  ): Promise<QuestionnairePreview> {
    return this.request<QuestionnairePreview>(
      "POST",
      "/api/personas/questionnaire/preview",
      {
        answers,
      },
    );
  }

  confirmQuestionnaire(
    answers: QuestionnaireAnswers,
    name?: string,
  ): Promise<Persona> {
    return this.request<Persona>(
      "POST",
      "/api/personas/questionnaire/confirm",
      {
        answers,
        ...(name ? { name } : {}),
      },
    );
  }

  promptSnapshot(hash: string): Promise<PromptSnapshot> {
    return this.request<PromptSnapshot>(
      "GET",
      `/api/chat/prompt-snapshots/${hash}`,
    );
  }

  messageFeedback(
    conversationId: string,
    messageId: number,
    verdict: "helped" | "unfit" | null,
  ): Promise<ChatMessage> {
    return this.request<ChatMessage>(
      "POST",
      `/api/conversations/${encodeURIComponent(conversationId)}/messages/${messageId}/feedback`,
      { verdict },
    );
  }

  // --- memory (E2-02) --------------------------------------------------------------------

  memory(
    filter: {
      q?: string;
      status?: string;
      evidence?: string;
      kind?: string;
      /** `core`, or a program key. */
      application?: string;
      tag?: string;
    } = {},
  ): Promise<MemoryOverview> {
    const params = new URLSearchParams();
    for (const [key, value] of Object.entries(filter))
      if (value?.trim()) params.set(key, value);
    const query = params.toString();
    return this.request<MemoryOverview>(
      "GET",
      `/api/memory${query ? `?${query}` : ""}`,
    );
  }

  /** Move a note to a program (null = the core) and/or set its tags (names; `parent/name` makes a
   *  new tag below a known one). */
  placeMemory(
    id: string,
    body: { application?: string | null; tags?: string[] },
  ): Promise<MemoryItem> {
    return this.request<MemoryItem>("PATCH", `/api/memory/${encodeURIComponent(id)}/placement`, body);
  }

  memorySort(): Promise<{ suggestions: MemorySortSuggestion[] }> {
    return this.request("GET", "/api/memory/sort");
  }

  applyMemorySort(assignments: { id: string; application: string | null }[]): Promise<{ moved: number }> {
    return this.request("POST", "/api/memory/sort", { assignments });
  }

  memoryTags(): Promise<MemoryTagTree> {
    return this.request("GET", "/api/memory/tags");
  }

  createMemoryTag(body: { name: string; parent_id: string; description?: string }): Promise<MemoryTagTree> {
    return this.request("POST", "/api/memory/tags", body);
  }

  /** Only the fields given change; null for responsible / sensitive / released = inherited. */
  updateMemoryTag(
    id: string,
    body: {
      name?: string;
      parent_id?: string;
      description?: string;
      responsible?: string | null;
      sensitive?: boolean | null;
      released?: string[] | null;
      reviewed?: boolean;
    },
  ): Promise<MemoryTagTree> {
    return this.request("PATCH", `/api/memory/tags/${encodeURIComponent(id)}`, body);
  }

  mergeMemoryTag(id: string, into: string): Promise<MemoryTagTree> {
    return this.request("POST", `/api/memory/tags/${encodeURIComponent(id)}/merge`, { into });
  }

  deleteMemoryTag(id: string): Promise<MemoryTagTree> {
    return this.request("DELETE", `/api/memory/tags/${encodeURIComponent(id)}`);
  }

  memoryItem(id: string): Promise<MemoryItem & { versions: MemoryVersion[] }> {
    return this.request("GET", `/api/memory/${encodeURIComponent(id)}`);
  }

  conversationMemory(conversationId: string): Promise<MemoryItem[]> {
    return this.request<MemoryItem[]>(
      "GET",
      `/api/conversations/${encodeURIComponent(conversationId)}/memory`,
    );
  }

  editMemory(
    id: string,
    patch: { text?: string; evidence?: MemoryItem["evidence"] },
  ): Promise<MemoryItem> {
    return this.request<MemoryItem>(
      "PATCH",
      `/api/memory/${encodeURIComponent(id)}`,
      patch,
    );
  }

  revertMemory(id: string, version: number): Promise<MemoryItem> {
    return this.request<MemoryItem>(
      "POST",
      `/api/memory/${encodeURIComponent(id)}/revert`,
      {
        version,
      },
    );
  }

  /** Soft delete: the note is hidden and can be restored. */
  deleteMemory(id: string): Promise<MemoryItem> {
    return this.request<MemoryItem>(
      "DELETE",
      `/api/memory/${encodeURIComponent(id)}`,
    );
  }

  restoreMemory(id: string): Promise<MemoryItem> {
    return this.request<MemoryItem>(
      "POST",
      `/api/memory/${encodeURIComponent(id)}/restore`,
    );
  }

  /** Take back a note the assistant wrote because of a statement (restores what it replaced). */
  undoMemory(id: string): Promise<MemoryItem> {
    return this.request<MemoryItem>(
      "POST",
      `/api/memory/${encodeURIComponent(id)}/undo`,
    );
  }

  /** The clarification questions waiting for the signed-in user. */
  memoryQuestions(): Promise<MemoryQuestion[]> {
    return this.request<MemoryQuestion[]>("GET", "/api/memory/questions");
  }

  answerQuestion(id: string, answer: QuestionAnswer): Promise<unknown> {
    return this.request(
      "POST",
      `/api/memory/questions/${encodeURIComponent(id)}/answer`,
      answer,
    );
  }

  dismissQuestion(id: string): Promise<unknown> {
    return this.request(
      "POST",
      `/api/memory/questions/${encodeURIComponent(id)}/dismiss`,
    );
  }

  resolveMemory(
    id: string,
    keep: "this" | "other" | "both",
  ): Promise<MemoryItem> {
    return this.request<MemoryItem>(
      "POST",
      `/api/memory/${encodeURIComponent(id)}/resolve`,
      {
        keep,
      },
    );
  }

  forgetAllMemory(): Promise<{ forgotten: number }> {
    return this.request("POST", "/api/memory/forget-all");
  }

  purgeDeletedMemory(): Promise<{ purged: number }> {
    return this.request("POST", "/api/memory/purge-deleted");
  }

  consolidateMemory(): Promise<{ started: boolean }> {
    return this.request("POST", "/api/memory/consolidate");
  }

  /** What the cross-check told a program (or the person: `person`). */
  memoryReports(recipient: string): Promise<{ reports: MemoryReport[] }> {
    return this.request("GET", `/api/memory/reports?recipient=${encodeURIComponent(recipient)}`);
  }

  markReportSeen(id: string): Promise<{ id: string; status: string }> {
    return this.request("POST", `/api/memory/reports/${encodeURIComponent(id)}/seen`);
  }

  crosscheckMemory(): Promise<{ started: boolean }> {
    return this.request("POST", "/api/memory/crosscheck");
  }

  /** E3-05: start a care run (check, expire, archive) now. */
  careMemory(): Promise<{ started: boolean }> {
    return this.request("POST", "/api/memory/care");
  }

  /** Digest of the chats without a checked summary, now: returns how many chats were started. */
  digestMemory(): Promise<{ started: number }> {
    return this.request("POST", "/api/memory/digest");
  }

  /** E3-05: "this note helped" / "this note did not fit": it weighs heavily in its usefulness. */
  memoryFeedback(id: string, helped: boolean): Promise<MemoryItem> {
    return this.request<MemoryItem>(
      "POST",
      `/api/memory/${encodeURIComponent(id)}/feedback`,
      {
        helped,
      },
    );
  }

  /** E3-05: put a note away (not recalled, still searchable, restorable). */
  archiveMemory(id: string): Promise<MemoryItem> {
    return this.request<MemoryItem>(
      "POST",
      `/api/memory/${encodeURIComponent(id)}/archive`,
    );
  }


  /** Send a message and stream the answer. Aborting `signal` is the stop button. */
  sendMessage(
    id: string,
    content: string,
    signal: AbortSignal,
    tools: ToolsMode = "auto",
    track?: StreamTrack,
  ): AsyncGenerator<ChatEvent> {
    return this.chatStream(
      `/api/conversations/${encodeURIComponent(id)}/messages`,
      { content, tools },
      signal,
      track,
    );
  }

  /** E3-07: follow an answer from the event `offset` on (0 = from its start, as a reload does).
   *  The answer runs in the hub whatever this reader does; closing it stops nothing. */
  attachAnswer(
    messageId: number,
    offset: number,
    signal: AbortSignal,
    track?: StreamTrack,
  ): AsyncGenerator<ChatEvent> {
    return this.chatStream(
      `/api/chat/messages/${messageId}/stream?offset=${offset}`,
      undefined,
      signal,
      track,
    );
  }

  /** E3-07: stop a running answer (the text so far is kept). Closing the page does not. */
  stopAnswer(messageId: number): Promise<ChatMessage> {
    return this.request<ChatMessage>(
      "POST",
      `/api/chat/messages/${messageId}/stop`,
    );
  }

  /** E7-01: a note for the check that runs for this answer; it reads it before its next turn. */
  steerCheck(
    messageId: number,
    text: string,
  ): Promise<{ queued: boolean; text: string }> {
    return this.request<{ queued: boolean; text: string }>(
      "POST",
      `/api/chat/messages/${messageId}/steer`,
      { text },
    );
  }

  /** E7-01: hold the check of this answer before its next turn, or let it go on. */
  pauseCheck(messageId: number, paused: boolean): Promise<{ paused: boolean }> {
    return this.request<{ paused: boolean }>(
      "POST",
      `/api/chat/messages/${messageId}/pause`,
      { paused },
    );
  }

  // --- parameters that change while the hub runs (E7-03) ------------------------------------

  params(): Promise<ParamsView> {
    return this.request<ParamsView>("GET", "/api/admin/params");
  }

  setParam(key: string, value: unknown): Promise<{ key: string; value: unknown }> {
    return this.request<{ key: string; value: unknown }>(
      "PUT",
      `/api/admin/params/${encodeURIComponent(key)}`,
      { value },
    );
  }

  resetParam(key: string): Promise<{ key: string; value: unknown }> {
    return this.request<{ key: string; value: unknown }>(
      "DELETE",
      `/api/admin/params/${encodeURIComponent(key)}`,
    );
  }

  paramHistory(key: string): Promise<ParamChange[]> {
    return this.request<ParamChange[]>(
      "GET",
      `/api/admin/params/${encodeURIComponent(key)}/history`,
    );
  }

  exportParams(): Promise<ParamPreset> {
    return this.request<ParamPreset>("GET", "/api/admin/params/export");
  }

  importParams(preset: ParamPreset): Promise<{ applied: string[] }> {
    return this.request<{ applied: string[] }>("PUT", "/api/admin/params/import", preset);
  }

  // --- the incognito folder (docs/PLATFORM.md, 3.9) ------------------------------------------------

  incognitoTabs(): Promise<{ tabs: IncognitoTab[] }> {
    return this.request("GET", "/api/incognito");
  }

  createIncognitoTab(internet = true): Promise<IncognitoTab> {
    return this.request("POST", "/api/incognito", { internet });
  }

  incognitoTab(id: string): Promise<IncognitoTab> {
    return this.request("GET", `/api/incognito/${encodeURIComponent(id)}`);
  }

  updateIncognitoTab(id: string, internet: boolean): Promise<IncognitoTab> {
    return this.request("PATCH", `/api/incognito/${encodeURIComponent(id)}`, { internet });
  }

  sendIncognito(id: string, content: string): Promise<{ message: IncognitoMessage; tab: IncognitoTab }> {
    return this.request("POST", `/api/incognito/${encodeURIComponent(id)}/messages`, { content });
  }

  deleteIncognitoTab(id: string): Promise<void> {
    return this.request<void>("DELETE", `/api/incognito/${encodeURIComponent(id)}`);
  }

  // --- hand-over between applications (docs/PLATFORM.md, 3.8) --------------------------------------

  handovers(): Promise<{ handovers: Handover[] }> {
    return this.request("GET", "/api/handovers");
  }

  prepareHandover(body: {
    to_application: string;
    conversation_id?: string;
    incognito_tab?: string;
    note?: string;
  }): Promise<Handover> {
    return this.request("POST", "/api/handovers", body);
  }

  /** Sending binds to the dossier the person saw; a dossier that holds a secret needs the
   *  explicit approval of this click (approveCritical). */
  sendHandover(id: string, hash: string, approveCritical = false): Promise<Handover> {
    const body = approveCritical ? { hash, approve_critical: true } : { hash };
    return this.request("POST", `/api/handovers/${encodeURIComponent(id)}/send`, body);
  }

  /** The machines the nodes run on, with the nodes on each. */
  machines(): Promise<MachinesView> {
    return this.request("GET", "/api/machines");
  }

  putMachines(body: {
    machines: { key: string; name: string; kind: string; host?: string }[];
    assign: Record<string, string>;
  }): Promise<MachinesView> {
    return this.request("PUT", "/api/machines", body);
  }

  /** The Akte of a hand-over as the Markdown text the person reads. */
  handoverFile(id: string): Promise<{ id: string; markdown: string; knowledge_path: string | null }> {
    return this.request("GET", `/api/handovers/${encodeURIComponent(id)}/file`);
  }

  cancelHandover(id: string): Promise<Handover> {
    return this.request("POST", `/api/handovers/${encodeURIComponent(id)}/cancel`);
  }

  // --- the project application (brief PPH-01-planning-app) -------------------------------

  projectChanges(taskId: string): Promise<ProjectChange[]> {
    return this.request("GET", `/api/project/changes?task_id=${encodeURIComponent(taskId)}`);
  }

  applyProjectChange(id: string): Promise<ProjectChange> {
    return this.request("POST", `/api/project/changes/${encodeURIComponent(id)}/apply`);
  }

  refuseProjectChange(id: string): Promise<ProjectChange> {
    return this.request("POST", `/api/project/changes/${encodeURIComponent(id)}/refuse`);
  }

  projectProjects(): Promise<{ projects: Project[] }> {
    return this.request("GET", "/api/project/projects");
  }

  createProject(body: ProjectCreate): Promise<Project> {
    return this.request("POST", "/api/project/projects", body);
  }

  projectProject(id: string): Promise<ProjectDetail> {
    return this.request("GET", `/api/project/projects/${encodeURIComponent(id)}`);
  }

  updateProject(id: string, body: ProjectPatch): Promise<Project> {
    return this.request("PATCH", `/api/project/projects/${encodeURIComponent(id)}`, body);
  }

  createProjectNode(projectId: string, body: ProjectNodeCreate): Promise<ProjectNode> {
    return this.request("POST", `/api/project/projects/${encodeURIComponent(projectId)}/nodes`, body);
  }

  updateProjectNode(id: string, body: ProjectNodePatch): Promise<ProjectNode> {
    return this.request("PATCH", `/api/project/nodes/${encodeURIComponent(id)}`, body);
  }

  moveProjectNode(id: string, body: ProjectMove): Promise<ProjectNode> {
    return this.request("POST", `/api/project/nodes/${encodeURIComponent(id)}/move`, body);
  }

  answerProjectQuestion(id: string, answer: string): Promise<ProjectNode> {
    return this.request("POST", `/api/project/nodes/${encodeURIComponent(id)}/answer`, { answer });
  }

  decideProjectRequest(id: string, confirm: boolean): Promise<ProjectNode> {
    return this.request("POST", `/api/project/nodes/${encodeURIComponent(id)}/decide`, { confirm });
  }

  projectChains(projectId: string): Promise<{ chains: ProjectChain[] }> {
    return this.request("GET", `/api/project/projects/${encodeURIComponent(projectId)}/chains`);
  }

  stopProjectChain(id: string): Promise<{ chain: ProjectChain; stopped: string[] }> {
    return this.request("POST", `/api/project/chains/${encodeURIComponent(id)}/stop`, {});
  }

  projectMandates(projectId: string): Promise<{ mandates: ProjectMandate[] }> {
    return this.request("GET", `/api/project/projects/${encodeURIComponent(projectId)}/mandates`);
  }

  /** The click on an order that waits: release it and give the mandate for its branch. */
  releaseProjectOrder(
    id: string,
    body: OrderRelease,
  ): Promise<{ order: ProjectNode; mandate: ProjectMandate; queue_note: string }> {
    return this.request("POST", `/api/project/nodes/${encodeURIComponent(id)}/mandate`, body);
  }

  /** The click on the card of an issue: the probe mandate of its investigation (hub issue_probes.py, D83). */
  allowProbes(issueId: string): Promise<{ mandate: ProjectMandate }> {
    return this.request("POST", `/api/project/nodes/${encodeURIComponent(issueId)}/probe-mandate`, {});
  }

  revokeProjectMandate(id: string): Promise<{ mandate: ProjectMandate }> {
    return this.request("POST", `/api/project/mandates/${encodeURIComponent(id)}/revoke`, {});
  }

  decideProjectProposal(id: string, accept: boolean): Promise<ProjectNode> {
    return this.request("POST", `/api/project/nodes/${encodeURIComponent(id)}/proposal`, { accept });
  }

  setProjectScope(
    projectId: string,
    body: { revision: number; scope: ProjectScope },
  ): Promise<Project> {
    return this.request("PUT", `/api/project/projects/${encodeURIComponent(projectId)}/scope`, body);
  }

  startProjectRun(
    projectId: string,
    body: { prompt: string; model?: string | null },
  ): Promise<{ id: string; title: string }> {
    return this.request(
      "POST",
      `/api/project/projects/${encodeURIComponent(projectId)}/runs`,
      body,
    );
  }

  /** Start one task node in its application; the node shows in progress until the run ends. */
  runProjectNode(id: string): Promise<{ id: string; title: string }> {
    return this.request("POST", `/api/project/nodes/${encodeURIComponent(id)}/run`);
  }

  /** The next iteration of a task (one click of the person): it continues `based_on` (a result
   *  of the task, none: the newest) with the feedback; `keep` makes the feedback a requirement. */
  iterateProjectNode(
    id: string,
    body: {
      based_on?: string | null;
      feedback: string;
      keep: boolean;
      program?: "issue_check" | "issue_fix" | "issue_steps" | "issue_investigate" | "issue_plan";
    },
  ): Promise<{ task: string; run: string }> {
    return this.request("POST", `/api/project/nodes/${encodeURIComponent(id)}/iterate`, body);
  }

  /** The person's own plan of an issue ("Plan vorgeben", hub RunService.given_plan): checked against
   *  the live code and filed as the issue's result; a plan that does not fit answers 422. */
  giveIssuePlan(
    id: string,
    plan: import("../project/types").IssuePlan,
  ): Promise<{ plan: import("../project/types").IssuePlan }> {
    return this.request("POST", `/api/project/nodes/${encodeURIComponent(id)}/plan`, plan);
  }

  /** Issues from the running system (hub project_issues.py): the card's click files one. */
  fileIssue(body: {
    text: string;
    application?: string | null;
    conversation?: string | null;
  }): Promise<{ task: ProjectNode; similar: { id: string; number: string; title: string }[] }> {
    return this.request("POST", "/api/project/issues", body);
  }

  checkOpenIssues(projectId: string): Promise<{ batch: unknown; count: number }> {
    return this.request("POST", "/api/project/issues/check-open", { project_id: projectId });
  }

  /** The fixes of an issue (hub issue_fixes.py, D103, D104). */
  issueFixes(
    nodeId: string,
  ): Promise<{
    fixes: import("../project/types").IssueFix[];
    plan?: import("../project/types").IssuePlan | null;
    probe_mandate?: ProjectMandate | null;
    probe_scope?: string;
  }> {
    return this.request("GET", `/api/project/nodes/${encodeURIComponent(nodeId)}/fixes`);
  }

  applyFix(id: string): Promise<import("../project/types").IssueFix> {
    return this.request("POST", `/api/project/fixes/${encodeURIComponent(id)}/apply`);
  }

  rolloutFix(id: string): Promise<import("../project/types").IssueFix> {
    return this.request("POST", `/api/project/fixes/${encodeURIComponent(id)}/rollout`);
  }

  discardFix(id: string): Promise<import("../project/types").IssueFix> {
    return this.request("POST", `/api/project/fixes/${encodeURIComponent(id)}/discard`);
  }

  confirmIssue(id: string, body: { works: boolean; feedback?: string }): Promise<{ task: ProjectNode; run: string | null }> {
    return this.request("POST", `/api/project/nodes/${encodeURIComponent(id)}/confirm`, body);
  }

  projectIterations(id: string): Promise<import("../project/types").ProjectIterations> {
    return this.request("GET", `/api/project/nodes/${encodeURIComponent(id)}/iterations`);
  }

  /** What every further iteration of a task must hold; the person adds and strikes out. */
  putProjectRequirements(
    id: string,
    body: { revision: number; items: (string | import("../project/types").ProjectRequirement)[] },
  ): Promise<import("../project/types").ProjectNode> {
    return this.request("PUT", `/api/project/nodes/${encodeURIComponent(id)}/requirements`, body);
  }

  /** The person's approval of a batch: the tasks run in this order while their results fit. */
  createProjectBatch(
    projectId: string,
    body: { node_ids: string[]; max_runs: number },
  ): Promise<import("../project/types").ProjectBatch> {
    return this.request(
      "POST",
      `/api/project/projects/${encodeURIComponent(projectId)}/batches`,
      body,
    );
  }

  listProjectBatches(
    projectId: string,
  ): Promise<{ batches: import("../project/types").ProjectBatch[] }> {
    return this.request(
      "GET",
      `/api/project/projects/${encodeURIComponent(projectId)}/batches`,
    );
  }

  /** The batch and the state of each of its tasks (the card in the chat reads it while it runs). */
  projectBatchState(id: string): Promise<import("../project/types").ProjectBatchState> {
    return this.request("GET", `/api/project/batches/${encodeURIComponent(id)}`);
  }

  stopProjectBatch(id: string): Promise<import("../project/types").ProjectBatch> {
    return this.request("POST", `/api/project/batches/${encodeURIComponent(id)}/stop`);
  }

  // --- PPH-04: search, labels, relations, reminders, saved views ---------------------------------

  /** Nodes across the person's projects: `filter` and `sort` are the hub's query language. */
  searchProjectNodes(opts: {
    filter?: string;
    sort?: string;
    project?: string;
    limit?: number;
    after?: number;
  }): Promise<ProjectSearch> {
    const q = new URLSearchParams();
    if (opts.filter) q.set("filter", opts.filter);
    if (opts.sort) q.set("sort", opts.sort);
    if (opts.project) q.set("project", opts.project);
    q.set("limit", String(opts.limit ?? 100));
    if (opts.after) q.set("after", String(opts.after));
    return this.request("GET", `/api/project/nodes?${q.toString()}`);
  }

  completeProjectNode(id: string, revision: number): Promise<ProjectCompleted> {
    return this.request("POST", `/api/project/nodes/${encodeURIComponent(id)}/complete`, { revision });
  }

  projectLabels(): Promise<{ labels: ProjectLabel[] }> {
    return this.request("GET", "/api/project/labels");
  }

  createProjectLabel(body: { title: string; color?: string | null }): Promise<ProjectLabel> {
    return this.request("POST", "/api/project/labels", body);
  }

  setProjectNodeLabels(
    nodeId: string,
    body: { add?: string[]; remove?: string[] },
  ): Promise<{ labels: ProjectLabel[] }> {
    return this.request("PUT", `/api/project/nodes/${encodeURIComponent(nodeId)}/labels`, body);
  }

  projectRelations(nodeId: string): Promise<{ relations: ProjectRelation[] }> {
    return this.request("GET", `/api/project/nodes/${encodeURIComponent(nodeId)}/relations`);
  }

  addProjectRelation(
    nodeId: string,
    body: { other_id: string; kind: ProjectRelationKind },
  ): Promise<{ relations: ProjectRelation[] }> {
    return this.request("POST", `/api/project/nodes/${encodeURIComponent(nodeId)}/relations`, body);
  }

  endProjectRelation(relationId: string): Promise<{ id: string; valid_to: string }> {
    return this.request("POST", `/api/project/relations/${encodeURIComponent(relationId)}/end`);
  }

  projectReminders(nodeId: string): Promise<{ reminders: ProjectReminder[] }> {
    return this.request("GET", `/api/project/nodes/${encodeURIComponent(nodeId)}/reminders`);
  }

  addProjectReminder(
    nodeId: string,
    body: { at?: string; relative_to?: "due" | "start" | "end"; offset_seconds?: number },
  ): Promise<ProjectReminder> {
    return this.request("POST", `/api/project/nodes/${encodeURIComponent(nodeId)}/reminders`, body);
  }

  removeProjectReminder(reminderId: string): Promise<void> {
    return this.request("DELETE", `/api/project/reminders/${encodeURIComponent(reminderId)}`);
  }

  projectViews(project?: string): Promise<{ views: ProjectSavedView[] }> {
    const q = project ? `?project=${encodeURIComponent(project)}` : "";
    return this.request("GET", `/api/project/views${q}`);
  }

  createProjectView(body: {
    title: string;
    type?: ProjectViewType;
    filter?: string;
    sort?: string;
    config?: Record<string, unknown>;
    project_id?: string | null;
  }): Promise<ProjectSavedView> {
    return this.request("POST", "/api/project/views", body);
  }

  deleteProjectView(id: string): Promise<void> {
    return this.request("DELETE", `/api/project/views/${encodeURIComponent(id)}`);
  }

  /** The grammar of the filter box, as the hub explains it. */
  projectFilterHelp(): Promise<{ help: string }> {
    return this.request("GET", "/api/project/filter/help");
  }

  // --- PPH-04 04f: the Akte, versions, trash, change sets ---------------------------------------

  projectTimeline(
    projectId: string,
    opts: { after?: number; kinds?: string[]; node?: string; limit?: number } = {},
  ): Promise<ProjectTimeline> {
    const q = new URLSearchParams();
    if (opts.after) q.set("after", String(opts.after));
    if (opts.kinds && opts.kinds.length > 0) q.set("kinds", opts.kinds.join(","));
    if (opts.node) q.set("node", opts.node);
    q.set("limit", String(opts.limit ?? 50));
    return this.request("GET", `/api/project/projects/${encodeURIComponent(projectId)}/timeline?${q.toString()}`);
  }

  verifyProjectTimeline(projectId: string): Promise<{ ok: boolean; problems: string[] }> {
    return this.request("GET", `/api/project/projects/${encodeURIComponent(projectId)}/timeline/verify`);
  }

  projectNodeHistory(nodeId: string): Promise<{ versions: ProjectVersion[] }> {
    return this.request("GET", `/api/project/nodes/${encodeURIComponent(nodeId)}/history`);
  }

  projectNodeDiff(nodeId: string, from: number, to: number): Promise<ProjectDiff> {
    return this.request("GET", `/api/project/nodes/${encodeURIComponent(nodeId)}/diff?from=${from}&to=${to}`);
  }

  /** Put a node's title and body back as they were at `toRev`: a new revision, nothing overwritten. */
  revertProjectNode(nodeId: string, body: { revision: number; to_rev: number }): Promise<ProjectNode> {
    return this.request("POST", `/api/project/nodes/${encodeURIComponent(nodeId)}/revert`, body);
  }

  projectTrash(): Promise<ProjectTrash> {
    return this.request("GET", "/api/project/trash");
  }

  trashProjectNode(id: string): Promise<ProjectNode> {
    return this.request("POST", `/api/project/nodes/${encodeURIComponent(id)}/trash`);
  }

  restoreProjectNode(id: string): Promise<ProjectNode> {
    return this.request("POST", `/api/project/nodes/${encodeURIComponent(id)}/restore`);
  }

  purgeProjectNode(id: string, confirmTitle: string): Promise<void> {
    return this.request("POST", `/api/project/nodes/${encodeURIComponent(id)}/purge`, {
      confirm_title: confirmTitle,
      mode: "keep_timeline",
    });
  }

  trashProject(id: string): Promise<Project> {
    return this.request("POST", `/api/project/projects/${encodeURIComponent(id)}/trash`);
  }

  restoreProject(id: string): Promise<Project> {
    return this.request("POST", `/api/project/projects/${encodeURIComponent(id)}/restore`);
  }

  purgeProject(id: string, confirmTitle: string, mode: ProjectPurgeMode): Promise<void> {
    return this.request("POST", `/api/project/projects/${encodeURIComponent(id)}/purge`, {
      confirm_title: confirmTitle,
      mode,
    });
  }

  projectChangeSets(by: { project?: string; taskId?: string }): Promise<{ change_sets: ProjectChangeSet[] }> {
    const q = by.taskId ? `task_id=${encodeURIComponent(by.taskId)}` : `project=${encodeURIComponent(by.project ?? "")}`;
    return this.request("GET", `/api/project/changesets?${q}`);
  }

  applyProjectChangeSet(id: string, selection?: number[]): Promise<ProjectChangeSet> {
    return this.request("POST", `/api/project/changesets/${encodeURIComponent(id)}/apply`, {
      selection: selection ?? null,
    });
  }

  refuseProjectChangeSet(id: string): Promise<ProjectChangeSet> {
    return this.request("POST", `/api/project/changesets/${encodeURIComponent(id)}/refuse`);
  }

  // --- PPH-04 04h and 04j: quiet hours, the person's time zone, export, templates ---------------

  projectNotificationSettings(): Promise<ProjectNotificationSettings> {
    return this.request("GET", "/api/project/notification-settings");
  }

  putProjectNotificationSettings(body: ProjectNotificationSettings): Promise<ProjectNotificationSettings> {
    return this.request("PUT", "/api/project/notification-settings", body);
  }

  /** The person's own time zone (an IANA name), or null to follow the hub's again. */
  putMyTimezone(timezone: string | null): Promise<Me> {
    return this.request("PUT", "/api/me/timezone", { timezone });
  }

  /** A picture of a report (`/api/reports/images/<id>`), loaded with the token: an <img> cannot
   *  send it. The caller revokes the object URL. */
  async reportImage(path: string): Promise<string> {
    if (!/^\/api\/reports\/images\/[0-9a-f-]{36}$/.test(path)) throw new ApiError(0, "bad_path", "not a report picture");
    const doFetch = this.fetchImpl ?? globalThis.fetch;
    const res = await doFetch(`${this.baseUrl}${path}`, { headers: { Authorization: `Bearer ${this.token}` } });
    if (res.status === 401) {
      this.onUnauthorized?.();
      throw new UnauthorizedError();
    }
    if (!res.ok) throw new ApiError(res.status, "not_found", "the picture could not be loaded");
    return URL.createObjectURL(await res.blob());
  }

  /** A project as one JSON document or a zip of Markdown files, with the name the hub gives it. */
  async downloadProjectExport(
    projectId: string,
    format: "json" | "markdown",
  ): Promise<{ blob: Blob; filename: string }> {
    const doFetch = this.fetchImpl ?? globalThis.fetch;
    let res: Response;
    try {
      res = await doFetch(
        `${this.baseUrl}/api/project/projects/${encodeURIComponent(projectId)}/export?format=${format}`,
        { headers: { Authorization: `Bearer ${this.token}` } },
      );
    } catch (err) {
      throw new ApiError(0, "network", err instanceof Error ? err.message : String(err));
    }
    if (res.status === 401) {
      this.onUnauthorized?.();
      throw new UnauthorizedError();
    }
    if (!res.ok) {
      let parsed: unknown;
      try {
        parsed = JSON.parse(await res.text());
      } catch {
        parsed = undefined;
      }
      throw errorFromBody(res.status, parsed, res.statusText || `HTTP ${res.status}`);
    }
    const named = /filename="([^"]+)"/.exec(res.headers.get("Content-Disposition") ?? "");
    return { blob: await res.blob(), filename: named?.[1] ?? `project.${format === "json" ? "json" : "zip"}` };
  }

  projectTemplates(): Promise<{ templates: ProjectTemplate[] }> {
    return this.request("GET", "/api/project/templates");
  }

  saveProjectTemplate(body: { project_id: string; name: string; description?: string }): Promise<ProjectTemplate> {
    return this.request("POST", "/api/project/templates", body);
  }

  deleteProjectTemplate(id: string): Promise<void> {
    return this.request("DELETE", `/api/project/templates/${encodeURIComponent(id)}`);
  }

  createProjectFromTemplate(id: string, body: { title: string; start_at?: string }): Promise<Project> {
    return this.request("POST", `/api/project/templates/${encodeURIComponent(id)}/projects`, body);
  }

  // --- the inbox (docs/PLATFORM.md, 3.7) -----------------------------------------------------------

  inbox(view: InboxView = "inbox", application?: string): Promise<InboxListing> {
    const q = new URLSearchParams({ view });
    if (application) q.set("application", application);
    return this.request<InboxListing>("GET", `/api/inbox?${q.toString()}`);
  }

  inboxCounts(): Promise<InboxCounts> {
    return this.request<InboxCounts>("GET", "/api/inbox/counts");
  }

  /** Read it: marked read and, unless pinned or still urgent, filed. */
  openInboxItem(id: string): Promise<InboxItem> {
    return this.request<InboxItem>("POST", `/api/inbox/${encodeURIComponent(id)}/open`);
  }

  /** "Als Recherche starten" in a chat: the research runs in this conversation (progress, then the
   *  chat's summary of the report). */
  startResearchInChat(conversationId: string, question: string, offerMessageId?: number): Promise<ChatMessage> {
    return this.request<ChatMessage>("POST", `/api/conversations/${encodeURIComponent(conversationId)}/research`, {
      question,
      offer_message_id: offerMessageId ?? null,
    });
  }

  /** Research › Recherchieren: a research of its own (api/research.py), its report in the shelf. */
  startResearch(question: string): Promise<ResearchRun> {
    return this.request<ResearchRun>("POST", "/api/research/runs", { question });
  }

  researchRuns(): Promise<{ runs: ResearchRun[] }> {
    return this.request<{ runs: ResearchRun[] }>("GET", "/api/research/runs");
  }

  stopResearch(id: string): Promise<{ stopped: boolean }> {
    return this.request<{ stopped: boolean }>("POST", `/api/research/runs/${encodeURIComponent(id)}/stop`);
  }

  /** Everything read and filed (of one application, if named); what waits for an action stays. */
  readAllInbox(application?: string): Promise<{ filed: number }> {
    const q = application ? `?application=${encodeURIComponent(application)}` : "";
    return this.request<{ filed: number }>("POST", `/api/inbox/read-all${q}`);
  }

  pinInboxItem(id: string, pinned: boolean): Promise<InboxItem> {
    return this.request<InboxItem>("POST", `/api/inbox/${encodeURIComponent(id)}/pin`, { pinned });
  }

  resolveInboxItem(id: string): Promise<InboxItem> {
    return this.request<InboxItem>("POST", `/api/inbox/${encodeURIComponent(id)}/resolve`);
  }

  itemMarks(id: string): Promise<{ marks: ItemMark[] }> {
    return this.request("GET", `/api/inbox/${encodeURIComponent(id)}/marks`);
  }

  addItemMark(id: string, kind: MarkKind, text: string, line?: number): Promise<ItemMark> {
    return this.request("POST", `/api/inbox/${encodeURIComponent(id)}/marks`, {
      kind,
      text,
      ...(line === undefined ? {} : { line }),
    });
  }

  resolveItemMark(id: string, resolved: boolean): Promise<ItemMark> {
    const q = resolved ? "" : "?resolved=false";
    return this.request("POST", `/api/inbox/marks/${encodeURIComponent(id)}/resolve${q}`);
  }

  deleteItemMark(id: string): Promise<void> {
    return this.request<void>("DELETE", `/api/inbox/marks/${encodeURIComponent(id)}`);
  }

  deleteInboxItem(id: string): Promise<void> {
    return this.request<void>("DELETE", `/api/inbox/${encodeURIComponent(id)}`);
  }

  /** Replace the last answer (or answer a message that got none). */
  regenerate(
    id: string,
    signal: AbortSignal,
    tools: ToolsMode = "auto",
    track?: StreamTrack,
  ): AsyncGenerator<ChatEvent> {
    const q = tools === "auto" ? "" : `?tools=${tools}`;
    return this.chatStream(
      `/api/conversations/${encodeURIComponent(id)}/regenerate${q}`,
      {},
      signal,
      track,
    );
  }

  /** Continue the last answer if it was cut off; the text is appended to that message. */
  continueAnswer(
    id: string,
    signal: AbortSignal,
    track?: StreamTrack,
  ): AsyncGenerator<ChatEvent> {
    return this.chatStream(
      `/api/conversations/${encodeURIComponent(id)}/continue`,
      {},
      signal,
      track,
    );
  }

  private async *chatStream(
    path: string,
    body: unknown,
    signal: AbortSignal,
    track?: StreamTrack,
  ): AsyncGenerator<ChatEvent> {
    const doFetch = this.fetchImpl ?? globalThis.fetch;
    let res: Response;
    try {
      res = await doFetch(this.baseUrl + path, {
        method: body === undefined ? "GET" : "POST",
        signal,
        headers: {
          Authorization: `Bearer ${this.token}`,
          Accept: "text/event-stream",
          ...(body === undefined ? {} : { "Content-Type": "application/json" }),
        },
        ...(body === undefined ? {} : { body: JSON.stringify(body) }),
      });
    } catch (err) {
      if (signal.aborted) return;
      throw new ApiError(
        0,
        "network",
        err instanceof Error ? err.message : String(err),
      );
    }
    if (res.status === 401) {
      this.onUnauthorized?.();
      throw new UnauthorizedError();
    }
    if (!res.ok || !res.body) {
      let parsed: unknown;
      try {
        parsed = await res.json();
      } catch {
        /* no JSON body */
      }
      throw errorFromBody(
        res.status,
        parsed,
        res.statusText || `HTTP ${res.status}`,
      );
    }
    const reader = res.body.getReader();
    const decoder = new TextDecoder();
    const parser = new SseParser();
    try {
      for (;;) {
        const { done, value } = await reader.read();
        if (done) return;
        for (const ev of parser.feed(decoder.decode(value, { stream: true }))) {
          const parsedEvent = toChatEvent(ev.event, ev.data);
          // the run numbers its events: remember how far this reader got, to attach from there
          if (track && ev.id !== undefined && /^\d+$/.test(ev.id))
            track.offset = Number(ev.id) + 1;
          if (parsedEvent) yield parsedEvent;
        }
      }
    } catch (err) {
      if (signal.aborted) return;
      throw new ApiError(
        0,
        "network",
        err instanceof Error ? err.message : String(err),
      );
    } finally {
      void reader.cancel().catch(() => undefined);
    }
  }

  listKeys(): Promise<ApiKey[]> {
    return this.request<ApiKey[]>("GET", "/api/keys");
  }

  /** Create a gateway API key; the response carries the key once. */
  createKey(name: string): Promise<ApiKey> {
    return this.request<ApiKey>("POST", "/api/keys", { name });
  }

  revokeKey(id: string): Promise<void> {
    return this.request<void>("DELETE", `/api/keys/${encodeURIComponent(id)}`);
  }

  /** The registered repositories (E6-01 S0). */
  devRepos(): Promise<DevRepoList> {
    return this.request<DevRepoList>("GET", "/api/dev/repos");
  }

  /** Register a repository; the hub reads it once and seals the token. */
  addDevRepo(body: AddDevRepo): Promise<DevRepo> {
    return this.request<DevRepo>("POST", "/api/dev/repos", body);
  }

  /** Change mode, test template or token; what is left out stays. */
  editDevRepo(id: string, body: EditDevRepo): Promise<DevRepo> {
    return this.request<DevRepo>(
      "PATCH",
      `/api/dev/repos/${encodeURIComponent(id)}`,
      body,
    );
  }

  refreshDevRepo(id: string): Promise<DevRepo> {
    return this.request<DevRepo>(
      "POST",
      `/api/dev/repos/${encodeURIComponent(id)}/refresh`,
    );
  }

  /** Step one of a push: the hub reads the workshop's diff and returns a proposal that shows it.
   *  Nothing leaves the workshop until executeProposal is clicked. */
  proposeDevPush(id: string, message: string): Promise<AgentProposal> {
    return this.request<AgentProposal>(
      "POST",
      `/api/dev/repos/${encodeURIComponent(id)}/push-proposal`,
      { message },
    );
  }

  /** D76: a new repository in the workshop of a dev node. */
  createLocalRepo(body: {
    name: string;
    node_id?: string;
    branch?: string;
    test_template?: string;
    title?: string;
    description?: string;
  }): Promise<DevRepo> {
    return this.request<DevRepo>("POST", "/api/dev/repos/local", body);
  }

  /** D76: a local clone of an external repository, to edit and test it in the workshop. */
  cloneLocalRepo(id: string, body: { name: string; node_id?: string }): Promise<DevRepo> {
    return this.request<DevRepo>("POST", `/api/dev/repos/${encodeURIComponent(id)}/clone`, body);
  }

  repoBranches(id: string): Promise<{ repo: string; default_branch: string; branches: DevBranch[] }> {
    return this.request("GET", `/api/dev/repos/${encodeURIComponent(id)}/branches`);
  }

  createRepoBranch(id: string, name: string, from?: string): Promise<Record<string, unknown>> {
    return this.request("POST", `/api/dev/repos/${encodeURIComponent(id)}/branches`, {
      name,
      ...(from ? { from } : {}),
    });
  }

  /** Take a branch into the default branch: the person's click (D76). */
  mergeRepoBranch(id: string, branch: string): Promise<Record<string, unknown>> {
    return this.request("POST", `/api/dev/repos/${encodeURIComponent(id)}/merge`, { branch });
  }

  publishRepoBranch(id: string, branch: string): Promise<Record<string, unknown>> {
    return this.request("POST", `/api/dev/repos/${encodeURIComponent(id)}/publish`, { branch });
  }

  pullRepoBranch(id: string, branch?: string): Promise<Record<string, unknown>> {
    return this.request("POST", `/api/dev/repos/${encodeURIComponent(id)}/pull`, branch ? { branch } : {});
  }

  setRepoUpstream(id: string, upstream: string | null): Promise<DevRepo> {
    return this.request<DevRepo>("PUT", `/api/dev/repos/${encodeURIComponent(id)}/upstream`, { upstream });
  }

  removeDevRepo(id: string): Promise<void> {
    return this.request<void>(
      "DELETE",
      `/api/dev/repos/${encodeURIComponent(id)}`,
    );
  }

  /** Store the GUI language of the signed-in user. */
  putLanguage(language: "en" | "de" | null): Promise<Me> {
    return this.request<Me>("PUT", "/api/me/language", { language });
  }

  getScheduling(): Promise<SchedulingView> {
    return this.request<SchedulingView>("GET", "/api/scheduling");
  }

  putScheduling(body: SchedulingUpdate): Promise<SchedulingView> {
    return this.request<SchedulingView>("PUT", "/api/scheduling", body);
  }

  getApplications(): Promise<ApplicationsView> {
    return this.request<ApplicationsView>("GET", "/api/scheduling/applications");
  }

  /** Replace the table of applications; a row left out is cleared. */
  putApplications(
    rows: Record<string, ApplicationRow>,
  ): Promise<ApplicationsView> {
    return this.request<ApplicationsView>(
      "PUT",
      "/api/scheduling/applications",
      { rows },
    );
  }

  getBlockPrograms(): Promise<BlockProgramInfo[]> {
    return this.request<BlockProgramInfo[]>("GET", "/api/blocks/programs");
  }

  /** What the block editor offers: ranks, suites, hooks, applications ... (admin). */
  getBlockCatalogue(): Promise<BlockCatalogue> {
    return this.request<BlockCatalogue>("GET", "/api/blocks/catalogue");
  }

  validateBlockProgram(
    program: Record<string, unknown>,
  ): Promise<{ ok: true } | { ok: false; error: string }> {
    return this.request("POST", "/api/blocks/validate", { program });
  }

  saveBlockProgram(name: string, program: Record<string, unknown>): Promise<unknown> {
    return this.request("PUT", `/api/blocks/programs/${encodeURIComponent(name)}`, { program });
  }

  deleteBlockProgram(name: string): Promise<unknown> {
    return this.request("DELETE", `/api/blocks/programs/${encodeURIComponent(name)}`);
  }

  startBlockRun(program: Record<string, unknown>, inputs: Record<string, string>): Promise<BlockRunView> {
    return this.request<BlockRunView>("POST", "/api/blocks/runs", { program, inputs });
  }

  getBlockRun(id: string): Promise<BlockRunView> {
    return this.request<BlockRunView>("GET", `/api/blocks/runs/${id}`);
  }

  stopBlockRun(id: string): Promise<unknown> {
    return this.request("POST", `/api/blocks/runs/${id}/stop`);
  }

  /** The models of every node and which of them programs may use (PRG-00 R1). */
  getPools(): Promise<PoolsView> {
    return this.request<PoolsView>("GET", "/api/scheduling/pools");
  }

  /** Admin: how the chat answers by effort (limits, second look, recall, notes ...). */
  getChatPolicy(): Promise<ChatPolicyView> {
    return this.request<ChatPolicyView>("GET", "/api/scheduling/chat-policy");
  }

  /** Admin: replace the chat policy (a key left out is the hub's own rule). */
  putChatPolicy(policy: Record<string, unknown>): Promise<ChatPolicyView> {
    return this.request<ChatPolicyView>("PUT", "/api/scheduling/chat-policy", { policy });
  }

  /** Admin: take a setting of the bench for a model and a kind of work, or give it back (null). */
  putModelProfile(model: string, fn: string, variant: string | null): Promise<PoolsView> {
    return this.request<PoolsView>("PUT", "/api/scheduling/model-profile", {
      model,
      function: fn,
      variant,
    });
  }

  /** Admin: what the models did in everyday use over the last days (SCH-06). */
  getMeasurements(days: number): Promise<Measurements> {
    return this.request<Measurements>("GET", `/api/scheduling/measurements?days=${days}`);
  }

  /** Admin: how often the machines loaded or changed a model, and what caused the loads (P2a). */
  getModelSwitches(days: number): Promise<ModelSwitches> {
    return this.request<ModelSwitches>("GET", `/api/scheduling/model-switches?days=${days}`);
  }

  /** Admin: the gateway picks the first node by the old order ("alt") or by the profile ("neu"). */
  setChoiceMode(mode: "alt" | "neu"): Promise<{ mode: string }> {
    return this.request<{ mode: string }>("PUT", "/api/scheduling/choice-mode", { mode });
  }

  /** Admin: how the second look judged the answers of each model in each application. */
  getQuality(days: number): Promise<QualityCounters> {
    return this.request<QualityCounters>("GET", `/api/scheduling/quality?days=${days}`);
  }

  /** Admin: the lowest share of green second looks an application should see (null = none). */
  putQualityFloor(
    application: string,
    floor: number | null,
  ): Promise<{ floors: Record<string, number> }> {
    return this.request("PUT", "/api/scheduling/quality-floor", { application, floor });
  }

  /** Admin: the house model of each node (left out = none) and the background workers. */
  putHouse(house: Record<string, string>, workers: string[], order?: string[]): Promise<PoolsView> {
    const body = order ? { house, workers, order } : { house, workers };
    return this.request<PoolsView>("PUT", "/api/scheduling/house", body);
  }

  /** A model reads this answer now (the person asked); the light is yellow until it is done. */
  reviewMessage(messageId: number): Promise<{ started: boolean }> {
    return this.request("POST", `/api/messages/${messageId}/review`);
  }

  /** Admin: how many models a node may hold at once; a node left out has no limit. */
  putMaxLoaded(maxLoaded: Record<string, number>): Promise<PoolsView> {
    return this.request<PoolsView>("PUT", "/api/scheduling/max-loaded", { max_loaded: maxLoaded });
  }

  /** Admin: prove what a model can do on one node (a tool call and an embedding). */
  runModelProbes(model: string, node: string): Promise<{ results: Record<string, { ok: boolean }> }> {
    return this.request("POST", "/api/scheduling/model-probes", { model, node });
  }

  /** Admin: kinds set by hand, model -> kinds (the others stay detected by name). */
  putModelKinds(modelKinds: Record<string, string[]>): Promise<PoolsView> {
    return this.request<PoolsView>("PUT", "/api/scheduling/model-kinds", { model_kinds: modelKinds });
  }

  /** Replace the pools: node -> available models; a node left out offers every model. */
  putPools(pools: Record<string, string[]>): Promise<PoolsView> {
    const body: Record<string, { available: string[] }> = {};
    for (const [node, available] of Object.entries(pools)) body[node] = { available };
    return this.request<PoolsView>("PUT", "/api/scheduling/pools", { pools: body });
  }

  benchSuites(): Promise<BenchSuite[]> {
    return this.request<BenchSuite[]>("GET", "/api/bench/suites");
  }

  benchRatings(): Promise<BenchRating[]> {
    return this.request<BenchRating[]>("GET", "/api/bench/ratings");
  }

  /** A better default model of the pool, if the ratings show one; it changes nothing by itself. */
  benchSuggestion(): Promise<{ suggestion: BenchSuggestion | null }> {
    return this.request<{ suggestion: BenchSuggestion | null }>("GET", "/api/bench/suggestion");
  }

  benchRuns(): Promise<BenchRun[]> {
    return this.request<BenchRun[]>("GET", "/api/bench/runs");
  }

  /** Admin: rate a model of the pool on a node with the chosen suites. */
  startBench(model: string, node: string, suites: string[], variant = ""): Promise<BenchRun> {
    return this.request<BenchRun>("POST", "/api/bench/runs", { model, node, suites, variant });
  }

  benchVariants(): Promise<BenchVariant[]> {
    return this.request<BenchVariant[]>("GET", "/api/bench/variants");
  }

  stopBench(runId: string): Promise<{ stopped: boolean }> {
    return this.request<{ stopped: boolean }>("DELETE", `/api/bench/runs/${encodeURIComponent(runId)}`);
  }

  /** Back to the environment's order and background nodes; nothing kept free, no pause. */
  resetScheduling(): Promise<SchedulingView> {
    return this.request<SchedulingView>("DELETE", "/api/scheduling");
  }

  /** Pause all background work for `minutes`; 0 resumes it. */
  pauseBackground(minutes: number): Promise<SchedulingView> {
    return this.request<SchedulingView>("POST", "/api/scheduling/pause", {
      minutes,
    });
  }

  /** Start one background job now; 409 with a reason when paused or no node is free. */
  runBackgroundTask(task: BackgroundTask): Promise<{ started: boolean }> {
    return this.request("POST", `/api/scheduling/run/${task}`);
  }

  getActivity(): Promise<ActivityView> {
    return this.request<ActivityView>("GET", "/api/activity");
  }

  nodeMetrics(): Promise<NodeMetrics> {
    return this.request<NodeMetrics>("GET", "/api/nodes/metrics");
  }

  listNodes(): Promise<NodeSummary[]> {
    return this.request<NodeSummary[]>("GET", "/api/nodes");
  }

  createModelTransfer(body: ModelTransferCreate): Promise<ModelTransfer> {
    return this.request<ModelTransfer>("POST", "/api/model-transfers", body);
  }

  listModelTransfers(nodeId?: string): Promise<ModelTransfer[]> {
    const query = nodeId ? `?node_id=${encodeURIComponent(nodeId)}` : "";
    return this.request<{ transfers: ModelTransfer[] }>(
      "GET",
      `/api/model-transfers${query}`,
    ).then((body) => body.transfers);
  }

  getModelTransfer(id: string): Promise<ModelTransfer> {
    return this.request<ModelTransfer>(
      "GET",
      `/api/model-transfers/${encodeURIComponent(id)}`,
    );
  }

  cancelModelTransfer(id: string): Promise<ModelTransfer> {
    return this.request<ModelTransfer>(
      "POST",
      `/api/model-transfers/${encodeURIComponent(id)}/cancel`,
    );
  }

  getNode(id: string): Promise<NodeDetail> {
    return this.request<NodeDetail>(
      "GET",
      `/api/nodes/${encodeURIComponent(id)}`,
    );
  }

  /**
   * Run a whitelisted node action. HTTP-level failures (404/409/504/network)
   * are mapped to `{ok: false, error}` so callers handle one shape; only 401
   * is thrown (as UnauthorizedError).
   */
  async runAction(
    id: string,
    name: string,
    args: Record<string, unknown>,
  ): Promise<ActionResult> {
    try {
      const res = await this.request<ActionResult>(
        "POST",
        `/api/nodes/${encodeURIComponent(id)}/actions`,
        { name, args },
      );
      return (
        res ?? {
          ok: false,
          error: { code: "bad_response", message: "empty response" },
        }
      );
    } catch (err) {
      if (err instanceof ApiError && !(err instanceof UnauthorizedError)) {
        return { ok: false, error: { code: err.code, message: err.message, status: err.status } };
      }
      throw err;
    }
  }

  /** Rename a node (admin): living settings follow, the old name still leads to the node. */
  renameNode(
    id: string,
    name: string,
  ): Promise<{ id: string; from: string; to: string; rewritten: Record<string, number> }> {
    return this.request("POST", `/api/nodes/${encodeURIComponent(id)}/rename`, { name });
  }

  /** Wake an offline node with Wake-on-LAN (E2-05 E); the hub sends the magic packet. */
  wakeNode(
    id: string,
  ): Promise<{ sent: number; broadcast: string; port: number }> {
    return this.request("POST", `/api/nodes/${encodeURIComponent(id)}/wake`);
  }

  /** Replace a node's config; the hub validates it (400 invalid_config) and pushes it. */
  putNodeConfig(id: string, config: unknown): Promise<NodeConfigResponse> {
    return this.request<NodeConfigResponse>(
      "PUT",
      `/api/nodes/${encodeURIComponent(id)}/config`,
      config,
    );
  }

  /** Keep a model loaded ("Automatisch bereithalten"); replaces its resident entry. */
  putResident(
    id: string,
    model: string,
    entry: ResidentUpdate,
  ): Promise<ResidentResponse> {
    return this.request<ResidentResponse>(
      "PUT",
      this.residentPath(id, model),
      entry,
    );
  }

  /** Stop keeping a model loaded (idempotent). */
  deleteResident(id: string, model: string): Promise<ResidentResponse> {
    return this.request<ResidentResponse>(
      "DELETE",
      this.residentPath(id, model),
    );
  }

  private residentPath(id: string, model: string): string {
    return `/api/nodes/${encodeURIComponent(id)}/residents/${encodeURIComponent(model)}`;
  }

  // --- the agent (E3-01a) ----------------------------------------------------------------

  agentTasks(): Promise<AgentTask[]> {
    return this.request<AgentTask[]>("GET", "/api/agent/tasks");
  }

  /** Start a task; refusals (409 no_capabilities, 429 too_many_tasks) are thrown as ApiError. */
  startAgentTask(
    prompt: string,
    model?: string,
    coordinator?: boolean,
  ): Promise<AgentTask> {
    return this.request<AgentTask>("POST", "/api/agent/tasks", {
      prompt,
      ...(model ? { model } : {}),
      ...(coordinator ? { coordinator: true } : {}),
    });
  }

  /** The proposals of the signed-in user that still wait for the click. */
  pendingProposals(): Promise<AgentProposal[]> {
    return this.request<AgentProposal[]>("GET", "/api/agent/proposals/pending");
  }

  agentTask(id: string): Promise<AgentTaskDetail> {
    return this.request<AgentTaskDetail>(
      "GET",
      `/api/agent/tasks/${encodeURIComponent(id)}`,
    );
  }

  cancelAgentTask(id: string): Promise<AgentTask> {
    return this.request<AgentTask>(
      "POST",
      `/api/agent/tasks/${encodeURIComponent(id)}/cancel`,
    );
  }

  continueAgentTask(id: string): Promise<AgentTask> {
    return this.request<AgentTask>(
      "POST",
      `/api/agent/tasks/${encodeURIComponent(id)}/continue`,
    );
  }

  /** The one click (E3-01b): runs this proposal, once. Never an API key's job. */
  executeProposal(id: string): Promise<AgentProposal> {
    return this.request<AgentProposal>(
      "POST",
      `/api/agent/proposals/${encodeURIComponent(id)}/execute`,
    );
  }

  /** The proposals one agent task made: the report card of a chat shows them with their buttons. */
  taskProposals(taskId: string): Promise<AgentProposal[]> {
    return this.request<AgentProposal[]>(
      "GET",
      `/api/agent/tasks/${encodeURIComponent(taskId)}/proposals`,
    );
  }

  /** One proposal as it is now (the inventory's confirmation card reloads it after a click). */
  proposal(id: string): Promise<AgentProposal> {
    return this.request<AgentProposal>(
      "GET",
      `/api/agent/proposals/${encodeURIComponent(id)}`,
    );
  }

  /**
   * The person asks for one change themselves (a restart button in the inventory). Nothing runs:
   * the answer is a pending proposal with its exact effect, and executeProposal is the click.
   */
  manualProposal(
    nodeId: string,
    capability: string,
    input: Record<string, unknown>,
  ): Promise<AgentProposal> {
    return this.request<AgentProposal>("POST", "/api/agent/proposals/manual", {
      node_id: nodeId,
      capability,
      input,
    });
  }

  refuseProposal(id: string): Promise<AgentProposal> {
    return this.request<AgentProposal>(
      "POST",
      `/api/agent/proposals/${encodeURIComponent(id)}/refuse`,
    );
  }

  /** How the agent talks to each model: native tool calls, the JSON protocol or auto. */
  agentSettings(): Promise<AgentSettings> {
    return this.request<AgentSettings>("GET", "/api/agent/settings");
  }

  saveAgentToolModes(
    toolModes: Record<string, string>,
  ): Promise<AgentSettings> {
    return this.request<AgentSettings>("PUT", "/api/agent/settings", {
      tool_modes: toolModes,
    });
  }

  // --- knowledge base (E2-03) -----------------------------------------------------------------

  knowledgeSources(): Promise<KnowledgeSource[]> {
    return this.request<KnowledgeSource[]>("GET", "/api/knowledge/sources");
  }

  knowledgeSource(id: string): Promise<KnowledgeSource> {
    return this.request<KnowledgeSource>(
      "GET",
      `/api/knowledge/sources/${encodeURIComponent(id)}`,
    );
  }

  /** Admin. The node must release the directory (`tools.docs_roots`); an empty root means its
   *  only released directory. */
  addKnowledgeSource(body: {
    name: string;
    node: string;
    root: string;
    visibility: "shared" | "private";
  }): Promise<KnowledgeSource> {
    return this.request<KnowledgeSource>(
      "POST",
      "/api/knowledge/sources",
      body,
    );
  }

  /** Admin. One Markdown/text file into an upload source (created when `source_id` is empty). */
  uploadKnowledgeFile(body: {
    source_id?: string;
    name?: string;
    visibility?: "shared" | "private";
    filename: string;
    text: string;
  }): Promise<KnowledgeSource> {
    return this.request<KnowledgeSource>(
      "POST",
      "/api/knowledge/uploads",
      body,
    );
  }

  reindexKnowledgeSource(id: string): Promise<KnowledgeSource> {
    return this.request<KnowledgeSource>(
      "POST",
      `/api/knowledge/sources/${encodeURIComponent(id)}/reindex`,
    );
  }

  deleteKnowledgeSource(id: string): Promise<void> {
    return this.request<void>(
      "DELETE",
      `/api/knowledge/sources/${encodeURIComponent(id)}`,
    );
  }

  knowledgeSearch(q: string): Promise<{ query: string; hits: KnowledgeHit[] }> {
    return this.request(
      "GET",
      `/api/knowledge/search?${new URLSearchParams({ q }).toString()}`,
    );
  }

  knowledgeStatus(): Promise<KnowledgeStatus> {
    return this.request<KnowledgeStatus>("GET", "/api/knowledge/status");
  }

  // --- inventory: what runs on which node (E3-10) ---------------------------------------------

  inventory(): Promise<InventoryList> {
    return this.request<InventoryList>("GET", "/api/inventory");
  }

  refreshInventory(nodeId: string): Promise<InventoryNode> {
    return this.request<InventoryNode>(
      "POST",
      `/api/inventory/nodes/${encodeURIComponent(nodeId)}/refresh`,
    );
  }

  // --- identity: who is in which group, which group opens which app (E3-12) --------------------

  identity(refresh = false): Promise<IdentityView> {
    return this.request<IdentityView>(
      "GET",
      `/api/identity${refresh ? "?refresh=true" : ""}`,
    );
  }

  setIdentityAppGroups(
    appGroups: Record<string, string[]>,
  ): Promise<{ app_groups: Record<string, string[]> }> {
    return this.request("PUT", "/api/identity/app-groups", {
      app_groups: appGroups,
    });
  }

  // --- tool nodes (E2-04) ----------------------------------------------------------------

  nodeCapabilities(id: string): Promise<NodeCapabilities> {
    return this.request<NodeCapabilities>(
      "GET",
      `/api/nodes/${encodeURIComponent(id)}/capabilities`,
    );
  }

  /**
   * Run a read-only capability with the rights of the signed-in user. Refusals (not granted, not
   * read-only, bad input, node offline …) come back as `{ok: false, error}` like runAction; only
   * 401 is thrown.
   */
  async runCapability(
    id: string,
    capability: string,
    input: Record<string, unknown>,
  ): Promise<ToolResult> {
    try {
      const res = await this.request<ToolResult>(
        "POST",
        `/api/nodes/${encodeURIComponent(id)}/capabilities/${encodeURIComponent(capability)}`,
        { input },
      );
      return (
        res ?? {
          ok: false,
          error: { code: "bad_response", message: "empty response" },
        }
      );
    } catch (err) {
      if (err instanceof ApiError && !(err instanceof UnauthorizedError)) {
        return { ok: false, error: { code: err.code, message: err.message } };
      }
      throw err;
    }
  }

  toolCalls(nodeId?: string, limit = 25): Promise<ToolCallRow[]> {
    const q = new URLSearchParams({ limit: String(limit) });
    if (nodeId) q.set("node_id", nodeId);
    return this.request<ToolCallRow[]>(
      "GET",
      `/api/tool-calls?${q.toString()}`,
    );
  }

  nodeGrants(id: string): Promise<GrantRow[]> {
    return this.request<GrantRow[]>(
      "GET",
      `/api/nodes/${encodeURIComponent(id)}/grants`,
    );
  }

  /** Replace what one user may run on a node (capability names, or "*" for all read-only). */
  putGrants(
    id: string,
    userId: string,
    capabilities: string[],
  ): Promise<{ user_id: string; capabilities: string[] }> {
    return this.request(
      "PUT",
      `/api/nodes/${encodeURIComponent(id)}/grants/${encodeURIComponent(userId)}`,
      { capabilities },
    );
  }

  listUsers(): Promise<UserRow[]> {
    return this.request<UserRow[]>("GET", "/api/users");
  }

  createPairingCode(): Promise<PairingCode> {
    return this.request<PairingCode>("POST", "/api/pairing-codes");
  }

  // --- the interface as data (docs/DESKTOP.md, D60, D70) ---------------------------------------------

  uiBootstrap(): Promise<UiBootstrap> {
    return this.request<UiBootstrap>("GET", "/api/ui/bootstrap");
  }

  putUiPrefs(patch: Partial<UiPrefs>): Promise<UiPrefs> {
    return this.request<UiPrefs>("PUT", "/api/ui/prefs", patch);
  }

  /** The custom themes of this hub (docs/THEMES.md). */
  uiThemes(): Promise<ThemeManifest[]> {
    return this.request("GET", "/api/ui/themes");
  }

  putTheme(key: string, theme: ThemeManifest): Promise<ThemeManifest> {
    return this.request("PUT", `/api/ui/themes/${encodeURIComponent(key)}`, theme);
  }

  /** The theme workshop (hub api/theme_workshop.py): one turn is one patch the hub checked. */
  workshopTurn(body: {
    theme: ThemeManifest;
    message: string;
    history: { role: "person" | "workshop"; text: string }[];
    requirements: string[];
  }): Promise<import("../desktop/workshop").WorkshopTurnResult> {
    return this.request("POST", "/api/ui/workshop/turn", body);
  }

  /** Without a model: a dark theme's own light backgrounds and dark text become the variables. */
  workshopFixContrast(theme: ThemeManifest): Promise<{ theme: ThemeManifest; changed: string[]; lint: string[] }> {
    return this.request("POST", "/api/ui/workshop/fix-contrast", { theme });
  }

  workshopStart(q: { base?: string; key?: string }): Promise<{ theme: ThemeManifest; lint: string[]; from: string | null }> {
    const params = new URLSearchParams();
    if (q.base) params.set("base", q.base);
    if (q.key) params.set("key", q.key);
    const qs = params.toString();
    return this.request("GET", `/api/ui/workshop/start${qs ? `?${qs}` : ""}`);
  }

  /** The person's save: stores the theme (admin) and files it below the task as an iteration. */
  workshopSave(body: {
    theme: ThemeManifest;
    task_id?: string | null;
    based_on?: string | null;
    steps: { message: string; reply: string }[];
    store: boolean;
  }): Promise<{ key: string; stored: boolean; result: string | null; number?: string; iteration: number | null }> {
    return this.request("POST", "/api/ui/workshop/save", body);
  }

  deleteTheme(key: string): Promise<void> {
    return this.request("DELETE", `/api/ui/themes/${encodeURIComponent(key)}`);
  }

  /** `language` (de/en): the language of the contract; without it, the person's stored one. */
  uiWorkspaceAgent(
    key: string,
    language?: "de" | "en",
  ): Promise<
    UiAgentDefaults & {
      workspace: string;
      denied: string[];
      planned: string[];
      contract?: import("../layout/types").AgentContractView | null;
    }
  > {
    const query = language ? `?language=${language}` : "";
    return this.request("GET", `/api/ui/workspaces/${encodeURIComponent(key)}/agent${query}`);
  }

  uiTemplates(): Promise<{ templates: UiTemplateView[] }> {
    return this.request("GET", "/api/ui/templates");
  }

  putUiTemplate(
    key: string,
    body: {
      title: string;
      icon?: string;
      position?: number;
      modules: string[];
      agent?: Partial<UiAgentDefaults>;
      requires_role?: "admin" | null;
    },
  ): Promise<UiTemplateView> {
    return this.request("PUT", `/api/ui/templates/${encodeURIComponent(key)}`, body);
  }

  duplicateUiTemplate(key: string, newKey: string, title?: string): Promise<UiTemplateView> {
    return this.request("POST", `/api/ui/templates/${encodeURIComponent(key)}/duplicate`, {
      key: newKey,
      ...(title ? { title } : {}),
    });
  }

  deleteUiTemplate(key: string): Promise<{ deleted: string }> {
    return this.request("DELETE", `/api/ui/templates/${encodeURIComponent(key)}`);
  }

  uiProfiles(): Promise<{ profiles: UiProfileView[] }> {
    return this.request("GET", "/api/ui/profiles");
  }

  putUiProfile(
    name: string,
    body: Pick<UiProfileView, "description" | "grants"> & { default?: boolean },
  ): Promise<UiProfileView> {
    return this.request("PUT", `/api/ui/profiles/${encodeURIComponent(name)}`, body);
  }

  deleteUiProfile(name: string): Promise<{ deleted: string }> {
    return this.request("DELETE", `/api/ui/profiles/${encodeURIComponent(name)}`);
  }

  uiUsers(): Promise<{ users: UiUserView[] }> {
    return this.request("GET", "/api/ui/users");
  }

  putUiUserProfile(
    userId: string,
    profile: string | null,
  ): Promise<{ user: string; profile: string | null }> {
    return this.request("PUT", `/api/ui/users/${encodeURIComponent(userId)}/profile`, { profile });
  }
}

/** A step of a server check as the chat shows it: the model's plan for a call, the call with its
 *  result, a note. */
export interface CheckStepEvent {
  seq: number;
  kind: "plan" | "call" | "note";
  text?: string;
  capability?: string;
  node?: string;
  input?: Record<string, unknown>;
  ok?: boolean;
  ref?: string | null;
  code?: string | null;
}

/** How a server check ended, with the evidence it collected. */
export interface CheckDone {
  task_id: string | null;
  status: string;
  stop_reason: string | null;
  error: string | null;
  reason: string | null;
  calls: number;
  evidence: {
    ref: string;
    tag: string;
    node?: string;
    capability?: string;
    url?: string;
    bytes: number;
  }[];
}

/** One event of a streamed chat answer (the hub's `meta`, `delta`, `done`, `error`). */
/** How far a reader of an answer got: the number of the next event, to attach again from there. */
export interface StreamTrack {
  offset: number;
}

export type ChatEvent =
  | {
      type: "meta";
      conversation_id: string;
      user_message: ChatMessage | null;
      /** Id of the truncated answer this stream continues, else null. */
      continues: number | null;
      /** E3-07: the message the answer is written into: what "stop" and "attach" name. */
      message_id?: number | null;
      /** Memory notes the model was given for this answer. */
      memory: MemoryRef[];
      model: string;
      node: string;
      fallback_from: string | null;
      fallback_state: string | null;
      context: {
        length: number;
        estimated_prompt_tokens: number;
        trimmed: number;
      };
      /** A server check runs before the answer: the user asked for it or the question looked like one. */
      checking?: "forced" | "detected" | "decided";
      /** The assistant is held to these nodes / tool groups in this conversation. */
      tool_scope?: ToolScope;
      /** The internet switch of this answer and, when on, the node the hub searches from. */
      internet?: boolean;
      internet_node?: string;
      /** The quality level of this answer and, at "thorough", the model that looks at it again. */
      quality?: Quality;
      verify_model?: string;
      /** The check runs on a slow stand-in machine and is held to this many tool calls. */
      check_max_calls?: number;
      /** The question may be about the user's servers: answered plainly, a check is offered. */
      check_offer?: boolean;
    }
  | { type: "delta"; text: string }
  /** The model called the check function: what was shown so far was a lead-in, drop it. */
  | { type: "discard" }
  /** E7-01: an item of the activity chain appears (`add`) or changes (`update`, whole item). */
  | { type: "activity"; op: "add" | "update"; item: ActivityItem }
  /** The thorough level: the big model is reading the answer against the evidence. */
  | { type: "verifying"; model: string }
  | ({
      type: "verification";
      message_id: number;
      available: boolean;
    } & Partial<Verification>)
  /** The server check (E3-01d): it starts, reports its steps, and ends. */
  | { type: "tools_start"; task_id: string }
  | ({ type: "tool_step" } & CheckStepEvent)
  | { type: "tools_wait"; task_id: string }
  | ({ type: "tools_done" } & CheckDone)
  /** E3-07 D3: the hub's confirmation bubble ("Schau ich mir an."), the line that shows which tool
   *  runs (it goes when the result is there) and the report card built from the check. */
  | { type: "bubble"; message: ChatMessage }
  | { type: "progress"; message_id: number; text: string }
  | { type: "progress_done"; message_id: number }
  | { type: "report"; message: ChatMessage }
  /** The model proposed a hand-over to another program: the dossier waits for the person's click. */
  | ({ type: "handover" } & Handover)
  /**
   * The node says the answer stands still (`paused`: the person at its machine came first,
   * `running`: it goes on) or that it unloaded the model (`interrupted`, `done` follows).
   */
  | {
      type: "status";
      state: "paused" | "running" | "interrupted";
      reasons?: string[];
      node: string;
    }
  | { type: "done"; message: ChatMessage }
  /** E3-07: the run ended by a "stop" call (or the hub going down: `interrupted`); the message
   *  is what was stored, null if nothing was said. */
  | { type: "stopped"; message: ChatMessage | null; interrupted: boolean }
  /** E3-07: attached to an answer that is over and no longer held in memory: the stored message. */
  | { type: "snapshot"; conversation_id: string; message: ChatMessage }
  | {
      type: "error";
      code: string;
      message: string;
      message_row?: ChatMessage | null;
      retry?: boolean;
      retry_after?: number;
    }
  /** After `done`: what the assistant wrote into its memory because of this exchange. */
  | {
      type: "memory";
      message_id: number;
      remembered: {
        id: string;
        text: string;
        evidence: string;
        /** Earlier notes this one contradicts (both are `disputed` now). */
        contradicts?: { id: string; text: string }[];
      }[];
      confirmed: { id: string; text: string; evidence: string }[];
      /** Statements that replaced or withdrew earlier notes (undoable). */
      corrected?: {
        id: string;
        text: string;
        evidence: string;
        replaced: { id: string; text: string }[];
        withdrawn: boolean;
      }[];
      /** Notes the user called wrong or a test, withdrawn without a replacement. */
      retracted?: { id: string; text: string }[];
      blocked: number;
      /** E3-07 D4: the user asked to note something (or the answer claimed it) and nothing was
       *  noted: why (secret | no_statement | too_long | nothing_found | memory_off | pending |
       *  failed). */
      not_noted?: string;
      /** Clarification questions opened by this exchange. */
      questions?: number;
    };

function toChatEvent(event: string, data: string): ChatEvent | null {
  let obj: Record<string, unknown>;
  try {
    obj = JSON.parse(data) as Record<string, unknown>;
  } catch {
    return null;
  }
  if (
    event === "meta" ||
    event === "delta" ||
    event === "discard" ||
    event === "activity" ||
    event === "verifying" ||
    event === "verification" ||
    event === "status" ||
    event === "done" ||
    event === "stopped" ||
    event === "snapshot" ||
    event === "error" ||
    event === "memory" ||
    event === "tools_start" ||
    event === "tool_step" ||
    event === "tools_wait" ||
    event === "tools_done" ||
    event === "bubble" ||
    event === "progress" ||
    event === "progress_done" ||
    event === "report" ||
    event === "handover"
  ) {
    return { type: event, ...obj } as ChatEvent;
  }
  return null;
}
