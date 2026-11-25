import { createRoot } from "react-dom/client";
import i18n from "../i18n";
import { inClient } from "../webshell/shell";

/** A yes/no question before a step that cannot be undone. In a plain browser it is the browser's
 *  own `confirm`. In the desktop client it is a dialog in the page: the client's windows are
 *  transparent and stay on top, and a native script dialog there froze the whole client (live
 *  2026-11-14, deleting a model). */
export function askConfirm(message: string): Promise<boolean> {
  if (!inClient()) return Promise.resolve(window.confirm(message));
  return new Promise((resolve) => {
    const host = document.createElement("div");
    document.body.appendChild(host);
    const root = createRoot(host);
    const done = (answer: boolean) => {
      root.unmount();
      host.remove();
      resolve(answer);
    };
    root.render(<ConfirmDialog message={message} onAnswer={done} />);
  });
}

function ConfirmDialog({
  message,
  onAnswer,
}: {
  message: string;
  onAnswer: (yes: boolean) => void;
}) {
  return (
    <div
      className="confirm-backdrop"
      role="presentation"
      onKeyDown={(e) => {
        if (e.key === "Escape") onAnswer(false);
      }}
    >
      <div className="confirm-dialog" role="alertdialog" aria-modal="true" aria-label={message}>
        <p>{message}</p>
        <div className="confirm-actions">
          <button type="button" className="danger" autoFocus onClick={() => onAnswer(true)}>
            {i18n.t("confirm.yes")}
          </button>
          <button type="button" onClick={() => onAnswer(false)}>
            {i18n.t("confirm.no")}
          </button>
        </div>
      </div>
    </div>
  );
}
