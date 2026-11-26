// What each view draws (docs/DESKTOP.md): the page component is mounted inside a program and gets
// what `App` gives it (the API client, the person, the nodes, the agent tick). The metadata (titles,
// who may use it) is in `meta.ts`.
import {
  useEffect,
  useState,
  type Dispatch,
  type ReactNode,
} from "react";
import type { CSSProperties } from "react";
import { useTranslation } from "react-i18next";
import { applicationAccent } from "../../chat/applications";
import type { ApiClient } from "../../api/client";
import { TasksPage } from "../../agent/TasksPage";
import { CarePage, SourcesPage } from "../../care/CarePage";
import { SettingsApp } from "../../settings/SettingsApp";
import { ApiAccess } from "../../components/ApiAccess";
import { DevRepos } from "../../components/DevRepos";
import { DevWorkshop } from "../../components/DevWorkshop";
import { FilesPage } from "../../files/FilesPage";
import { ModelCatalogue } from "../../components/ModelCatalogue";
import { ModelDetail } from "../../components/ModelDetail";
import { NodeDetailView } from "../../components/NodeDetail";
import { Overview } from "../../components/Overview";
import { IdentityPage } from "../../identity/IdentityPage";
import { InstructionsPage } from "../../instructions/InstructionsPage";
import { InventoryPage } from "../../inventory/InventoryPage";
import { KnowledgePage } from "../../knowledge/KnowledgePage";
import { ProjectPage } from "../../project/ProjectPage";
import { ResearchPage } from "../../research/ResearchPage";
import { ArchivePage } from "../../archive/ArchivePage";
import { DocumentPage } from "../../library/DocumentPage";
import { ShelfPage } from "../../library/ShelfPage";
import { WikiPage } from "../../wiki/WikiPage";
import { SheetsPage } from "../../sheets/SheetsPage";
import { ProgramSettings } from "../../desktop/ProgramSettings";
import { MemoryPage } from "../../memory/MemoryPage";
import { BenchPanel } from "../../scheduling/BenchPanel";
import { MeasurementsPanel } from "../../scheduling/MeasurementsPanel";
import { ApplicationsPage } from "../../scheduling/ApplicationsPage";
import { TaskKindsPage } from "../../scheduling/TaskKindsPage";
import { ModelSwitchesPanel } from "../../scheduling/ModelSwitchesPanel";
import { QualityPanel } from "../../scheduling/QualityPanel";
import { PoolsPanel } from "../../scheduling/PoolsPanel";
import { ProgramsPage } from "../../blocks/ProgramsPage";
import { ParamsPage } from "../../params/ParamsPage";
import { ReleasesPage } from "../../releases/ReleasesPage";
import { SchedulingPage } from "../../scheduling/SchedulingPage";
import { HandoversPage } from "../../handover/HandoversPage";
import { ThemesPage } from "../../desktop/ThemesPage";
import { ThemeWorkshop } from "../../desktop/ThemeWorkshop";
import { IncognitoPage } from "../../incognito/IncognitoPage";
import { HomePage } from "../../home/HomePage";
import { InboxPage } from "../../inbox/InboxPage";
import type { Action } from "../../state/store";
import { TalkPage } from "../../talk/TalkPage";
import type { Me, NodeSummary, ToolScope } from "../../types";
import type { PropValue, UiBootstrap } from "../types";
import { requestOpen } from "../intent";
import { Tabbed, type TabSpec } from "./Tabbed";
import { WorkspacesAdmin } from "./WorkspacesAdmin";
import { ToolModes } from "../../agent/ToolModes";

function Loading() {
  const { t } = useTranslation();
  return <p className="muted">{t("app.loading")}</p>;
}

/** What `App` knows and the pages need. */
export interface ModuleCtx {
  api: ApiClient;
  me: Me | null;
  isAdmin: boolean;
  nodes: NodeSummary[];
  nodesLoaded: boolean;
  nodesError: string | null;
  refreshNodes: () => void;
  /** Bumped when the agent made progress. */
  tick: number;
  /** Bumped when the hub says the inbox changed. */
  inboxTick: number;
  /** The node page tells the node list what it loaded. */
  dispatch: Dispatch<Action>;
}

/** What a window tells its module about itself. */
export interface WindowCtx {
  workspace: string;
  boot: UiBootstrap | null;
}

type Props = Record<string, PropValue>;

const str = (v: PropValue | undefined): string | undefined =>
  typeof v === "string" && v ? v : undefined;

/** The conversation of a program (docs/DESKTOP.md): a conversation started here begins with what
 *  the program and the person's profile allow and belongs to the program (the application). One
 *  history, no pickers, no timings; a link to one conversation opens that one. */
