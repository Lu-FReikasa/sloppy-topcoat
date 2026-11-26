import { Herald } from "./persona/Herald";
import { heraldOf, signalTask } from "./persona/heraldNews";
import { useCallback, useEffect, useMemo, useReducer, useState } from "react";
import { useTranslation } from "react-i18next";
import { ApiClient, UnauthorizedError } from "./api/client";
import { connectStream, LOST_AFTER_MS, type StreamStatus } from "./api/sse";
import { hubEvents } from "./data/hubEvents";
import { clearToken, loadToken, saveToken } from "./auth";
import { announceHubMessage, parseHubMessage } from "./chat/hubMessage";
import { announceProjectChanged, projectOfEvent } from "./project/liveProject";
import { Login } from "./components/Login";
import { requestOpen } from "./layout/intent";
import { routeToModule } from "./layout/modules/meta";
import type { ModuleCtx } from "./layout/modules/render";
import { Desktop } from "./desktop/Desktop";
import { WebShell } from "./webshell/WebShell";
import { useWantsNav } from "./webshell/shell";
import { describeError, toActionError } from "./errors";
import { applyLanguage, isLanguage, type Language } from "./i18n";
import { useHashRoute, workspaceHref } from "./route";
import { actionFromStreamEvent, initialState, reducer } from "./state/store";
import type { Me } from "./types";

export function App() {
  const [token, setToken] = useState<string | null>(() => loadToken());
  const [invalid, setInvalid] = useState(false);

  const login = useCallback((tok: string) => {
    saveToken(tok);
    setInvalid(false);
    setToken(tok);
  }, []);

  const logout = useCallback(() => {
    clearToken();
    setToken(null);
  }, []);

  const unauthorized = useCallback(() => {
    clearToken();
    setInvalid(true);
    setToken(null);
  }, []);

  // the herald of the desktop client never asks for a sign-in: without one it just stays hidden
  if (!token) return heraldOf(window.location.search) ? null : <Login onLogin={login} invalid={invalid} />;
  // Keyed by token so all session state resets on re-login.
  return (
    <AuthedApp
      key={token}
      token={token}
      onLogout={logout}
      onUnauthorized={unauthorized}
    />
  );
}

interface AuthedProps {
  token: string;
  onLogout: () => void;
  onUnauthorized: () => void;
}

/** The whole interface is the desktop (docs/DESKTOP.md). An address of a page (`#/chat/<id>`,
 *  `#/admin/nodes/<id>`, a bookmark of the old interface) is not a page any more: it records which
 *  view it stands for, and the desktop opens the program that holds it on that view. */
