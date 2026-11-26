import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useTranslation } from "react-i18next";
import type { ApiClient } from "../api/client";
import type { StreamStatus } from "../api/sse";
import { APPLICATIONS } from "../chat/applications";
import { LanguageSwitch } from "../components/LanguageSwitch";
import { CommandPalette, type PaletteEntry } from "../desktop/CommandPalette";
import { navViews, programInfo, programTitle, viewTitle } from "../desktop/programs";
import { programFor, viewFor, type ProgramView } from "../desktop/state";
import { allThemes, resolveTheme, safeCss } from "../desktop/theme";
import type { Language } from "../i18n";
import { PersonaPrompt } from "../instructions/PersonaPrompt";
import { INTENT_EVENT, takeIntent } from "../layout/intent";
import { ModuleView } from "../layout/modules/ModuleView";
import type { ModuleCtx } from "../layout/modules/render";
import { useNarrow } from "../layout/useNarrow";
import { useUi } from "../layout/useUi";
import { workspaceHref } from "../route";
import { LookContext, type LookControls } from "../settings/lookContext";
import { SETTINGS } from "../settings/registry";
import { setShellChoice } from "./shell";
import { ShellFailure, ShellLoading } from "../components/ShellPage";

interface Props {
  api: ApiClient;
  ctx: ModuleCtx;
  /** The program the address names (`#/w/<key>`). */
  routeKey?: string;
  uiTick?: number;
  status: StreamStatus;
  onSaveLanguage: (lang: Language) => void;
  onLogout: () => void;
}

/** Programs from this position on are the system's (Administration, Settings): listed apart. */
const SYSTEM_FROM = 90;

/** The web GUI (the desktop plan, decision 6 and phase 4): classic navigation instead of windows.
 *  A bar on top with the search (the palette, Ctrl+K) and the person's menu, the applications on the
 *  left with the views of the one that is open, one view at a time. The views are the desktop's
 *  pages; only the frame around them differs. */
