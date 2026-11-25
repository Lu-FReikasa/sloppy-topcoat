import { useEffect, useMemo, useRef, useState } from "react";
import { useTranslation } from "react-i18next";
import type { ApiClient } from "../api/client";
import { APPLICATIONS } from "../chat/applications";
import { ChatSettings } from "../chat/ChatSettings";
import { LanguageSwitch } from "../components/LanguageSwitch";
import { ClientSettings } from "../desktop/ClientSettings";
import { clientConfig } from "../desktop/client";
import { ProgramSettings } from "../desktop/ProgramSettings";
import { programTitle } from "../desktop/programs";
import { isBuiltin } from "../desktop/theme";
import { InstructionsPage } from "../instructions/InstructionsPage";
import type { Workspace } from "../layout/types";
import { MemoryPage } from "../memory/MemoryPage";
import { inClient, setShellChoice, useWantsNav } from "../webshell/shell";
import { useLook } from "./lookContext";
import { SECTIONS, SETTINGS, isSection, searchSettings, type SectionKey } from "./registry";

interface Props {
  api: ApiClient;
  /** The applications the person has (the programs of the bootstrap); Settings › Applications
   *  lists those that have an assistant. */
  workspaces: readonly Workspace[];
  /** Where to open: a section, an application of "apps", an option to jump to. */
  section?: string;
  app?: string;
  focus?: string;
}

/** Settings as one application (the desktop plan, phase 4): six sections, a search over every
 *  option, a jump to one (`focus`, from the search or the palette). The pages it is made of are the
 *  ones that existed before, each showing only its part here. A jump from outside (the ⚙ of a
 *  window, the palette) mounts it anew (render.tsx keys it by section, app and focus). */
export function SettingsApp({ api, workspaces, section, app, focus }: Props) {
  const { t } = useTranslation();
  const inClient = clientConfig() !== null;
  const sections = SECTIONS.filter((s) => s !== "client" || inClient);
  const apps = useMemo(
    () => workspaces.filter((w) => (APPLICATIONS as readonly string[]).includes(w.key)),
    [workspaces],
  );
  const [shown, setShown] = useState<SectionKey>(() => (isSection(section) ? section : "general"));
  const [shownApp, setShownApp] = useState<string | null>(app ?? null);
  // the option to jump to; a new object each time, so the same option can be asked for twice
  const [target, setTarget] = useState<{ id: string } | null>(focus ? { id: focus } : null);
  const handled = useRef<{ id: string } | null>(null);
  const [query, setQuery] = useState("");
  const body = useRef<HTMLDivElement>(null);

  // the option asked for: open what hides it, scroll to it, mark it a moment
  useEffect(() => {
    if (!target || handled.current === target) return;
    let tries = 0;
    let timer = 0;
    const look = () => {
      const el = body.current?.querySelector<HTMLElement>(`[data-setting="${target.id}"]`);
      if (!el) {
        // the section loads its data first
        if (tries++ < 20) timer = window.setTimeout(look, 100);
        return;
      }
      for (let d: HTMLElement | null = el; d; d = d.parentElement) if (d instanceof HTMLDetailsElement) d.open = true;
      el.scrollIntoView?.({ block: "center" });
      el.classList.add("setting-flash");
      timer = window.setTimeout(() => el.classList.remove("setting-flash"), 1600);
      handled.current = target;
    };
    look();
    return () => window.clearTimeout(timer);
  }, [target, shown, shownApp]);

  const found = useMemo(
    () => searchSettings(query, t).filter((e) => sections.includes(e.section)),
    [query, t, sections],
  );
  const go = (id: string) => {
    const entry = SETTINGS.find((e) => e.id === id);
    if (!entry) return;
    setQuery("");
    setShown(entry.section);
    setTarget({ id });
  };
  const currentApp = shownApp && apps.some((w) => w.key === shownApp) ? shownApp : (apps[0]?.key ?? null);

  return (
    <div className="settings-app">
      <nav className="settings-nav" aria-label={t("settingsApp.nav")}>
        {sections.map((s) => (
          <button
            key={s}
            type="button"
            aria-current={shown === s ? "page" : undefined}
            className={shown === s ? "active" : undefined}
            onClick={() => {
              setShown(s);
              setQuery("");
            }}
          >
            {t(`settingsApp.section.${s}`)}
          </button>
        ))}
      </nav>
      <div className="settings-body" ref={body}>
        <div className="settings-search">
          <input
            type="search"
            value={query}
            aria-label={t("settingsApp.search")}
            placeholder={t("settingsApp.search")}
            onChange={(e) => setQuery(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === "Enter" && found[0]) go(found[0].id);
            }}
          />
          {query.trim() && (
            <ul className="settings-found" aria-label={t("settingsApp.found")}>
              {found.map((e) => (
                <li key={e.id}>
                  <button type="button" className="linklike" onClick={() => go(e.id)}>
                    {t(`settingsApp.item.${e.id}`)}
                  </button>{" "}
                  <span className="muted small">{t(`settingsApp.section.${e.section}`)}</span>
                </li>
              ))}
              {found.length === 0 && <li className="muted">{t("settingsApp.nothing")}</li>}
            </ul>
          )}
        </div>
        <h1>{t(`settingsApp.section.${shown}`)}</h1>
        <p className="muted small">{t(`settingsApp.intro.${shown}`)}</p>
        {shown === "general" && <General api={api} />}
        {shown === "client" && <ClientSettings />}
        {shown === "assistant" && (
          <>
            <InstructionsPage api={api} parts={["proposals"]} />
            <ChatSettings api={api} part="assistant" />
            <InstructionsPage api={api} parts={["persona", "rules"]} />
            <details className="settings-advanced card" data-setting="assistant-core">
              <summary>{t("settingsApp.core")}</summary>
              <p className="muted small">{t("settingsApp.coreHint")}</p>
              <InstructionsPage api={api} parts={["recent", "preview"]} />
            </details>
          </>
        )}
        {shown === "apps" && (
          <div data-setting="apps">
            {apps.length === 0 ? (
              <p className="muted">{t("settingsApp.noApps")}</p>
            ) : (
              <>
                <div className="settings-apps" role="tablist" aria-label={t("settingsApp.section.apps")}>
                  {apps.map((w) => (
                    <button
                      key={w.key}
                      type="button"
                      role="tab"
                      aria-selected={currentApp === w.key}
                      className={currentApp === w.key ? "primary" : undefined}
                      onClick={() => setShownApp(w.key)}
                    >
                      {programTitle(w, t)}
                    </button>
                  ))}
                </div>
                {currentApp && (
                  <div key={currentApp}>
                    <ProgramSettings api={api} workspace={currentApp} />
                    <MemoryPage api={api} application={currentApp} />
                  </div>
                )}
              </>
            )}
          </div>
        )}
        {shown === "memory" && (
          <>
            <ChatSettings api={api} part="memory" />
            <div data-setting="memory-notes">
              <MemoryPage api={api} application="settings" />
            </div>
          </>
        )}
        {shown === "household" && <InstructionsPage api={api} parts={["household"]} />}
      </div>
    </div>
  );
}