function AuthedApp({ token, onLogout, onUnauthorized }: AuthedProps) {
  const { t, i18n } = useTranslation();
  const route = useHashRoute();
  // the web navigation in a plain browser, the window desktop in the client (phase 4)
  const nav = useWantsNav();
  const [state, dispatch] = useReducer(reducer, initialState);
  const [me, setMe] = useState<Me | null>(null);
  const [status, setStatus] = useState<StreamStatus>("connecting");
  const [epoch, setEpoch] = useState(0);
  const [agentTick, setAgentTick] = useState(0);
  const [inboxTick, setInboxTick] = useState(0);
  // the hub says the interface data (programs, grants, choices) changed (docs/DESKTOP.md)
  const [uiTick, setUiTick] = useState(0);
  const [nodesError, setNodesError] = useState<string | null>(null);

  const api = useMemo(
    () => new ApiClient({ token, onUnauthorized }),
    [token, onUnauthorized],
  );

  const refreshNodes = useCallback(() => {
    api.listNodes().then(
      (nodes) => {
        setNodesError(null);
        dispatch({ type: "nodes_loaded", nodes });
      },
      (err: unknown) => {
        if (err instanceof UnauthorizedError) return;
        setNodesError(describeError(toActionError(err), t, i18n));
      },
    );
  }, [api, t, i18n]);

  useEffect(() => {
    api.me().then(
      (m) => {
        setMe(m);
        // The user's stored choice wins over this browser's language.
        if (isLanguage(m.language) && m.language !== i18n.resolvedLanguage)
          applyLanguage(m.language);
      },
      () => undefined,
    );
    refreshNodes();
  }, [api, refreshNodes, i18n]);

  const saveLanguage = useCallback(
    (lang: Language) => {
      api.putLanguage(lang).then(setMe, () => undefined);
    },
    [api],
  );

  useEffect(() => {
    return connectStream({
      url: "/api/stream",
      token,
      onUnauthorized,
      onStatus: setStatus,
      // back within a few seconds of the hub's return, not after a long backoff
      maxBackoffMs: LOST_AFTER_MS,
      onOpen: () => {
        // Catch up on anything missed while disconnected, the programs too: a hub that was
        // updated while away may offer other ones.
        refreshNodes();
        setEpoch((e) => e + 1);
        setUiTick((n) => n + 1);
        // the data layer (web/src/data) reloads its live resources
        hubEvents.connected();
      },
      onEvent: (ev) => {
        hubEvents.publish(ev);
        // the agent's progress (E3-01a): the task pages refetch
        if (
          ev.event === "agent_task" ||
          ev.event === "agent_step" ||
          ev.event === "agent_proposal"
        )
          setAgentTick((n) => n + 1);
        if (ev.event === "inbox" || ev.event === "agent_task" || ev.event === "chat_message")
          setInboxTick((n) => n + 1);
        if (ev.event === "ui_access" || ev.event === "ui_prefs" || ev.event === "ui_themes")
          setUiTick((n) => n + 1);
        if (ev.event === "chat_message") {
          const hubMessage = parseHubMessage(ev.data);
          if (hubMessage) announceHubMessage(hubMessage);
        }
        // a change to a project (the agent's, a batch's, a click's): its open page reloads
        if (ev.event === "project_changed") {
          const projectId = projectOfEvent(ev.data);
          if (projectId) announceProjectChanged(projectId);
        }
        // the herald and Clipper at a window follow the person's tasks
        signalTask(ev.event, ev.data);
        const action = actionFromStreamEvent(ev);
        if (action) dispatch(action);
      },
    });
  }, [token, onUnauthorized, refreshNodes]);

  // Without a loaded profile the admin menu stays (the hub guards every admin route anyway).
  const isAdmin = me?.role !== "user";

  // Every address that is not a program is moved to the desktop, which opens the view it stands for.
  const moveToWorkspace = route.view !== "w";
  useEffect(() => {
    if (!moveToWorkspace) return;
    const target = routeToModule(route);
    // the desktop stays mounted (no flicker), so it is told, not left to find it at its start
    if (target) requestOpen(target);
    window.location.replace(workspaceHref());
  }, [moveToWorkspace, route]);

  const moduleCtx = useMemo<ModuleCtx>(
    () => ({
      api,
      me,
      isAdmin,
      nodes: state.nodes,
      nodesLoaded: state.nodesLoaded,
      nodesError,
      refreshNodes,
      tick: agentTick,
      inboxTick,
      epoch,
      detail: state.detail,
      dispatch,
    }),
    [
      api,
      me,
      isAdmin,
      state.nodes,
      state.nodesLoaded,
      nodesError,
      refreshNodes,
      agentTick,
      inboxTick,
      epoch,
      state.detail,
    ],
  );

  // the desktop client's corner window: only Clipper with the news of finished tasks
  if (heraldOf(window.location.search)) return <Herald />;
  if (nav)
    return (
      <WebShell
        api={api}
        ctx={moduleCtx}
        routeKey={route.view === "w" ? route.key : undefined}
        uiTick={uiTick}
        status={status}
        onSaveLanguage={saveLanguage}
        onLogout={onLogout}
      />
    );
  // the desktop stays mounted while another address moves to it: unmounting it for one render
  // rebuilt every program (a flash, over the overlay's see-through page a flicker of everything)
  return (
    <Desktop
      api={api}
      ctx={moduleCtx}
      routeKey={route.view === "w" ? route.key : undefined}
      moving={moveToWorkspace}
      uiTick={uiTick}
      status={status}
      onSaveLanguage={saveLanguage}
      onLogout={onLogout}
    />
  );
}
