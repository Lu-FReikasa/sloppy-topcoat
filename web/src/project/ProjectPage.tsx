import {
  useCallback,
  useEffect,
  useMemo,
  useState,
  type FormEvent,
} from "react";
import { useTranslation } from "react-i18next";
import { ApiError, type ApiClient } from "../api/client";
import { describeError, toActionError } from "../errors";
import { BoardView } from "./BoardView";
import { CalendarView } from "./CalendarView";
import { ChangeSets } from "./ChangeSets";
import { Conversations } from "./Conversations";
import { saveBlob } from "./download";
import { GanttView } from "./GanttView";
import { FilterBar } from "./FilterBar";
import { ListView } from "./ListView";
import { NodeDetail } from "./NodeDetail";
import { ProjectSettings } from "./ProjectSettings";
import { QuickAdd } from "./QuickAddForm";
import { TemplatesPage } from "./TemplatesPage";
import { Timeline } from "./Timeline";
import { TrashPage } from "./TrashPage";
import { TableView } from "./TableView";
import { TodayView } from "./TodayView";
import { browserZone, formatDue } from "./time";
import {
  PROJECT_GRANTS,
  type ProjectDetail,
  type ProjectGrant,
  type ProjectNode,
  type ProjectNodeCreate,
  type Project,
  type ProjectReportKind,
  type ProjectSavedView,
  type ProjectScope,
  type ProjectTaskStatus,
} from "./types";
import "./project.css";
import { onProjectChanged } from "./liveProject";
import { askConfirm } from "../components/askConfirm";

// changes that come in a burst (three new tasks) are one reload
const LIVE_RELOAD_MS = 300;

const REPORT_KINDS: readonly ProjectReportKind[] = [
  "note",
  "question",
  "decision",
  "document",
  "fact",
];
const TODAY = "today";
const TRASH = "trash";
const TEMPLATES = "templates";
const SETTINGS = "settings";

type Tab = "tree" | "timeline" | "sets" | "chains";

type Layout = "list" | "table" | "board" | "calendar" | "gantt";
const LAYOUTS: readonly Layout[] = [
  "list",
  "table",
  "board",
  "calendar",
  "gantt",
];

interface Props {
  api: ApiClient;
  /** Open this project (an inbox item of a reminder does). */
  project?: string;
  /** and this entry of it. */
  node?: string;
}

/** The Projekte page. Projects (and "Today", across all of them) on the left; the selected project
 *  in the middle as a list, a table or a board, with a filter, a sort rule and saved views; the
 *  selected node below it. Each project has its own grants for the project agent. Every write
 *  carries the revision it was read at; a conflict reloads and says so, it never overwrites. */
