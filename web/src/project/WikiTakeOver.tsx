import { useState } from "react";
import { useTranslation } from "react-i18next";
import type { ApiClient } from "../api/client";
import { wikiHref } from "../route";
import type { WikiTakeOver as Preview } from "../types";

/** "Take into the wiki" on a project's document, or on a document of the Ablage (`doc`: it goes to
 *  the folder of the application that made it): the first click only asks the hub what would
 *  happen and shows it as a card (target page, new page or the next version); only the click on
 *  that card takes it over (D15). */
export function WikiTakeOver({
  api,
  nodeId,
  doc,
  onError,
}: {
  api: ApiClient;
  nodeId?: string;
  doc?: { kind: "report" | "artifact"; id: string };
  onError: (err: unknown) => void;
}) {
  const { t, i18n } = useTranslation();
  const lang = i18n.language?.startsWith("de") ? "de" : "en";
  const look = () => (doc ? api.wikiAblagePreview(doc.kind, doc.id, lang) : api.wikiTakeOverPreview(nodeId ?? ""));
  const take = () => (doc ? api.wikiAblageTakeOver(doc.kind, doc.id, lang) : api.wikiTakeOver(nodeId ?? ""));
  const [preview, setPreview] = useState<Preview | null>(null);
  const [done, setDone] = useState<{ id: string; version: number } | null>(null);
  const [busy, setBusy] = useState(false);

  const ask = async () => {
    setBusy(true);
    setDone(null);
    try {
      setPreview(await look());
    } catch (e) {
      onError(e);
    } finally {
      setBusy(false);
    }
  };

  const confirm = async () => {
    setBusy(true);
    try {
      const page = await take();
      setDone({ id: page.id, version: page.version });
      setPreview(null);
    } catch (e) {
      onError(e);
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="project-form" data-testid="wiki-take-over">
      {!preview && (
        <button type="button" disabled={busy} onClick={() => void ask()}>
          {t("wiki.takeOver")}
        </button>
      )}
      {preview && (
        <div className="card" role="group" aria-label={t("wiki.takeOverTitle")} data-testid="wiki-take-over-card">
          {preview.unchanged ? (
            <p>{t("wiki.takeOverUnchanged", { path: preview.path })}</p>
          ) : (
            <>
              <p>
                <strong>
                  {preview.new_page
                    ? t("wiki.takeOverNew", { path: preview.path })
                    : t("wiki.takeOverVersion", { path: preview.path, n: preview.version })}
                </strong>
              </p>
              <p className="muted small">{t("wiki.takeOverChars", { chars: preview.chars })}</p>
            </>
          )}
          <div className="row-actions">
            {!preview.unchanged && (
              <button type="button" className="primary" disabled={busy} onClick={() => void confirm()}>
                {t("wiki.takeOverConfirm")}
              </button>
            )}
            <button type="button" disabled={busy} onClick={() => setPreview(null)}>
              {t("wiki.cancel")}
            </button>
          </div>
        </div>
      )}
      {done && (
        <p role="status" className="small">
          {t("wiki.takenOver", { n: done.version })} <a href={wikiHref(done.id)}>{t("wiki.open")}</a>
        </p>
      )}
    </div>
  );
}