function ChatWindow({
  api,
  workspace,
  id,
}: {
  api: ApiClient;
  workspace: string;
  id?: string;
}) {
  const { t } = useTranslation();
  // undefined until the hub has said what the workspace allows: nothing is started without it
  const [scope, setScope] = useState<ToolScope | null | undefined>(undefined);
  useEffect(() => {
    let alive = true;
    api.uiWorkspaceAgent(workspace).then(
      (agent) => alive && setScope(agent.tool_scope),
      () => alive && setScope(null),
    );
    return () => {
      alive = false;
    };
  }, [api, workspace]);
  if (scope === undefined) return <Loading />;
  return (
    <div
      className="chat-window"
      data-app={workspace}
      style={{ "--chat-accent": applicationAccent(workspace) } as CSSProperties}
    >
      <div className="chat-window-bar">
        <button
          type="button"
          className="linklike"
          onClick={() =>
            requestOpen({
              module: "page.handovers",
              props: { from: workspace },
            })
          }
        >
          {t("chat.handover")}
        </button>
      </div>
      <TalkPage
        api={api}
        newChatScope={scope}
        application={workspace}
        conversationId={id}
      />
    </div>
  );
}

/** The tabs of a view the person may use (a page whose module is not allowed is left out). */
function offered(win: WindowCtx, tabs: TabSpec[]): TabSpec[] {
  const allowed = new Map(
    (win.boot?.modules ?? []).map((m) => [m.id, m.allowed]),
  );
  return tabs.filter((x) => allowed.get(x.module) ?? true);
}