export function ProjectPage({
  api,
  project: openProject,
  node: openNode,
}: Props) {
  const { t, i18n } = useTranslation();
  const [zone, setZone] = useState(() => browserZone());
  const [ownZone, setOwnZone] = useState<string | null>(null);
  const [hubZone, setHubZone] = useState("UTC");
  const [projects, setProjects] = useState<Project[] | null>(null);
  const [selected, setSelected] = useState<string | null>(null);
  const [detail, setDetail] = useState<ProjectDetail | null>(null);
  const [nodeId, setNodeId] = useState<string | null>(null);
  const [opened, setOpened] = useState<Set<string>>(new Set());
  const [closed, setClosed] = useState<Set<string>>(new Set());
  const [showScope, setShowScope] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [message, setMessage] = useState<string | null>(null);
  const [layout, setLayout] = useState<Layout>("list");
  const [tab, setTab] = useState<Tab>("tree");
  const [showReplaced, setShowReplaced] = useState(false);
  const [pendingSets, setPendingSets] = useState(0);
  const [naming, setNaming] = useState(false);
  const [templateName, setTemplateName] = useState("");
  const [filter, setFilter] = useState("");
  const [sort, setSort] = useState("");
  const [filterError, setFilterError] = useState<string | null>(null);
  const [found, setFound] = useState<ProjectNode[] | null>(null);
  const [views, setViews] = useState<ProjectSavedView[]>([]);
  const [limits, setLimits] = useState<
    Partial<Record<ProjectTaskStatus, number>>
  >({});
  const [tick, setTick] = useState(0);
  const [now, setNow] = useState(() => new Date());

  const fail = useCallback(
    (err: unknown) => {
      if (err instanceof ApiError && err.status === 409) {
        setError(t("project.conflict"));
      } else {
        setError(describeError(toActionError(err), t, i18n));
      }
    },
    [t, i18n],
  );

  const loadList = useCallback(async () => {
    try {
      setProjects((await api.projectProjects()).projects);
    } catch (err) {
      fail(err);
    }
  }, [api, fail]);

  const isProject =
    selected !== null &&
    !([TODAY, TRASH, TEMPLATES, SETTINGS] as string[]).includes(selected);

  const loadDetail = useCallback(
    async (id: string) => {
      try {
        setDetail(await api.projectProject(id));
        setNow(new Date());
      } catch (err) {
        fail(err);
      }
    },
    [api, fail],
  );

  useEffect(() => {
    let alive = true;
    api.projectProjects().then(
      (r) => {
        if (alive) setProjects(r.projects);
      },
      (err) => {
        if (alive) fail(err);
      },
    );
    return () => {
      alive = false;
    };
  }, [api, fail]);

  useEffect(() => {
    if (!selected || !isProject) return;
    let alive = true;
    api.projectProject(selected).then(
      (d) => {
        if (alive) {
          setDetail(d);
          setNow(new Date());
        }
      },
      (err) => {
        if (alive) fail(err);
      },
    );
    api.projectViews(selected).then(
      (r) => alive && setViews(r.views),
      () => undefined,
    );
    return () => {
      alive = false;
    };
  }, [api, fail, selected, isProject]);

  // the person's own time zone drives the dates of the page, as it drives "today" on the hub
  useEffect(() => {
    let alive = true;
    void Promise.resolve()
      .then(() => api.me())
      .then(
        (me) => {
          if (!alive) return;
          setOwnZone(me.timezone ?? null);
          setHubZone(me.hub_timezone ?? "UTC");
          if (me.effective_timezone) setZone(me.effective_timezone);
        },
        () => undefined, // the browser's zone stays
      );
    return () => {
      alive = false;
    };
  }, [api]);

  // opened from an inbox item: in the project, on the entry
  useEffect(() => {
    if (!openProject) return;
    void Promise.resolve().then(() => {
      setSelected(openProject);
      setNodeId(openNode ?? null);
      setTab("tree");
    });
  }, [openProject, openNode]);

  // a change made elsewhere (the agent, a batch, a click in the chat) shows at once: the open
  // project reloads, the list too (titles, counts); a burst of changes is one reload
  useEffect(() => {
    let timer: ReturnType<typeof setTimeout> | undefined;
    const off = onProjectChanged((projectId) => {
      if (timer !== undefined) clearTimeout(timer);
      timer = setTimeout(() => {
        void loadList();
        if (isProject && selected === projectId) {
          void loadDetail(projectId);
          setTick((n) => n + 1);
        }
      }, LIVE_RELOAD_MS);
    });
    return () => {
      off();
      if (timer !== undefined) clearTimeout(timer);
    };
  }, [isProject, selected, loadDetail, loadList]);

  // how many change sets wait for the person, shown on their tab
  useEffect(() => {
    if (!isProject || !selected) return;
    let alive = true;
    api.projectChangeSets({ project: selected }).then(
      (r) =>
        alive &&
        setPendingSets(
          r.change_sets.filter((c) => c.status === "pending").length,
        ),
      () => undefined,
    );
    return () => {
      alive = false;
    };
  }, [api, isProject, selected, tick]);

  // a filter or a sort rule is answered by the hub (its query language), the plain tree is not
  useEffect(() => {
    if (!isProject || (!filter && !sort)) return;
    let alive = true;
    api
      .searchProjectNodes({
        filter,
        sort,
        project: selected ?? undefined,
        limit: 200,
      })
      .then(
        (r) => {
          if (!alive) return;
          setFound(r.nodes);
          setFilterError(null);
        },
        (err) => {
          if (!alive) return;
          if (err instanceof ApiError && err.status === 422)
            setFilterError(err.message);
          else fail(err);
        },
      );
    return () => {
      alive = false;
    };
  }, [api, fail, isProject, selected, filter, sort, tick]);

  const expandNode = (id: string) => {
    setClosed((prev) => {
      const next = new Set(prev);
      next.delete(id);
      return next;
    });
    setOpened((prev) => new Set(prev).add(id));
  };

  const collapseNode = (id: string) => {
    setOpened((prev) => {
      const next = new Set(prev);
      next.delete(id);
      return next;
    });
    setClosed((prev) => new Set(prev).add(id));
  };

  const select = (id: string) => {
    setSelected(id);
    setNodeId(null);
    setShowScope(false);
    setOpened(new Set());
    setClosed(new Set());
    setError(null);
    setMessage(null);
    setFilter("");
    setSort("");
    setFound(null);
    setFilterError(null);
    setLimits({});
    setTab("tree");
  };

  /** Run a write, then reload what it touched. A stale revision reloads and says so. */
  const write = async (action: () => Promise<unknown>, done?: string) => {
    setError(null);
    setMessage(null);
    try {
      await action();
      if (done) setMessage(done);
      await loadList();
      if (isProject && selected) await loadDetail(selected);
      setTick((n) => n + 1);
    } catch (err) {
      fail(err);
      if (isProject && selected) await loadDetail(selected);
    }
  };

  const exportAs = async (id: string, format: "json" | "markdown") => {
    setError(null);
    try {
      const file = await api.downloadProjectExport(id, format);
      saveBlob(file.blob, file.filename);
      setMessage(t("project.exported"));
    } catch (err) {
      fail(err);
    }
  };

  const saveTemplate = async (project: Project) => {
    const name = templateName.trim();
    if (!name) return;
    await write(async () => {
      await api.saveProjectTemplate({ project_id: project.id, name });
      setNaming(false);
      setTemplateName("");
    }, t("project.templateSaved"));
  };

  const complete = async (node: ProjectNode) => {
    await write(async () => {
      const done = await api.completeProjectNode(node.id, node.revision);
      setMessage(
        done.reopened
          ? t("project.reopened", {
              when: formatDue(done.due_at, i18n.language, zone),
            })
          : t("project.completed"),
      );
    });
  };

  const moveTo = (task: ProjectNode, status: ProjectTaskStatus) => {
    if (task.status === status) return;
    void write(
      () => api.updateProjectNode(task.id, { revision: task.revision, status }),
      t("project.saved"),
    );
  };

  /** Make a task from the quick add line; its labels are found by name or made. */
  const quickCreate = async (
    project: Project,
    body: ProjectNodeCreate,
    labels: string[],
  ) => {
    await write(async () => {
      const created = await api.createProjectNode(project.id, body);
      if (labels.length > 0) {
        const known = (await api.projectLabels()).labels;
        const ids: string[] = [];
        for (const name of labels) {
          const hit = known.find(
            (l) => l.title.toLowerCase() === name.toLowerCase(),
          );
          ids.push(
            hit ? hit.id : (await api.createProjectLabel({ title: name })).id,
          );
        }
        await api.setProjectNodeLabels(created.id, { add: ids });
      }
    }, t("project.created"));
  };

  const applyFilter = (nextFilter: string, nextSort: string) => {
    setFilter(nextFilter);
    setSort(nextSort);
    setFilterError(null);
    if (!nextFilter && !nextSort) setFound(null);
  };

  const pickView = (view: ProjectSavedView) => {
    if ((LAYOUTS as readonly string[]).includes(view.type))
      setLayout(view.type as Layout);
    const wip = (view.config?.wip ?? {}) as Partial<
      Record<ProjectTaskStatus, number>
    >;
    setLimits(wip);
    applyFilter(view.filter, view.sort);
  };

  const saveView = (title: string) =>
    write(async () => {
      await api.createProjectView({
        title,
        type: layout,
        filter,
        sort,
        config: { wip: limits },
        project_id: selected,
      });
      setViews((await api.projectViews(selected ?? undefined)).views);
    }, t("project.viewSaved"));

  const deleteView = (view: ProjectSavedView) =>
    write(async () => {
      await api.deleteProjectView(view.id);
      setViews((await api.projectViews(selected ?? undefined)).views);
    });

  const nodes = useMemo(() => detail?.nodes ?? [], [detail]);

const collapsed = useMemo(() => {
  const out = new Set<string>();
  for (const n of nodes) {
    if (nodes.filter((c) => c.parent_id === n.id).length >= 3 && !opened.has(n.id))
      out.add(n.id);
  }
  for (const id of closed) out.add(id);
  return out;
}, [nodes, opened, closed]);

  // A task's run (project_runs.py): start it, approve a batch of the open tasks, and keep the page
  // current while a run is in progress (the node's status and the batch change on the hub).
  const runNode = async (node: ProjectNode) => {
    try {
      await api.runProjectNode(node.id);
      await loadDetail(node.project_id);
    } catch (err) {
      fail(err);
    }
  };

  const approveBatch = async () => {
    if (!selected) return;
    const open = nodes.filter((n) => n.kind === "task" && n.status === "todo");
    if (open.length === 0) return;
    const titles = open.map((n) => `${n.ref} ${n.title}`).join("\n");
    if (!await askConfirm(t("project.runBatchConfirm", { count: open.length, titles }))) return;
    try {
      const batch = await api.createProjectBatch(selected, {
        node_ids: open.map((n) => n.id),
        max_runs: open.length,
      });
      if (batch.status === "stopped") {
        setMessage(t("project.runBatchStopped", { reason: batch.stop_reason ?? "" }));
      }
      await loadDetail(selected);
    } catch (err) {
      fail(err);
    }
  };

  const running = nodes.some((n) => n.status === "in_progress");
  useEffect(() => {
    if (!running || !selected) return;
    const timer = window.setInterval(() => void loadDetail(selected), 4000);
    return () => window.clearInterval(timer);
  }, [running, selected, loadDetail]);
  const node = nodes.find((n) => n.id === nodeId) ?? null;
  // a report that was replaced by a newer one is greyed and left out unless asked for
  const current_ = useMemo(
    () => (showReplaced ? nodes : nodes.filter((n) => n.current !== false)),
    [nodes, showReplaced],
  );
  const hasReplaced = nodes.some((n) => n.current === false);
  const visible = useMemo(
    () => visibleNodes(current_, collapsed),
    [current_, collapsed],
  );
  const current = isProject
    ? (projects?.find((p) => p.id === selected) ?? detail?.project ?? null)
    : null;
  const filtering = !!(filter || sort);
  // what the table and the board show: the hub's answer to a filter, else the tasks of the tree
  const shown = filtering ? (found ?? []) : current_;
  const tasks = shown.filter((n) => n.kind === "task");
  const listRows = filtering
    ? (found ?? []).map((n) => ({ node: n, depth: 0, hasChildren: false, childCount: 0 }))
    : visible;

  return (
    // the project and task in view, for Clipper's conversation (persona/ClipperDock.tsx, placeOf):
    // the window's props name only what it was opened with (live 2026-11-20: the agent guessed "p1")
    <div className="project" data-place-project={selected ?? undefined} data-place-node={nodeId ?? undefined}>
      <aside className="project-projects" aria-label={t("project.projects")}>
        <h2>{t("project.projects")}</h2>
        <ul className="project-list">
          <li>
            <button
              type="button"
              className={`project-row${selected === TODAY ? " is-selected" : ""}`}
              onClick={() => select(TODAY)}
            >
              <span className="project-name">{t("project.today")}</span>
            </button>
          </li>
          <li>
            <button
              type="button"
              className={`project-row${selected === TEMPLATES ? " is-selected" : ""}`}
              onClick={() => select(TEMPLATES)}
            >
              <span className="project-name">{t("project.templates")}</span>
            </button>
          </li>
          <li>
            <button
              type="button"
              className={`project-row${selected === TRASH ? " is-selected" : ""}`}
              onClick={() => select(TRASH)}
            >
              <span className="project-name">{t("project.trash")}</span>
            </button>
          </li>
          <li>
            <button
              type="button"
              className={`project-row${selected === SETTINGS ? " is-selected" : ""}`}
              onClick={() => select(SETTINGS)}
            >
              <span className="project-name">{t("project.settingsEntry")}</span>
            </button>
          </li>
        </ul>
        {projects === null ? (
          <p className="project-muted">{t("project.loading")}</p>
        ) : projects.length === 0 ? (
          <p className="project-muted">{t("project.empty")}</p>
        ) : (
          <ul className="project-list">
            {projects.map((p) => (
              <li key={p.id}>
                <button
                  type="button"
                  className={`project-row${p.id === selected ? " is-selected" : ""}`}
                  onClick={() => select(p.id)}
                >
                  <span className="project-name">{p.title}</span>
                  <span className="project-muted">
                    {t(`project.statuses.${p.status}`)}
                  </span>
                </button>
              </li>
            ))}
          </ul>
        )}
        <NewProject
          onCreate={(title, description) =>
            write(async () => {
              const created = await api.createProject({ title, description });
              select(created.id);
            }, t("project.created"))
          }
        />
      </aside>

      <section className="project-tree" aria-label={t("project.tree")}>
        {selected === TEMPLATES ? (
          <>
            {error && (
              <p role="alert" className="project-error">
                {error}
              </p>
            )}
            {message && (
              <p role="status" className="project-ok">
                {message}
              </p>
            )}
            <TemplatesPage
              api={api}
              zone={zone}
              tick={tick}
              onError={fail}
              onChanged={(m) => {
                setError(null);
                setMessage(m);
              }}
              onCreated={(created) => {
                void loadList();
                select(created.id);
                setMessage(t("project.created"));
              }}
            />
          </>
        ) : selected === SETTINGS ? (
          <>
            {error && (
              <p role="alert" className="project-error">
                {error}
              </p>
            )}
            <ProjectSettings
              api={api}
              zone={zone}
              ownZone={ownZone}
              hubZone={hubZone}
              onError={fail}
              onZone={(own, effective) => {
                setOwnZone(own);
                setZone(effective);
              }}
            />
          </>
        ) : selected === TRASH ? (
          <>
            {error && (
              <p role="alert" className="project-error">
                {error}
              </p>
            )}
            {message && (
              <p role="status" className="project-ok">
                {message}
              </p>
            )}
            <TrashPage
              api={api}
              tick={tick}
              onError={fail}
              onChanged={(m) => {
                setError(null);
                setMessage(m);
                void loadList();
              }}
            />
          </>
        ) : selected === TODAY ? (
          <>
            {error && (
              <p role="alert" className="project-error">
                {error}
              </p>
            )}
            {message && (
              <p role="status" className="project-ok">
                {message}
              </p>
            )}
            <TodayView
              api={api}
              now={now}
              zone={zone}
              tick={tick}
              onError={fail}
              onComplete={complete}
              onOpen={(n) => {
                select(n.project_id);
                setNodeId(n.id);
              }}
            />
          </>
        ) : !current ? (
          <p className="project-muted">{t("project.pick")}</p>
        ) : (
          <>
            <header className="project-head">
              <div>
                <h2>{current.title}</h2>
                <span className="project-muted">
                  {t(`project.statuses.${current.status}`)}
                </span>
              </div>
              <div className="project-actions">
                {nodes.some((n) => n.kind === "task" && n.status === "todo") && (
                  <button type="button" onClick={() => void approveBatch()}>
                    {t("project.runBatch")}
                  </button>
                )}
                <button type="button" onClick={() => setShowScope((v) => !v)}>
                  {t("project.scope")}
                </button>
                <button
                  type="button"
                  onClick={() => void exportAs(current.id, "json")}
                >
                  {t("project.exportJson")}
                </button>
                <button
                  type="button"
                  onClick={() => void exportAs(current.id, "markdown")}
                >
                  {t("project.exportZip")}
                </button>
                <button
                  type="button"
                  onClick={() => setNaming((v) => !v)}
                  aria-expanded={naming}
                >
                  {t("project.saveTemplate")}
                </button>
                <button
                  type="button"
                  onClick={() =>
                    write(async () => {
                      await api.trashProject(current.id);
                      setSelected(null);
                      setDetail(null);
                    }, t("project.trashed"))
                  }
                >
                  {t("project.moveToTrash")}
                </button>
                {current.status !== "dropped" && (
                  <button
                    type="button"
                    onClick={() =>
                      write(
                        () =>
                          api.updateProject(current.id, {
                            revision: current.revision,
                            status: "dropped",
                          }),
                        t("project.archived"),
                      )
                    }
                  >
                    {t("project.archive")}
                  </button>
                )}
              </div>
            </header>
            {error && (
              <p role="alert" className="project-error">
                {error}
              </p>
            )}
            {message && (
              <p role="status" className="project-ok">
                {message}
              </p>
            )}

            {naming && (
              <form
                className="project-form project-bar"
                onSubmit={(e) => {
                  e.preventDefault();
                  void saveTemplate(current);
                }}
              >
                <input
                  aria-label={t("project.templateName")}
                  placeholder={t("project.templateName")}
                  value={templateName}
                  onChange={(e) => setTemplateName(e.target.value)}
                  maxLength={80}
                />
                <button type="submit" disabled={!templateName.trim()}>
                  {t("project.saveTemplate")}
                </button>
              </form>
            )}
            <div
              className="project-switch"
              role="tablist"
              aria-label={t("project.tree")}
            >
              {(["tree", "timeline", "sets", "chains"] as const).map((tb) => (
                <button
                  key={tb}
                  type="button"
                  role="tab"
                  aria-selected={tab === tb}
                  className={tab === tb ? "is-selected" : undefined}
                  onClick={() => setTab(tb)}
                >
                  {t(
                    tb === "tree"
                      ? "project.treeTab"
                      : tb === "timeline"
                        ? "project.timelineTab"
                        : tb === "sets"
                          ? "project.changeSetsTab"
                          : "project.chainsTab",
                  )}
                  {tb === "sets" && pendingSets > 0 ? ` (${pendingSets})` : ""}
                </button>
              ))}
            </div>
            {tab === "timeline" ? (
              <Timeline
                api={api}
                projectId={current.id}
                nodes={nodes}
                zone={zone}
                tick={tick}
                onError={fail}
              />
            ) : tab === "chains" ? (
              <Conversations
                api={api}
                projectId={current.id}
                tick={tick}
                onError={fail}
                onChanged={() => {
                  void loadDetail(current.id);
                  setTick((n) => n + 1);
                }}
              />
            ) : tab === "sets" ? (
              <ChangeSets
                api={api}
                projectId={current.id}
                tick={tick}
                onError={fail}
                onDecided={(m) => {
                  setError(null);
                  setMessage(m);
                  void loadDetail(current.id);
                  void loadList();
                  setTick((n) => n + 1);
                }}
              />
            ) : showScope ? (
              <ScopeEditor
                scope={current.scope}
                onSave={(scope) =>
                  write(
                    () =>
                      api.setProjectScope(current.id, {
                        revision: current.revision,
                        scope,
                      }),
                    t("project.scopeSaved"),
                  )
                }
              />
            ) : (
              <>
                <p className="project-description">
                  {current.description || t("project.noDescription")}
                </p>
                <QuickAdd
                  zone={zone}
                  parentId={null}
                  onCreate={(body, labels) =>
                    void quickCreate(current, body, labels)
                  }
                />
                <NewNode
                  nodes={nodes}
                  onCreate={(body) =>
                    write(
                      () => api.createProjectNode(current.id, body),
                      t("project.created"),
                    )
                  }
                />
                <div
                  className="project-switch"
                  role="group"
                  aria-label={t("project.viewSwitch")}
                >
                  {LAYOUTS.map((l) => (
                    <button
                      key={l}
                      type="button"
                      aria-pressed={layout === l}
                      className={layout === l ? "is-selected" : undefined}
                      onClick={() => setLayout(l)}
                    >
                      {t(`project.view${l[0]!.toUpperCase()}${l.slice(1)}`)}
                    </button>
                  ))}
                </div>
                <FilterBar
                  key={current.id}
                  api={api}
                  filter={filter}
                  sort={sort}
                  views={views}
                  error={filterError}
                  onApply={applyFilter}
                  onPickView={pickView}
                  onSaveView={(title) => void saveView(title)}
                  onDeleteView={(v) => void deleteView(v)}
                />
                {hasReplaced && (
                  <label className="project-bar">
                    <input
                      type="checkbox"
                      checked={showReplaced}
                      onChange={(e) => setShowReplaced(e.target.checked)}
                    />
                    {t("project.showReplaced")}
                  </label>
                )}
                {layout === "list" &&
                  (listRows.length === 0 ? (
                    <p className="project-muted">{t("project.noNodes")}</p>
                  ) : (
                    <ListView
                      rows={listRows}
                      collapsed={collapsed}
                      selectedId={nodeId}
                      now={now}
                      zone={zone}
                      onToggle={(id) => (collapsed.has(id) ? expandNode(id) : collapseNode(id))}
                      onOpen={setNodeId}
                      onComplete={(n) => void complete(n)}
                    />
                  ))}
                {layout === "table" && (
                  <TableView
                    nodes={tasks}
                    sort={sort}
                    selectedId={nodeId}
                    now={now}
                    zone={zone}
                    onSort={(next) => applyFilter(filter, next)}
                    onOpen={setNodeId}
                    onComplete={(n) => void complete(n)}
                  />
                )}
                {layout === "calendar" && (
                  <CalendarView
                    tasks={tasks}
                    zone={zone}
                    selectedId={nodeId}
                    onOpen={setNodeId}
                    onMove={(task, patch) =>
                      void write(
                        () =>
                          api.updateProjectNode(task.id, {
                            revision: task.revision,
                            ...patch,
                          }),
                        t("project.saved"),
                      )
                    }
                  />
                )}
                {layout === "gantt" && (
                  <GanttView
                    nodes={shown}
                    zone={zone}
                    selectedId={nodeId}
                    onOpen={setNodeId}
                    onMove={(task, patch) =>
                      void write(
                        () =>
                          api.updateProjectNode(task.id, {
                            revision: task.revision,
                            ...patch,
                          }),
                        t("project.saved"),
                      )
                    }
                  />
                )}
                {layout === "board" && (
                  <BoardView
                    tasks={tasks}
                    limits={limits}
                    selectedId={nodeId}
                    now={now}
                    zone={zone}
                    onMove={moveTo}
                    onOpen={setNodeId}
                    onLimit={(status, limit) =>
                      setLimits((prev) => {
                        const next = { ...prev };
                        if (limit === null) delete next[status];
                        else next[status] = limit;
                        return next;
                      })
                    }
                  />
                )}
                {node && (
                  <NodeDetail
                    key={`${node.id}:${node.revision}`}
                    api={api}
                    node={node}
                    nodes={nodes}
                    zone={zone}
                    onRun={(n) => void runNode(n)}
                    onSave={(patch) =>
                      write(
                        () =>
                          api.updateProjectNode(node.id, {
                            revision: node.revision,
                            ...patch,
                          }),
                        t("project.saved"),
                      )
                    }
                    onMove={(parentId) =>
                      write(
                        () =>
                          api.moveProjectNode(node.id, {
                            revision: node.revision,
                            parent_id: parentId,
                          }),
                        t("project.moved"),
                      )
                    }
                    onAnswer={(answer) =>
                      write(
                        () => api.answerProjectQuestion(node.id, answer),
                        t("project.saved"),
                      )
                    }
                    onComplete={(n) => void complete(n)}
                    onTrash={() =>
                      write(async () => {
                        await api.trashProjectNode(node.id);
                        setNodeId(null);
                      }, t("project.trashed"))
                    }
                    onChanged={() => void (selected && loadDetail(selected))}
                    onRestored={() => {
                      setMessage(t("project.versionRestored"));
                      void (selected && loadDetail(selected));
                      setTick((n) => n + 1);
                    }}
                    onError={fail}
                  />
                )}
                <AskAgent
                  onSend={(prompt) =>
                    write(
                      () => api.startProjectRun(current.id, { prompt }),
                      t("project.started"),
                    )
                  }
                />
              </>
            )}
          </>
        )}
      </section>
    </div>
  );
}


