// The pages of the trusted shell that stand before any look is loaded: sign-in, loading and the
// failure page (ARCHITECTURE D113: outside the themes' reach). They have a fixed dark style of their
// own (.shell-page in styles.css), so they neither follow the system's light scheme nor depend on a
// look's variables that are not there yet.
import { useState, type ReactNode } from "react";
import { useTranslation } from "react-i18next";

export function ShellPage({ children, busy = false }: { children: ReactNode; busy?: boolean }) {
  return (
    <main className="shell-page" aria-busy={busy || undefined}>
      <span className="shell-mark" aria-hidden="true">
        topcoat
      </span>
      {children}
    </main>
  );
}

export function ShellLoading() {
  const { t } = useTranslation();
  return (
    <ShellPage busy>
      <p className="shell-loading" role="status">
        {t("layout.loading")}
      </p>
    </ShellPage>
  );
}

interface FailureProps {
  /** The hub did not answer at all (errors.ts, hubUnreachable). */
  unreachable: boolean;
  /** The technical reason, shown small under the actions. */
  detail?: string | null;
  onRetry: () => Promise<unknown> | void;
}

/** The workspaces could not be loaded: says why in words, offers one action, keeps the detail
 *  small. The page also comes back by itself: the stream's return reloads the workspaces. */
export function ShellFailure({ unreachable, detail, onRetry }: FailureProps) {
  const { t } = useTranslation();
  const [trying, setTrying] = useState(false);
  const retry = async () => {
    setTrying(true);
    try {
      await onRetry();
    } finally {
      setTrying(false);
    }
  };
  return (
    <ShellPage>
      <section className="shell-card" role="alert" aria-labelledby="shell-failure-title">
        <h1 id="shell-failure-title">{t(unreachable ? "shell.unreachable" : "shell.failed")}</h1>
        <p>{t(unreachable ? "shell.unreachableHint" : "shell.failedHint")}</p>
        <button type="button" className="primary" onClick={() => void retry()} disabled={trying}>
          {t(trying ? "shell.retrying" : "layout.retry")}
        </button>
        {detail && <p className="shell-detail">{t("shell.details", { detail })}</p>}
      </section>
    </ShellPage>
  );
}
