import { useCallback, useEffect, useMemo, useState } from "react";
import { ApiError, type ApiClient } from "../api/client";
import { onProjectChanged } from "./liveProject";
import type {
  Project,
  ProjectDetail,
  ProjectNode,
  ProjectSavedView,
  ProjectTaskStatus,
} from "./types";

// changes that come in a burst (three new tasks) are one reload
const LIVE_RELOAD_MS = 300;
// how often the page reloads while a task runs
const RUNNING_POLL_MS = 4000;
// a node with this many children starts folded
const FOLD_FROM_CHILDREN = 3;
// the most nodes a filter answer shows
const SEARCH_LIMIT = 200;
const UNPROCESSABLE = 422;

export const TODAY = "today";
export const TRASH = "trash";
export const TEMPLATES = "templates";
export const SETTINGS = "settings";

export type Layout = "list" | "table" | "board" | "calendar" | "gantt";
export const LAYOUTS: readonly Layout[] = [
  "list",
  "table",
  "board",
  "calendar",
  "gantt",
];

export type WipLimits = Partial<Record<ProjectTaskStatus, number>>;

export interface TreeRow {
  node: ProjectNode;
  depth: number;
  hasChildren: boolean;
  childCount: number;
}

/** The left column and the selected project's tree: the project list, which entry is selected
 *  (a project, or Today/Templates/Trash/Settings), the loaded project with its nodes, which nodes
 *  are folded, the layout, the filter and sort rule with the hub's answer, saved views and WIP
 *  limits. Keeps the loaded project current (live changes, a running task, pending change sets).
 *  `fail` shows an error on the page. */
export function useProjectTree(api: ApiClient, fail: (err: unknown) => void) {
  const [projects, setProjects] = useState<Project[] | null>(null);
  const [selected, setSelected] = useState<string | null>(null);
  const [detail, setDetail] = useState<ProjectDetail | null>(null);
  const [opened, setOpened] = useState<Set<string>>(new Set());
  const [closed, setClosed] = useState<Set<string>>(new Set());
  const [layout, setLayout] = useState<Layout>("list");
  const [showReplaced, setShowReplaced] = useState(false);
  const [pendingSets, setPendingSets] = useState(0);
  const [filter, setFilter] = useState("");
  const [sort, setSort] = useState("");
  const [filterError, setFilterError] = useState<string | null>(null);
  const [found, setFound] = useState<ProjectNode[] | null>(null);
  const [views, setViews] = useState<ProjectSavedView[]>([]);
  const [limits, setLimits] = useState<WipLimits>({});
  const [tick, setTick] = useState(0);
  const [now, setNow] = useState(() => new Date());

  /** Tell the parts of the page that load their own data (timeline, change sets, …) to reload. */
  const bump = useCallback(() => setTick((n) => n + 1), []);

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
          bump();
        }
      }, LIVE_RELOAD_MS);
    });
    return () => {
      off();
      if (timer !== undefined) clearTimeout(timer);
    };
  }, [isProject, selected, loadDetail, loadList, bump]);

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
        limit: SEARCH_LIMIT,
      })
      .then(
        (r) => {
          if (!alive) return;
          setFound(r.nodes);
          setFilterError(null);
        },
        (err) => {
          if (!alive) return;
          if (err instanceof ApiError && err.status === UNPROCESSABLE)
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

  /** Select an entry of the left column; the tree's folds, filter and limits start afresh. */
  const select = (id: string) => {
    setSelected(id);
    setOpened(new Set());
    setClosed(new Set());
    setFilter("");
    setSort("");
    setFound(null);
    setFilterError(null);
    setLimits({});
  };

  /** Nothing selected any more (the project went to the trash). */
  const unselect = () => {
    setSelected(null);
    setDetail(null);
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
    const wip = (view.config?.wip ?? {}) as WipLimits;
    setLimits(wip);
    applyFilter(view.filter, view.sort);
  };

  const setLimit = (status: ProjectTaskStatus, limit: number | null) =>
    setLimits((prev) => {
      const next = { ...prev };
      if (limit === null) delete next[status];
      else next[status] = limit;
      return next;
    });

  const nodes = useMemo(() => detail?.nodes ?? [], [detail]);

  // a node with three or more children starts folded, until the person opens it
  const collapsed = useMemo(() => {
    const out = new Set<string>();
    for (const n of nodes) {
      if (
        nodes.filter((c) => c.parent_id === n.id).length >=
          FOLD_FROM_CHILDREN &&
        !opened.has(n.id)
      )
        out.add(n.id);
    }
    for (const id of closed) out.add(id);
    return out;
  }, [nodes, opened, closed]);

  const toggleNode = (id: string) =>
    collapsed.has(id) ? expandNode(id) : collapseNode(id);

  // keep the page current while a run is in progress (the node's status and the batch change on
  // the hub)
  const running = nodes.some((n) => n.status === "in_progress");
  useEffect(() => {
    if (!running || !selected) return;
    const timer = window.setInterval(
      () => void loadDetail(selected),
      RUNNING_POLL_MS,
    );
    return () => window.clearInterval(timer);
  }, [running, selected, loadDetail]);

  // a report that was replaced by a newer one is greyed and left out unless asked for
  const currentNodes = useMemo(
    () => (showReplaced ? nodes : nodes.filter((n) => n.current !== false)),
    [nodes, showReplaced],
  );
  const hasReplaced = nodes.some((n) => n.current === false);
  const visible = useMemo(
    () => visibleNodes(currentNodes, collapsed),
    [currentNodes, collapsed],
  );
  const project = isProject
    ? (projects?.find((p) => p.id === selected) ?? detail?.project ?? null)
    : null;
  const filtering = !!(filter || sort);
  // what the table and the board show: the hub's answer to a filter, else the tasks of the tree
  const shown = filtering ? (found ?? []) : currentNodes;
  const tasks = shown.filter((n) => n.kind === "task");
  const listRows: TreeRow[] = filtering
    ? (found ?? []).map((n) => ({
        node: n,
        depth: 0,
        hasChildren: false,
        childCount: 0,
      }))
    : visible;

  return {
    projects,
    selected,
    setSelected,
    isProject,
    project,
    nodes,
    loadList,
    loadDetail,
    select,
    unselect,
    tick,
    bump,
    now,
    collapsed,
    toggleNode,
    layout,
    setLayout,
    showReplaced,
    setShowReplaced,
    hasReplaced,
    pendingSets,
    filter,
    sort,
    filterError,
    applyFilter,
    views,
    setViews,
    pickView,
    limits,
    setLimit,
    shown,
    tasks,
    listRows,
  };
}

/** The nodes in their order, with the ones under a collapsed node left out. */
export function visibleNodes(
  nodes: ProjectNode[],
  collapsed: Set<string>,
): TreeRow[] {
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
