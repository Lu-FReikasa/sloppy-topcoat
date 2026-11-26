import { Fragment, useEffect, useRef, useState, type ReactNode } from "react";
import { useTranslation } from "react-i18next";
import type { StreamStatus } from "../api/sse";
import { LanguageSwitch } from "../components/LanguageSwitch";
import type { Language } from "../i18n";
import type { Workspace } from "../layout/types";
import { barItems, type BarItem, type ThemeManifest } from "./theme";
import type { Me } from "../types";
import { programHue, programTitle } from "./programs";
import type { DesktopState } from "./state";

/** A program's signal on the taskbar: amber = something to read, red = something urgent. */
export type Signal = "amber" | "red";

interface Props {
  programs: Workspace[];
  /** Programs that want attention (the inbox with unread or urgent items). */
  signals?: Record<string, Signal>;
  theme: ThemeManifest;
  state: DesktopState;
  status: StreamStatus;
  me: Me | null;
  onProgram: (key: string) => void;
  onPalette: () => void;
  /** The program the start button opens (Start, on its overview), if there is one; else the
   *  button opens the palette. It is not listed again among the programs. */
  start?: { key: string; open: () => void };
  /** Opens Settings (look and hue live there since phase 4); absent without a Settings program. */
  onSettings?: () => void;
  onSaveLanguage: (lang: Language) => void;
  onLogout: () => void;
}

/** The bar at the bottom (docs/DESKTOP.md): the palette, one button per program, the connection and
 *  the person's menu. There is no menu bar at the top. */
export function Taskbar({
  programs,
  signals = {},
  theme,
  state,
  status,
  me,
  onProgram,
  onPalette,
  start,
  onSettings,
  onSaveLanguage,
  onLogout,
}: Props) {
  const { t } = useTranslation();
  const [menu, setMenu] = useState(false);
  const menuRoot = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (!menu) return;
    const away = (e: Event) => {
      if (menuRoot.current && !menuRoot.current.contains(e.target as Node)) setMenu(false);
    };
    // this Esc is the menu's: the desktop then does not minimise the window in front, and a page
    // (Sheets) does not take it either
    const esc = (e: KeyboardEvent) => {
      if (e.key !== "Escape") return;
      e.preventDefault();
      setMenu(false);
    };
    document.addEventListener("pointerdown", away);
    document.addEventListener("keydown", esc);
    return () => {
      document.removeEventListener("pointerdown", away);
      document.removeEventListener("keydown", esc);
    };
  }, [menu]);

  const part: Record<BarItem, ReactNode> = {
    start: (
      <button
        type="button"
        className={`dt-start${start && state.focus === start.key && state.open.includes(start.key) ? " dt-front" : ""}`}
        onClick={start ? start.open : onPalette}
        title={start ? t("desktop.startHint") : t("desktop.paletteHint")}
        aria-label={start ? t("desktop.start") : t("desktop.palette")}
      >
        <span aria-hidden="true">tc</span> <span className="dt-label">{start ? t("desktop.start") : t("desktop.palette")}</span>
      </button>
    ),
    programs: (
      <nav className="dt-tasks" aria-label={t("desktop.programs")}>
        {programs.filter((p) => p.key !== start?.key).map((p, i) => {
          const open = state.open.includes(p.key);
          const minimised = state.minimised.includes(p.key);
          const front = open && !minimised && state.focus === p.key;
          const signal = signals[p.key];
          return (
            <button
              key={p.key}
              type="button"
              className={`dt-task${open ? " dt-open" : ""}${minimised ? " dt-min" : ""}${front ? " dt-front" : ""}${signal ? ` dt-signal-${signal}` : ""}`}
              style={{ ["--pg-hue" as string]: String(programHue(p.key)) }}
              aria-pressed={front}
              aria-label={programTitle(p, t)}
              title={i < 9 ? `${programTitle(p, t)} (${t("desktop.altKey", { n: i + 1 })})` : programTitle(p, t)}
              onClick={() => onProgram(p.key)}
            >
              <span className="dt-icon" aria-hidden="true">
                {p.icon}
              </span>{" "}
              <span className="dt-label">{programTitle(p, t)}</span>
              {signal && <span className="dt-signal" aria-label={t(`desktop.signal.${signal}`)} />}
            </button>
          );
        })}
      </nav>
    ),
    clock: <MissionClock zone={me?.effective_timezone} />,
    status: (
      <span className={`conn conn-${status}`} role="status">
        <span className="conn-dot" aria-hidden="true" />
        <span className="conn-text">{t(`connection.${status}`)}</span>
      </span>
    ),
    me: (
      <div className="dt-me" ref={menuRoot}>
        <button type="button" aria-haspopup="menu" aria-expanded={menu} onClick={() => setMenu((m) => !m)}>
          {me?.name ?? t("desktop.me")}
        </button>
        {menu && (
          <div className="dt-me-menu" role="menu" aria-label={t("desktop.me")}>
            {onSettings && (
              <button
                type="button"
                role="menuitem"
                onClick={() => {
                  setMenu(false);
                  onSettings();
                }}
              >
                {t("desktop.openSettings")}
              </button>
            )}
            <LanguageSwitch onSave={onSaveLanguage} />
            <button type="button" role="menuitem" onClick={onLogout}>
              {t("header.logout")}
            </button>
          </div>
        )}
      </div>
    ),
  };

  return (
    <footer className="dt-taskbar">
      {barItems(theme).map((item) => (
        <Fragment key={item}>{part[item]}</Fragment>
      ))}
    </footer>
  );
}

/** The clock of the taskbar, to the second, in the person's time zone (their own, else the hub's;
 *  Berlin when neither is known). It showed universal time before 2026-11-13. */
export function MissionClock({ zone }: { zone?: string }) {
  const [now, setNow] = useState(() => new Date());
  useEffect(() => {
    const timer = setInterval(() => setNow(new Date()), 1000);
    return () => clearInterval(timer);
  }, []);
  return (
    <span className="dt-clock" data-testid="mission-clock">
      {clockText(now, zone ?? DEFAULT_ZONE)}
    </span>
  );
}

const DEFAULT_ZONE = "Europe/Berlin";

/** hh:mm:ss in `zone` (a wrong zone name falls back to Berlin). */
export function clockText(now: Date, zone: string): string {
  const fmt = (z: string) =>
    new Intl.DateTimeFormat("de-DE", { timeZone: z, hour: "2-digit", minute: "2-digit", second: "2-digit", hour12: false }).format(now);
  try {
    return fmt(zone);
  } catch {
    return fmt(DEFAULT_ZONE);
  }
}
