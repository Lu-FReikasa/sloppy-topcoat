import { Component, type ErrorInfo, type ReactNode } from "react";
import { useTranslation } from "react-i18next";
import type { LayoutWindow, UiModuleInfo } from "../types";
import { moduleMeta } from "./meta";
import { renderModule, type ModuleCtx, type WindowCtx } from "./render";

interface BoundaryProps {
  label: string;
  children: ReactNode;
  retryLabel: string;
  failedLabel: string;
}

/** One page that fails must not take the whole workspace down with it. */
class Boundary extends Component<BoundaryProps, { error: Error | null }> {
  state = { error: null as Error | null };

  static getDerivedStateFromError(error: Error) {
    return { error };
  }

  componentDidCatch(error: Error, info: ErrorInfo) {
    console.error(`window ${this.props.label} failed:`, error, info.componentStack);
  }

  render() {
    if (!this.state.error) return this.props.children;
    return (
      <div className="ws-failed" role="alert">
        <p className="bad">{this.props.failedLabel}</p>
        <p className="muted small">{this.state.error.message}</p>
        <button type="button" onClick={() => this.setState({ error: null })}>
          {this.props.retryLabel}
        </button>
      </div>
    );
  }
}

interface Props {
  win: LayoutWindow;
  ctx: ModuleCtx;
  winCtx: WindowCtx;
  info?: UiModuleInfo;
}

/** The content of one window: the module, or the reason there is none. */
export function ModuleView({ win, ctx, winCtx, info }: Props) {
  const { t } = useTranslation();
  const meta = moduleMeta(win.module);
  if (!meta) {
    return (
      <p className="muted ws-note" role="status">
        {t("layout.unknownModule", { module: win.module })}
      </p>
    );
  }
  const title = t(meta.titleKey);
  if (info && !info.allowed) {
    return (
      <p className="bad ws-note" role="status">
        ⊘ {t("layout.blocked", { module: title })}
      </p>
    );
  }
  return (
    <Boundary label={win.id} retryLabel={t("layout.retry")} failedLabel={t("layout.failed", { module: title })}>
      <div className="ws-page">{renderModule(win.module, ctx, win.props ?? {}, winCtx)}</div>
    </Boundary>
  );
}