/** The nodes in their order, with the ones under a collapsed node left out. */
export function visibleNodes(nodes: ProjectNode[], collapsed: Set<string>) {
  const byId = new Map(nodes.map((n) => [n.id, n]));
  const depthOf = (n: ProjectNode): number => {
    let depth = 0;
    let parent = n.parent_id ? byId.get(n.parent_id) : undefined;
    while (parent) {
      depth += 1;
      parent = parent.parent_id ? byId.get(parent.parent_id) : undefined;
    }
    return depth;
  };
  const hidden = (n: ProjectNode): boolean => {
    let parent = n.parent_id ? byId.get(n.parent_id) : undefined;
    while (parent) {
      if (collapsed.has(parent.id)) return true;
      parent = parent.parent_id ? byId.get(parent.parent_id) : undefined;
    }
    return false;
  };
  return nodes
    .filter((n) => !hidden(n))
    .map((n) => ({
      node: n,
      depth: depthOf(n),
      hasChildren: nodes.some((c) => c.parent_id === n.id),
      childCount: nodes.filter((c) => c.parent_id === n.id).length,
    }));
}

function NewProject({
  onCreate,
}: {
  onCreate: (title: string, description: string) => void;
}) {
  const { t } = useTranslation();
  const [title, setTitle] = useState("");
  const [description, setDescription] = useState("");
  const submit = (e: FormEvent) => {
    e.preventDefault();
    if (!title.trim()) return;
    onCreate(title.trim(), description.trim());
    setTitle("");
    setDescription("");
  };
  return (
    <form className="project-form" onSubmit={submit}>
      <h3>{t("project.newProject")}</h3>
      <label>
        {t("project.titleLabel")}
        <input
          value={title}
          onChange={(e) => setTitle(e.target.value)}
          maxLength={200}
        />
      </label>
      <label>
        {t("project.description")}
        <textarea
          value={description}
          onChange={(e) => setDescription(e.target.value)}
          rows={3}
        />
      </label>
      <button type="submit" disabled={!title.trim()}>
        {t("project.create")}
      </button>
    </form>
  );
}