export function WebShell({ api, ctx, routeKey, uiTick, status, onSaveLanguage, onLogout }: Props) {
  const { t } = useTranslation();
  const ui = useUi(api, uiTick);
  const narrow = useNarrow();
  const [palette, setPalette] = useState(false);
  const [menu, setMenu] = useState(false);
  const [sideOpen, setSideOpen] = useState(false);
  const [intentTick, setIntentTick] = useState(0);
  const [unread, setUnread] = useState<{ n: number; urgent: boolean }>({ n: 0, urgent: false });
  // the address that opened the view was not known: a note above it, until the person moves on
  const [moved, setMoved] = useState(false);
  const burgerRef = useRef<HTMLButtonElement>(null);
  const meRef = useRef<HTMLDivElement>(null);
  const meButtonRef = useRef<HTMLButtonElement>(null);

  const programs = ui.workspaces;
  const offered = ui.boot?.modules;
  const modules = useMemo(() => new Map((offered ?? []).map((m) => [m.id, m])), [offered]);
  const infos = useMemo(() => programs.map((p) => programInfo(p, modules)), [programs, modules]);
  const hasSettings = programs.some((p) => p.key === "settings");

  // the program open and the view each program last showed (this tab only)
  const [current, setCurrent] = useState<string | null>(null);
  const [views, setViews] = useState<Record<string, ProgramView>>({});
  const shownKey =
    current && programs.some((p) => p.key === current)
      ? current
      : routeKey && programs.some((p) => p.key === routeKey)
        ? routeKey
        : (ui.prefs?.desktop?.focus && programs.some((p) => p.key === ui.prefs?.desktop?.focus)
            ? ui.prefs.desktop.focus
            : (programs[0]?.key ?? null));
  const shownInfo = infos.find((p) => p.key === shownKey);
  const view: ProgramView | null = shownKey
    ? (views[shownKey] ?? (shownInfo ? { module: shownInfo.first, props: {} } : null))
    : null;

  const open = useCallback((key: string, v?: ProgramView, note = false) => {
    setCurrent(key);
    setMoved(note);
    if (v) setViews((all) => ({ ...all, [key]: v }));
    setSideOpen(false);
  }, []);

  useEffect(() => {
    let alive = true;
    api.inboxCounts().then(
      (c) => alive && setUnread({ n: c.unread ?? 0, urgent: (c.urgent ?? 0) > 0 }),
      () => undefined,
    );
    return () => {
      alive = false;
    };
  }, [api, ctx.inboxTick]);

  // a link of a page or a window asked for a view (`requestOpen`, an address like #/chat/<id>)
  useEffect(() => {
    const onIntent = () => setIntentTick((n) => n + 1);
    window.addEventListener(INTENT_EVENT, onIntent);
    return () => window.removeEventListener(INTENT_EVENT, onIntent);
  }, []);
  const ready = ui.status === "ready";
  const opening = useRef(false);
  useEffect(() => {
    if (!ready) return;
    const intent = takeIntent();
    if (!intent) return;
    const named = intent.program;
    let key: string | null;
    let v: ProgramView;
    if (named && infos.some((p) => p.key === named)) {
      key = named;
      v = { module: intent.module, props: intent.props };
    } else {
      const direct = programFor(infos, intent.module, shownKey);
      v = direct ? { module: intent.module, props: intent.props } : viewFor(intent.module, intent.props);
      key = direct ?? programFor(infos, v.module, shownKey);
    }
    // shown after this render, as the desktop's preferences arrive after it
    const note = intent.moved === true;
    if (!key) return;
    opening.current = true;
    queueMicrotask(() => {
      opening.current = false;
      open(key, v, note);
      // the address names the program it opens in (the effect below left it alone meanwhile)
      if (window.location.hash !== workspaceHref(key)) window.location.replace(workspaceHref(key));
    });
    // only a new intent opens something
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [ready, intentTick, infos, routeKey]);

  // the address follows the program shown; an address brought in (a bookmark, the back button)
  // opens its program. What the shell wrote itself names the program shown already.
  const [seenRoute, setSeenRoute] = useState(routeKey);
  if (routeKey !== seenRoute) {
    setSeenRoute(routeKey);
    if (routeKey && routeKey !== current && programs.some((p) => p.key === routeKey)) {
      setCurrent(routeKey);
      setMoved(false);
    }
  }
  useEffect(() => {
    // a view asked for is about to open: writing the program shown until then would be read back
    // as an address brought in and open that program again (a deep link at load was lost, #76)
    if (!ready || !shownKey || shownKey === routeKey || opening.current) return;
    window.location.replace(workspaceHref(shownKey));
  }, [ready, shownKey, routeKey]);

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === "k") {
        e.preventDefault();
        setPalette(true);
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, []);

  // the drawer and the user menu close on Escape and on a tap outside; the focus goes back to the
  // button that opened them, unless the tap gave it to something else
  const closeSide = useCallback(() => {
    setSideOpen(false);
    burgerRef.current?.focus();
  }, []);
  useEffect(() => {
    if (!menu && !sideOpen) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key !== "Escape" || e.defaultPrevented) return;
      if (menu) {
        setMenu(false);
        meButtonRef.current?.focus();
      } else closeSide();
    };
    const onClick = (e: MouseEvent) => {
      if (!menu || meRef.current?.contains(e.target as Node)) return;
      setMenu(false);
      const at = document.activeElement;
      if (!at || at === document.body || meRef.current?.contains(at)) meButtonRef.current?.focus();
    };
    document.addEventListener("keydown", onKey);
    document.addEventListener("click", onClick);
    return () => {
      document.removeEventListener("keydown", onKey);
      document.removeEventListener("click", onClick);
    };
  }, [menu, sideOpen, closeSide]);

  const openSettings = useCallback(
    (props: ProgramView["props"] = {}) => open("settings", { module: "page.settings", props }),
    [open],
  );

  const entries = useMemo<PaletteEntry[]>(() => {
    const out: PaletteEntry[] = [];
    for (const p of programs) {
      const title = programTitle(p, t);
      out.push({ id: `p-${p.key}`, label: title, hint: t("desktop.program"), run: () => open(p.key) });
      for (const m of navViews(p, modules)) {
        out.push({
          id: `v-${p.key}-${m}`,
          label: viewTitle(m, t),
          hint: title,
          run: () => open(p.key, { module: m, props: {} }),
        });
      }
    }
    if (hasSettings) {
      for (const e of SETTINGS) {
        if (e.section === "client") continue;
        out.push({
          id: `s-${e.id}`,
          label: t(`settingsApp.item.${e.id}`),
          hint: `${t("workspace.settings")} › ${t(`settingsApp.section.${e.section}`)}`,
          words: e.words,
          quiet: true,
          run: () => openSettings({ section: e.section, focus: e.id }),
        });
      }
    }
    out.push({ id: "a-logout", label: t("header.logout"), hint: t("desktop.action"), run: onLogout });
    return out;
  }, [programs, modules, hasSettings, t, open, openSettings, onLogout]);

  if (ui.status === "loading" && !ui.boot) return <ShellLoading />;
  if (!ui.boot) return <ShellFailure unreachable={ui.unreachable} detail={ui.error} onRetry={ui.reload} />;

  const customThemes = ui.boot.themes ?? [];
  const theme = resolveTheme(ui.prefs?.look, customThemes);
  const hue = ui.prefs?.hue ?? 140;
  const css = safeCss(theme);
  const look: LookControls = {
    theme,
    themes: allThemes(customThemes),
    hue,
    onTheme: (l) => void ui.setPrefs({ look: l }),
    onHue: (h) => void ui.setPrefs({ hue: h }, { lazy: true }),
    onSaveLanguage,
  };
  const apps = programs.filter((p) => p.position < SYSTEM_FROM);
  const system = programs.filter((p) => p.position >= SYSTEM_FROM);
  const shown = programs.find((p) => p.key === shownKey);
  const inbox = programs.find((p) => p.modules.includes("page.inbox"));

  // the inbox has its own place on top; the program that holds it lists its other views
  const ownViews = (p: (typeof programs)[number]) =>
    navViews(p, modules).filter((m) => !(inbox && m === "page.inbox"));
  const programLink = (p: (typeof programs)[number]) => {
    const here = p.key === shownKey;
    const sub = here ? ownViews(p) : [];
    const first = ownViews(p)[0];
    return (
      <li key={p.key}>
        <button
          type="button"
          className={here ? "ws-nav-program active" : "ws-nav-program"}
          aria-current={here && sub.length <= 1 ? "page" : undefined}
          onClick={() => open(p.key, p === inbox && first ? { module: first, props: {} } : undefined)}
        >
          <span className="ws-nav-icon" aria-hidden="true">
            {p.icon}
          </span>{" "}
          {programTitle(p, t)}
        </button>
        {sub.length > 1 && (
          <ul className="ws-nav-views">
            {sub.map((m) => (
              <li key={m}>
                <button
                  type="button"
                  className={view?.module === m ? "active" : undefined}
                  aria-current={view?.module === m ? "page" : undefined}
                  onClick={() => open(p.key, { module: m, props: {} })}
                >
                  {viewTitle(m, t)}
                </button>
              </li>
            ))}
          </ul>
        )}
      </li>
    );
  };

  return (
    <LookContext.Provider value={look}>
      <div
        className={`dt-root ws-shell dt-look-${theme.base ?? "terminal"} dt-theme-${theme.key}${narrow ? " ws-narrow" : ""}`}
        style={{ ["--ws-hue" as string]: String(hue), ["--ws-sat" as string]: "70%" }}
      >
        {css && <style data-testid="theme-css">{css}</style>}
        <header className="ws-top">
          {narrow && (
            <button
              ref={burgerRef}
              type="button"
              className="ws-burger"
              aria-expanded={sideOpen}
              aria-label={t("webShell.menu")}
              onClick={() => setSideOpen((o) => !o)}
            >
              ☰
            </button>
          )}
          <span className="ws-logo">topcoat</span>
          <button
            type="button"
            className="ws-search"
            aria-label={t("desktop.paletteSearch")}
            onClick={() => setPalette(true)}
          >
            <span className="ws-search-label">
              <span className="ws-search-icon" aria-hidden="true">
                ⌕
              </span>{" "}
              <span className="ws-search-text">{t("desktop.paletteSearch")}</span>
            </span>
            <kbd aria-hidden="true">Ctrl+K</kbd>
          </button>
          <span className={`conn conn-${status}`} role="status">
            <span className="conn-dot" aria-hidden="true" />
            <span className="conn-text">{t(`connection.${status}`)}</span>
          </span>
          <div className="ws-me" ref={meRef}>
            <button
              ref={meButtonRef}
              type="button"
              className="ws-me-button"
              aria-haspopup="menu"
              aria-expanded={menu}
              onClick={() => setMenu((m) => !m)}
            >
              <span className="ws-me-name">{ctx.me?.name ?? t("desktop.me")}</span>
              <span aria-hidden="true">▾</span>
            </button>
            {menu && (
              <div className="dt-me-menu ws-me-menu" role="menu" aria-label={t("desktop.me")}>
                {hasSettings && (
                  <button
                    type="button"
                    role="menuitem"
                    onClick={() => {
                      setMenu(false);
                      openSettings();
                    }}
                  >
                    {t("desktop.openSettings")}
                  </button>
                )}
                <LanguageSwitch onSave={onSaveLanguage} />
                <button type="button" role="menuitem" onClick={() => setShellChoice("windows")}>
                  {t("webShell.toWindows")}
                </button>
                <button type="button" role="menuitem" onClick={onLogout}>
                  {t("header.logout")}
                </button>
              </div>
            )}
          </div>
        </header>
        {ui.error && (
          <p className="bad dt-banner" role="alert">
            {ui.error}
          </p>
        )}
        <PersonaPrompt api={api} quiet />
        <div className="ws-body">
          {narrow && sideOpen && <div className="ws-backdrop" aria-hidden="true" onClick={closeSide} />}
          <nav
            className={`ws-side${narrow && !sideOpen ? " ws-side-closed" : ""}`}
            aria-label={t("webShell.navigation")}
          >
            {inbox && (
              <button
                type="button"
                className={
                  shownKey === inbox.key && view?.module === "page.inbox" ? "ws-nav-inbox active" : "ws-nav-inbox"
                }
                onClick={() => open(inbox.key, { module: "page.inbox", props: {} })}
              >
                {t("inbox.title")}
                {unread.n > 0 && (
                  <span className={unread.urgent ? "nav-badge ws-badge-urgent" : "nav-badge"}>{unread.n}</span>
                )}
              </button>
            )}
            <p className="ws-nav-head">{t("webShell.applications")}</p>
            <ul className="ws-nav-list">{apps.map(programLink)}</ul>
            {system.length > 0 && (
              <>
                <p className="ws-nav-head">{t("webShell.system")}</p>
                <ul className="ws-nav-list">{system.map(programLink)}</ul>
              </>
            )}
          </nav>
          <main className="ws-main">
            {shown && view && shownInfo ? (
              <>
                <div className="ws-crumb">
                  <span>
                    {programTitle(shown, t)}
                    {navViews(shown, modules).length > 1 && <> › {viewTitle(view.module, t)}</>}
                  </span>
                  {hasSettings && (APPLICATIONS as readonly string[]).includes(shown.key) && (
                    <button
                      type="button"
                      className="linklike"
                      onClick={() => openSettings({ section: "apps", app: shown.key })}
                    >
                      ⚙ {t("settingsApp.open", { app: programTitle(shown, t) })}
                    </button>
                  )}
                </div>
                {moved && (
                  <p className="ws-moved" role="status">
                    {t("webShell.moved")}
                  </p>
                )}
                <ModuleView
                  key={`${shown.key}:${view.module}:${JSON.stringify(view.props)}`}
                  win={{ id: `${shown.key}:${view.module}`, module: view.module, docked: false, props: view.props }}
                  ctx={ctx}
                  winCtx={{ workspace: shown.key, boot: ui.boot }}
                  info={modules.get(view.module)}
                />
              </>
            ) : (
              <p className="muted">{t("desktop.empty")}</p>
            )}
          </main>
        </div>
        {palette && <CommandPalette entries={entries} onClose={() => setPalette(false)} />}
      </div>
    </LookContext.Provider>
  );
}
