import { useEffect, useLayoutEffect, useRef, type KeyboardEvent, type PointerEvent, type RefObject } from "react";
import { useTranslation } from "react-i18next";
import type { PushToTalkHandle } from "./usePushToTalk";

/** The page key: hold Ctrl+Space (works while typing in the composer). */
export function isTalkKey(e: { code: string; ctrlKey: boolean; altKey: boolean; metaKey: boolean }): boolean {
  return e.code === "Space" && e.ctrlKey && !e.altKey && !e.metaKey;
}

/** Whether the page's keys belong to it: in the desktop shell only the window in front takes them,
 *  and never while a dialog is open. */
function inFront(el: HTMLElement | null): boolean {
  if (!el || document.querySelector('[aria-modal="true"]')) return false;
  const win = el.closest(".dt-program");
  return !win || win.classList.contains("dt-focused");
}

/** The microphone key with the voice orb in it: hold it (or Ctrl+Space) to talk, let go to send.
 *  The orb shows listening, thinking and speaking; a status line says it for screen readers. */
export function PushToTalkButton({
  voice,
  scope,
  disabled,
}: {
  voice: PushToTalkHandle;
  /** the page the key belongs to (the window in front takes it) */
  scope: RefObject<HTMLElement | null>;
  disabled?: boolean;
}) {
  const { t } = useTranslation();
  const byKey = useRef(false);
  const live = useRef(voice);
  useLayoutEffect(() => {
    live.current = voice;
  });

  useEffect(() => {
    function down(e: globalThis.KeyboardEvent) {
      if (!isTalkKey(e) || !inFront(scope.current)) return;
      e.preventDefault();
      if (e.repeat || byKey.current) return;
      byKey.current = true;
      live.current.press();
    }
    function up(e: globalThis.KeyboardEvent) {
      if (!byKey.current || (e.code !== "Space" && e.key !== "Control")) return;
      e.preventDefault();
      byKey.current = false;
      live.current.release();
    }
    function lost() {
      // the window lost the keys (Alt+Tab while holding): what was said so far is sent
      if (!byKey.current) return;
      byKey.current = false;
      live.current.release();
    }
    function esc(e: globalThis.KeyboardEvent) {
      if (e.key !== "Escape" || live.current.state === "idle" || !inFront(scope.current)) return;
      e.preventDefault();
      live.current.cancel();
    }
    window.addEventListener("keydown", down);
    window.addEventListener("keyup", up);
    window.addEventListener("keydown", esc);
    window.addEventListener("blur", lost);
    return () => {
      window.removeEventListener("keydown", down);
      window.removeEventListener("keyup", up);
      window.removeEventListener("keydown", esc);
      window.removeEventListener("blur", lost);
    };
  }, [scope]);

  if (!voice.available) return null;
  const { state } = voice;
  const listening = state === "listening";

  function pointerDown(e: PointerEvent<HTMLButtonElement>) {
    if (e.button !== 0 || disabled) return;
    e.preventDefault();
    e.currentTarget.setPointerCapture?.(e.pointerId);
    voice.press();
  }
  function pointerUp() {
    if (listening) voice.release();
  }
  function keyDown(e: KeyboardEvent<HTMLButtonElement>) {
    if ((e.key !== " " && e.key !== "Enter") || e.ctrlKey) return;
    e.preventDefault();
    if (!e.repeat && !disabled) voice.press();
  }
  function keyUp(e: KeyboardEvent<HTMLButtonElement>) {
    if ((e.key !== " " && e.key !== "Enter") || e.ctrlKey) return;
    e.preventDefault();
    if (listening) voice.release();
  }

  return (
    <>
      <button
        type="button"
        className={`voice-key voice-${state}`}
        aria-pressed={listening}
        aria-label={t("voice.hold")}
        title={t("voice.holdHint")}
        disabled={disabled && state === "idle"}
        onPointerDown={pointerDown}
        onPointerUp={pointerUp}
        onPointerCancel={pointerUp}
        onKeyDown={keyDown}
        onKeyUp={keyUp}
        onClick={(e) => e.preventDefault()}
        onContextMenu={(e) => e.preventDefault()}
      >
        {state === "idle" ? (
          <svg className="voice-mic" viewBox="0 0 24 24" width="18" height="18" aria-hidden="true">
            <rect x="9" y="3" width="6" height="11" rx="3" fill="none" stroke="currentColor" strokeWidth="2" />
            <path d="M5 11a7 7 0 0 0 14 0M12 18v3" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" />
          </svg>
        ) : (
          <span className="voice-orb" aria-hidden="true" />
        )}
      </button>
      <span className="sr-only" role="status">
        {state === "idle" ? "" : t(`voice.state.${state}`)}
      </span>
    </>
  );
}

/** What went wrong with the last try, under the composer (a refused microphone, nothing heard). */
export function VoiceProblemLine({ voice }: { voice: PushToTalkHandle }) {
  const { t } = useTranslation();
  if (!voice.problem) return null;
  return (
    <p className="voice-problem error" role="alert">
      {t(`voice.problem.${voice.problem}`)}
    </p>
  );
}