function NewNode({
  nodes,
  onCreate,
}: {
  nodes: ProjectNode[];
  onCreate: (body: {
    kind: "topic" | "task" | "report";
    title: string;
    parent_id: string | null;
    report_kind?: ProjectReportKind;
  }) => void;
}) {
  const { t } = useTranslation();
  const [kind, setKind] = useState<"topic" | "task" | "report">("task");
  const [reportKind, setReportKind] = useState<ProjectReportKind>("note");
  const [title, setTitle] = useState("");
  const [parent, setParent] = useState<string>("");
  const parents = nodes.filter((n) => n.kind !== "report");
  const submit = (e: FormEvent) => {
    e.preventDefault();
    if (!title.trim()) return;
    onCreate({
      kind,
      title: title.trim(),
      parent_id: parent || null,
      ...(kind === "report" ? { report_kind: reportKind } : {}),
    });
    setTitle("");
  };
  return (
    <form className="project-form project-inline" onSubmit={submit}>
      <select
        aria-label={t("project.kind")}
        value={kind}
        onChange={(e) => setKind(e.target.value as typeof kind)}
      >
        <option value="topic">{t("project.kindTopic")}</option>
        <option value="task">{t("project.kindTask")}</option>
        <option value="report">{t("project.kindReport")}</option>
      </select>
      {kind === "report" && (
        <select
          aria-label={t("project.reportKind")}
          value={reportKind}
          onChange={(e) => setReportKind(e.target.value as ProjectReportKind)}
        >
          {REPORT_KINDS.map((k) => (
            <option key={k} value={k}>
              {t(`project.reportKinds.${k}`)}
            </option>
          ))}
        </select>
      )}
      <input
        aria-label={t("project.titleLabel")}
        placeholder={t("project.newNode")}
        value={title}
        onChange={(e) => setTitle(e.target.value)}
        maxLength={200}
      />
      <select
        aria-label={t("project.below")}
        value={parent}
        onChange={(e) => setParent(e.target.value)}
      >
        <option value="">{t("project.topLevel")}</option>
        {parents.map((n) => (
          <option key={n.id} value={n.id}>
            {n.number} {n.title}
          </option>
        ))}
      </select>
      <button type="submit" disabled={!title.trim()}>
        {t("project.add")}
      </button>
    </form>
  );
}

