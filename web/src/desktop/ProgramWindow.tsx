import { OpenChatsList } from "../chat/OpenChatsList";
import { memo, useRef, type CSSProperties, type PointerEvent as ReactPointerEvent, type ReactNode } from "react";
import { useTranslation } from "react-i18next";
import { ModuleView } from "../layout/modules/ModuleView";
import type { ModuleCtx } from "../layout/modules/render";
import type { UiBootstrap, UiModuleInfo, Workspace } from "../layout/types";
import { navViews, programCode, programHue, programPaper, programTilt, programTitle, viewTitle } from "./programs";
import { GRIPS, type Box, type Grip } from "./floating";
import type { ProgramView } from "./state";
import type { ChromeButton, ThemeManifest } from "./theme";
import { sameBoot, sameCtx } from "./redraw";

interface Props {
  program: Workspace;
  view: ProgramView;
  focused: boolean;
  maximised: boolean;
  modules: Map<string, UiModuleInfo>;
  ctx: ModuleCtx;
  boot: UiBootstrap | null;
  chrome: ThemeManifest["chrome"];
  style: CSSProperties;
  onFocus: () => void;
  onView: (module: string) => void;
  onMinimise: () => void;
  onMaximise: () => void;
  onClose: () => void;
  /** Opens the application's own settings (its frame and tools), where it has them. */
  onSettings?: () => void;
  /** The overlay's free windows: moved by the title bar, sized at the edges, pinned to the normal
   *  desktop (a native window of the desktop client). */
  free?: {
    onGrab: (e: ReactPointerEvent<HTMLElement>, grip: Grip) => void;
    onPin: (el: HTMLElement) => void;
    /** To the next screen (one overlay window per screen): closed here, opened there. */
    onSend?: () => void;
  };
  /** The window is tiled: its screen, its place in the group and the split after it. */
  tile?: TileInfo;
  /** The window is on its way out (closed or minimised): the theme's leaving plays. */
  closing?: boolean;
}

/** Where a tiled window stands (Desktop.tsx): written to the element for the drag handler. */
export interface TileInfo {
  screen: number;
  index: number;
  /** The split after this window: side by side ("v") or above each other ("h"); the last has none. */
  split: "v" | "h" | null;
  /** The area that split divides. */
  area: Box | null;
}

/** The handlers of one program's window, made once per program (Desktop.tsx). */
export type WindowHandlers = Pick<Props, "onFocus" | "onView" | "onMinimise" | "onMaximise" | "onClose" | "onSettings"> & {
  free: NonNullable<Props["free"]>;
};