export function renderModule(
  id: string,
  ctx: ModuleCtx,
  props: Props,
  win: WindowCtx,
): ReactNode {
  const { api, me, nodes } = ctx;
  switch (id) {
    case "page.home":
      return <HomePage workspaces={win.boot?.workspaces ?? []} modules={win.boot?.modules ?? []} self={win.workspace} />;
    case "page.inbox":
      return <InboxPage api={api} tick={ctx.inboxTick} />;
    case "page.incognito":
      return <IncognitoPage api={api} />;
    case "page.themes":
      return <ThemesPage api={api} />;
    case "page.theme-workshop":
      return (
        <ThemeWorkshop
          key={`${str(props.task) ?? ""}|${str(props.base) ?? ""}|${str(props.key) ?? ""}`}
          api={api}
          task={str(props.task)}
          base={str(props.base)}
          themeKey={str(props.key)}
        />
      );
    case "page.applications":
      // the applications with what belongs to them: the kinds of work they take, who may use
      // them (the templates and the profiles that grant them; phase 4, from Access)
      return (
        <Tabbed
          initial={str(props.tab)}
          tabs={offered(win, [
            {
              key: "applications",
              module: "page.applications",
              render: () => <ApplicationsPage api={api} />,
            },
            {
              key: "task_kinds",
              module: "page.task_kinds",
              render: () => <TaskKindsPage api={api} />,
            },
            {
              key: "rights",
              module: "layout.workspaces",
              labelKey: "layout.tab.rights",
              render: () => <WorkspacesAdmin api={api} boot={win.boot} part="apps" />,
            },
          ])}
        />
      );
    case "page.task_kinds":
      return <TaskKindsPage api={api} />;
    case "page.handovers":
      return (
        <HandoversPage
          api={api}
          from={str(props.from)}
          incognito={str(props.incognito)}
          id={str(props.id)}
          tick={ctx.inboxTick}
        />
      );
    case "page.chat":
      return (
        <ChatWindow api={api} workspace={win.workspace} id={str(props.id)} />
      );
    case "page.chat.research":
      return <ChatWindow api={api} workspace="research" id={str(props.id)} />;
    case "page.chat.dev":
      return <ChatWindow api={api} workspace="dev" id={str(props.id)} />;
    case "page.chat.admin":
      return <ChatWindow api={api} workspace="admin" id={str(props.id)} />;
    case "page.chat.project":
      return <ChatWindow api={api} workspace="project" id={str(props.id)} />;
    case "page.tasks":
      return <TasksPage api={api} taskId={str(props.id)} tick={ctx.tick} />;
    case "page.library":
      return (
        <ShelfPage
          api={api}
          tick={ctx.tick}
          application={win.workspace}
        />
      );
    case "page.archive":
      return <ArchivePage api={api} application={win.workspace} />;
    case "page.document":
      return <DocumentPage api={api} kind={str(props.kind)} id={str(props.id)} />;
    case "page.wiki":
      return <WikiPage api={api} id={str(props.id)} />;
    case "page.sheets":
      return <SheetsPage api={api} id={str(props.id)} />;
    case "page.program":
      return <ProgramSettings api={api} workspace={win.workspace} />;
    case "page.memory":
      return <MemoryPage api={api} application={win.workspace} />;
    case "page.research":
      return <ResearchPage api={api} />;
    case "page.knowledge":
      return <KnowledgePage api={api} me={me} />;
    case "page.project":
      return <ProjectPage api={api} project={str(props.project) ?? undefined} node={str(props.node) ?? undefined} />;
    case "page.instructions":
      return <InstructionsPage api={api} />;
    case "page.settings":
      return (
        <SettingsApp
          key={`${str(props.section) ?? ""}|${str(props.app) ?? ""}|${str(props.focus) ?? ""}`}
          api={api}
          workspaces={win.boot?.workspaces ?? []}
          section={str(props.section)}
          app={str(props.app)}
          focus={str(props.focus)}
        />
      );
    case "page.overview":
      return (
        <Tabbed
          initial={str(props.tab)}
          tabs={offered(win, [
            {
              key: "machines",
              module: "page.overview",
              render: () => (
                <Overview
                  api={api}
                  nodes={nodes}
                  loaded={ctx.nodesLoaded}
                  error={ctx.nodesError}
                  canAdd={ctx.isAdmin}
                />
              ),
            },
            {
              key: "inventory",
              module: "page.inventory",
              render: () => <InventoryPage api={api} />,
            },
            {
              key: "releases",
              module: "page.releases",
              render: () => <ReleasesPage api={api} />,
            },
          ])}
        />
      );
    case "page.node": {
      const nodeId = str(props.id);
      return nodeId ? (
        <NodeDetailView
          key={nodeId}
          api={api}
          nodeId={nodeId}
          onLoaded={(detail) => ctx.dispatch({ type: "detail_loaded", detail })}
        />
      ) : null;
    }
    case "page.models":
      return (
        <Tabbed
          initial={str(props.tab)}
          tabs={offered(win, [
            {
              key: "models",
              module: "page.models",
              render: () => (
                <ModelCatalogue
                  api={api}
                  nodes={nodes}
                  loaded={ctx.nodesLoaded}
                  error={ctx.nodesError}
                  onChanged={ctx.refreshNodes}
                />
              ),
            },
            {
              // which model works where: the pools and who does what
              key: "scheduling",
              module: "page.scheduling",
              labelKey: "layout.tab.distribution",
              render: () => (
                <>
                  <PoolsPanel api={api} />
                  <SchedulingPage api={api} />
                </>
              ),
            },
            {
              // how well and how fast: bench, everyday measurements, model changes, second look
              key: "measurements",
              module: "page.scheduling",
              labelKey: "layout.tab.measurements",
              render: () => (
                <>
                  <BenchPanel api={api} />
                  <MeasurementsPanel api={api} />
                  <ModelSwitchesPanel api={api} />
                  <QualityPanel api={api} />
                </>
              ),
            },
            {
              key: "params",
              module: "page.params",
              render: () => (
                <>
                  <ToolModes api={api} />
                  <ParamsPage api={api} />
                </>
              ),
            },
          ])}
        />
      );
    case "page.model": {
      const name = str(props.name);
      return name ? (
        <ModelDetail
          key={name}
          api={api}
          name={name}
          nodes={nodes}
          loaded={ctx.nodesLoaded}
          error={ctx.nodesError}
          onChanged={ctx.refreshNodes}
        />
      ) : null;
    }
    case "page.scheduling":
      return (
        <>
          <PoolsPanel api={api} />
          <BenchPanel api={api} />
          <MeasurementsPanel api={api} />
          <ModelSwitchesPanel api={api} />
          <QualityPanel api={api} />
          <SchedulingPage api={api} />
        </>
      );
    case "page.releases":
      return <ReleasesPage api={api} />;
    case "page.params":
      return (
        <>
          <ToolModes api={api} />
          <ParamsPage api={api} />
        </>
      );
    case "page.programs":
      return <ProgramsPage api={api} />;
    case "page.inventory":
      return <InventoryPage api={api} />;
    case "page.identity":
      return <IdentityPage api={api} isAdmin={ctx.isAdmin} />;
    case "page.repos":
      return <DevRepos api={api} />;
    case "page.workshop":
      return <DevWorkshop api={api} nodes={nodes} />;
    case "page.files":
      return <FilesPage api={api} nodes={nodes} />;
    case "page.care":
      return <CarePage api={api} />;
    case "page.sources":
      return <SourcesPage api={api} me={me} />;
    case "page.access":
      return <ApiAccess api={api} />;
    case "layout.workspaces":
      return (
        <Tabbed
          initial={str(props.tab)}
          tabs={offered(win, [
            {
              // who signs in: the people and their profile (the applications are under
              // Applications since phase 4)
              key: "people",
              module: "layout.workspaces",
              labelKey: "layout.tab.people",
              render: () => <WorkspacesAdmin api={api} boot={win.boot} part="people" />,
            },
            {
              key: "keys",
              module: "page.access",
              render: () => <ApiAccess api={api} />,
            },
            {
              key: "identity",
              module: "page.identity",
              render: () => <IdentityPage api={api} isAdmin={ctx.isAdmin} />,
            },
          ])}
        />
      );
    default:
      return null;
  }
}