function ScopeEditor({
  scope,
  onSave,
}: {
  scope: ProjectScope;
  onSave: (scope: ProjectScope) => void;
}) {
  const { t } = useTranslation();
  const [draft, setDraft] = useState<ProjectScope>(scope);
  const set = (target: string, action: string, level: string) => {
    setDraft((prev) => {
      const next: ProjectScope = JSON.parse(JSON.stringify(prev));
      next[target] = { ...(next[target] ?? {}) };
      if (level === "none") delete next[target][action];
      else next[target][action] = level as ProjectGrant;
      if (Object.keys(next[target]).length === 0) delete next[target];
      return next;
    });
  };
  return (
    <div className="project-scope">
      <p className="project-muted">{t("project.scopeHelp")}</p>
      <table>
        <thead>
          <tr>
            <th>{t("project.scopeTarget")}</th>
            <th>{t("project.scopeAction")}</th>
            <th>{t("project.scopeLevel")}</th>
          </tr>
        </thead>
        <tbody>
          {Object.entries(PROJECT_GRANTS).flatMap(([target, actions]) =>
            actions.map((action) => (
              <tr key={`${target}.${action}`}>
                <td>{t(`project.targets.${target}`)}</td>
                <td>{t(`project.actions.${action}`)}</td>
                <td>
                  <select
                    aria-label={`${target} ${action}`}
                    value={draft[target]?.[action] ?? "none"}
                    onChange={(e) => set(target, action, e.target.value)}
                  >
                    <option value="none">{t("project.grantNone")}</option>
                    <option value="free">{t("project.grantFree")}</option>
                    <option value="confirm">{t("project.grantConfirm")}</option>
                  </select>
                </td>
              </tr>
            )),
          )}
        </tbody>
      </table>
      <p className="project-muted">{t("project.scopeNever")}</p>
      <button type="button" onClick={() => onSave(draft)}>
        {t("project.saveScope")}
      </button>
    </div>
  );
}

function AskAgent({ onSend }: { onSend: (prompt: string) => void }) {
  const { t } = useTranslation();
  const [prompt, setPrompt] = useState("");
  return (
    <form
      className="project-form"
      onSubmit={(e) => {
        e.preventDefault();
        if (!prompt.trim()) return;
        onSend(prompt.trim());
        setPrompt("");
      }}
    >
      <h3>{t("project.ask")}</h3>
      <textarea
        aria-label={t("project.ask")}
        placeholder={t("project.askPlaceholder")}
        value={prompt}
        onChange={(e) => setPrompt(e.target.value)}
        rows={3}
      />
      <button type="submit" disabled={!prompt.trim()}>
        {t("project.send")}
      </button>
    </form>
  );
}