/** One program: its title bar, its navigation and the view it shows (docs/DESKTOP.md). */
function ProgramWindowView({
  program,
  view,
  focused,
  maximised,
  modules,
  ctx,
  boot,
  chrome,
  style,
  onFocus,
  onView,
  onMinimise,
  onMaximise,
  onClose,
  onSettings,
  free,
  tile,
  closing = false,
}: Props) {
  const { t } = useTranslation();
  const self = useRef<HTMLElement>(null);
  const title = programTitle(program, t);
  const views = navViews(program, modules);
  const buttons: Record<ChromeButton, ReactNode> = {
    minimise: (
      <button key="minimise" type="button" className="dt-minimise" onClick={onMinimise} aria-label={t("desktop.minimise", { program: title })}>
        _
      </button>
    ),
    maximise: (
      <button
        key="maximise"
        type="button"
        className="dt-maximise"
        onClick={onMaximise}
        aria-pressed={maximised}
        aria-label={t(maximised ? "desktop.restore" : "desktop.maximise", {
          program: title,
        })}
      >
        {maximised ? "▭" : "□"}
      </button>
    ),
    close: (
      <button key="close" type="button" className="dt-close" onClick={onClose} aria-label={t("desktop.close", { program: title })}>
        ×
      </button>
    ),
  };
  return (
    <section
      ref={self}
      className={`dt-program${focused ? " dt-focused" : ""}${maximised ? " dt-maxi" : ""}${closing ? " dt-closing" : ""}`}
      style={{
        ...style,
        ["--pg-hue" as string]: String(programHue(program.key)),
        // the desk look: how the slip lies, and the tape that holds it
        ["--rot" as string]: `${programTilt(program.key)}deg`,
        ["--tape" as string]: `${-programTilt(program.key) * 5}deg`,
      }}
      aria-label={title}
      data-program={program.key}
      data-tile-screen={tile ? tile.screen : undefined}
      data-tile-index={tile ? tile.index : undefined}
      data-tile-split={tile?.split ?? undefined}
      data-tile-area={tile?.area ? [tile.area.x, tile.area.y, tile.area.w, tile.area.h].join(",") : undefined}
      data-paper={programPaper(program.key)}
      onPointerDownCapture={focused ? undefined : onFocus}
      onFocusCapture={focused ? undefined : onFocus}
    >
      {chrome.titlebar === "top" && (
        <header
          className="dt-titlebar"
          onDoubleClick={onMaximise}
          onPointerDown={
            free && !maximised
              ? (e) => {
                  if (!(e.target as HTMLElement).closest("button")) free.onGrab(e, "move");
                }
              : undefined
          }
        >
          <span className="dt-lamp" aria-hidden="true" />
          <span className="dt-code" aria-hidden="true">
            {programCode(program.key)}
          </span>
          <span className="dt-title">
            <span className="dt-icon" aria-hidden="true">
              {program.icon}
            </span>{" "}
            {title}
          </span>
          <span className="dt-titlebar-buttons">
            {onSettings && (
              <button
                key="settings"
                type="button"
                className="dt-settings"
                onClick={onSettings}
                aria-label={t("desktop.settings", { program: title })}
                title={t("desktop.settings", { program: title })}
              >
                ⚙
              </button>
            )}
            {free && (
              <button
                key="pin"
                type="button"
                className="dt-pin"
                onClick={() => self.current && free.onPin(self.current)}
                aria-label={t("desktop.pin", { program: title })}
                title={t("desktop.pin", { program: title })}
              >
                📌
              </button>
            )}
            {free?.onSend && (
              <button
                key="send"
                type="button"
                className="dt-send"
                onClick={free.onSend}
                aria-label={t("desktop.send", { program: title })}
                title={t("desktop.send", { program: title })}
              >
                ⇄
              </button>
            )}
            {chrome.buttons.map((b) => buttons[b])}
          </span>
        </header>
      )}
      {free &&
        !maximised &&
        // a tiled window is sized only at its divider; a free one at every edge
        (tile ? (tile.split ? [tile.split === "v" ? ("e" as const) : ("s" as const)] : []) : GRIPS).map((g) => (
          <span key={g} className={`dt-grip dt-grip-${g}`} aria-hidden="true" onPointerDown={(e) => free.onGrab(e, g)} />
        ))}
      <div className="dt-program-body">
        {views.length > 1 && (
          <nav className="dt-nav" aria-label={t("desktop.views", { program: title })}>
            {views.map((id) => (
              <button
                key={id}
                type="button"
                aria-current={view.module === id ? "page" : undefined}
                onClick={() => onView(id)}
              >
                {viewTitle(id, t)}
              </button>
            ))}
            {program.modules.includes("page.chat") && (
              <OpenChatsList
                api={ctx.api}
                program={program.key}
                current={view.module === "page.chat" && typeof view.props.id === "string" ? view.props.id : undefined}
                tick={ctx.tick}
              />
            )}
          </nav>
        )}
        <div className="dt-view">
          <ModuleView
            // a link to another tab or record of the same view starts it afresh on that one
            key={`${view.module}:${JSON.stringify(view.props)}`}
            win={{
              id: `${program.key}:${view.module}`,
              module: view.module,
              docked: false,
              props: view.props,
            }}
            ctx={ctx}
            winCtx={{ workspace: program.key, boot }}
            info={modules.get(view.module)}
          />
        </div>
      </div>
    </section>
  );
}

const sameView = (a: ProgramView, b: ProgramView) =>
  a === b || (a.module === b.module && JSON.stringify(a.props) === JSON.stringify(b.props));

// `--talk-share`: the room the window gives Clipper's folded-out talk (talkPlace.ts)
const STYLE_KEYS = ["left", "top", "width", "height", "zIndex", "--talk-share"] as const;

/** Draws again only for what this window shows (redraw.ts); the handlers are stable per program
 *  (Desktop.tsx), so they are compared as they are. */
export function samePropsForWindow(a: Props, b: Props): boolean {
  return (
    a.program === b.program &&
    sameView(a.view, b.view) &&
    a.focused === b.focused &&
    a.maximised === b.maximised &&
    !!a.closing === !!b.closing &&
    a.modules === b.modules &&
    a.chrome === b.chrome &&
    STYLE_KEYS.every((k) => (a.style as Record<string, unknown>)[k] === (b.style as Record<string, unknown>)[k]) &&
    a.onFocus === b.onFocus &&
    a.onView === b.onView &&
    a.onMinimise === b.onMinimise &&
    a.onMaximise === b.onMaximise &&
    a.onClose === b.onClose &&
    a.onSettings === b.onSettings &&
    a.free === b.free &&
    JSON.stringify(a.tile ?? null) === JSON.stringify(b.tile ?? null) &&
    sameCtx(a.view.module, a.ctx, b.ctx) &&
    sameBoot(a.view.module, a.boot, b.boot)
  );
}

export const ProgramWindow = memo(ProgramWindowView, samePropsForWindow);