/** Language, look, hue and the chat list: what used to be in the name menu and on top of "Me". */
function General({ api }: { api: ApiClient }) {
  const { t } = useTranslation();
  const look = useLook();
  return (
    <>
      <section className="card" data-setting="language">
        <h2>{t("settingsApp.item.language")}</h2>
        <LanguageSwitch onSave={look?.onSaveLanguage} />
        <p className="muted small">{t("settingsApp.languageHint")}</p>
      </section>
      {look && (
        <section className="card form">
          <h2>{t("settingsApp.appearance")}</h2>
          <label data-setting="look">
            <span>{t("desktop.look")}</span>
            <select value={look.theme.key} onChange={(e) => look.onTheme(e.target.value)}>
              {look.themes.map((th) => (
                <option key={th.key} value={th.key}>
                  {isBuiltin(th.key) ? t(`desktop.looks.${th.key}`) : th.name}
                </option>
              ))}
            </select>
          </label>
          <label data-setting="hue">
            <span>{t("desktop.hue")}</span>
            <input
              type="range"
              min={0}
              max={359}
              value={look.hue}
              onChange={(e) => look.onHue(Number(e.target.value))}
            />
          </label>
        </section>
      )}
      {!inClient() && <ShellSwitch />}
      <ChatSettings api={api} part="chatlist" />
    </>
  );
}

/** In a browser: the classic navigation or the window desktop (kept in this browser). */
function ShellSwitch() {
  const { t } = useTranslation();
  const nav = useWantsNav();
  return (
    <section className="card" data-setting="shell">
      <h2>{t("settingsApp.item.shell")}</h2>
      <div className="row" role="radiogroup" aria-label={t("settingsApp.item.shell")}>
        <label className="check">
          <input type="radio" name="shell" checked={nav} onChange={() => setShellChoice("nav")} />
          {t("settingsApp.shellNav")}
        </label>{" "}
        <label className="check">
          <input type="radio" name="shell" checked={!nav} onChange={() => setShellChoice("windows")} />
          {t("settingsApp.shellWindows")}
        </label>
      </div>
      <p className="muted small">{t("settingsApp.shellHint")}</p>
    </section>
  );
}
