import { useCallback, useEffect, useState } from "react";
import { useTranslation } from "react-i18next";
import type { ApiClient } from "../api/client";
import { Markdown, copyToClipboard } from "../chat/Markdown";
import { APPLICATIONS, AppTag } from "../inbox/AppTag";
import type { Handover } from "../types";
import { HANDOVER_GROUPS, type HandoverGroup, groupHandovers } from "./groups";

/** Groups that have ended start folded (issue #177). */
const FOLDED: ReadonlySet<HandoverGroup> = new Set(["done", "withdrawn"]);

interface Props {
  api: ApiClient;
  /** The application the request comes from (the latest conversation there is handed over). */
  from?: string;
  /** An incognito tab to hand a question over from: the question only, no context. */
  incognito?: string;
  /** A hand-over to show: one the assistant proposed is opened as a dossier to send. */
  id?: string;
  /** Bumped when the hub says something changed (a result came back). */
  tick?: number;
}

/** Hand-over between applications (docs/PLATFORM.md, section 3.8). The person picks the other
 *  application, reads the dossier the agent wrote, and only then sends it: what is sent is exactly
 *  the dossier on the screen. The answer comes back as a summary in the inbox. */
export function HandoversPage({ api, from, incognito, id, tick = 0 }: Props) {
  const { t } = useTranslation();
  const [list, setList] = useState<Handover[]>([]);
  const [prepared, setDraft] = useState<Handover | null>(null);
  // a dossier the assistant proposed (the address names it) is shown like one prepared here
  const [dismissed, setDismissed] = useState<string | null>(null);
  // the explicit approval of this send for a dossier that holds a secret (only that click)
  const [approveCritical, setApproveCritical] = useState(false);
  const [target, setTarget] = useState<string | null>(null);
  const [note, setNote] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  // the Akte that is open: the text of one hand-over
  const [file, setFile] = useState<{
    id: string;
    markdown: string;
    path: string | null;
  } | null>(null);
  const [copied, setCopied] = useState(false);

  const reload = useCallback(() => {
    api.handovers().then(
      (r) => setList(r.handovers),
      () => setError(t("handover.loadFailed")),
    );
  }, [api, t]);
  useEffect(reload, [reload, tick]);

  const proposed = id
    ? list.find((h) => h.id === id && h.status === "proposed")
    : undefined;
  const draft =
    prepared ?? (proposed && proposed.id !== dismissed ? proposed : null);
  const canStart = from !== undefined || incognito !== undefined;
  const targets = APPLICATIONS.filter((a) => a !== from);

  async function prepare() {
    if (!target) return;
    setBusy(true);
    setError(null);
    try {
      let conversation_id: string | undefined;
      if (incognito === undefined) {
        const mine = (await api.listConversations()).filter(
          (c) => (c.application ?? "chat") === from,
        );
        conversation_id = mine[0]?.id;
        if (!conversation_id) {
          setError(t("handover.nothing"));
          return;
        }
      }
      setDraft(
        await api.prepareHandover({
          to_application: target,
          conversation_id,
          incognito_tab: incognito,
          note: note.trim() || undefined,
        }),
      );
    } catch {
      setError(t("handover.prepareFailed"));
    } finally {
      setBusy(false);
    }
  }

  async function send() {
    if (!draft) return;
    setBusy(true);
    setError(null);
    try {
      await api.sendHandover(draft.id, draft.hash, approveCritical);
      setApproveCritical(false);
      setDraft(null);
      setDismissed(draft.id);
      setNote("");
      reload();
    } catch {
      setError(t("handover.sendFailed"));
    } finally {
      setBusy(false);
    }
  }

  async function openFile(id: string) {
    if (file?.id === id) {
      setFile(null);
      return;
    }
    try {
      const got = await api.handoverFile(id);
      setFile({ id, markdown: got.markdown, path: got.knowledge_path });
      setCopied(false);
    } catch {
      setError(t("handover.fileFailed"));
    }
  }

  function download(f: { markdown: string; path: string | null; id: string }) {
    const blob = new Blob([f.markdown], {
      type: "text/markdown;charset=utf-8",
    });
    const url = URL.createObjectURL(blob);
    const link = document.createElement("a");
    link.href = url;
    link.download = f.path ?? `akte-${f.id.slice(0, 8)}.md`;
    link.click();
    URL.revokeObjectURL(url);
  }

  async function withdraw(id: string) {
    try {
      await api.cancelHandover(id);
    } catch {
      setError(t("handover.cancelFailed"));
    }
    if (draft?.id === id) {
      setDraft(null);
      setDismissed(id);
    }
    reload();
  }

  const shown = list.filter((h) => h.id !== draft?.id);
  const groups = groupHandovers(shown);

  const row = (h: Handover) => (
    <li
      key={h.id}
      data-testid="handover"
      aria-current={h.id === id ? "true" : undefined}
    >
      <AppTag application={h.from_application} />
      {" → "}
      <AppTag application={h.to_application} />{" "}
      <strong>{h.dossier.question}</strong>
      <div className="muted small">
        {t(`handover.status.${h.status}`)}
        {h.result_summary ? `: ${h.result_summary}` : ""}
        {h.error ? `: ${h.error}` : ""}
      </div>
      <details className="handover-full facts-more">
        <summary>{t("handover.fullText")}</summary>
        <DossierFields dossier={h.dossier} />
      </details>
      <div className="handover-actions">
        <button
          type="button"
          aria-expanded={file?.id === h.id}
          onClick={() => void openFile(h.id)}
        >
          {file?.id === h.id
            ? t("handover.fileClose")
            : t("handover.fileOpen")}
        </button>
        {(h.status === "proposed" || h.status === "sent") && (
          <button type="button" onClick={() => void withdraw(h.id)}>
            {t("handover.discard")}
          </button>
        )}
      </div>
      {file?.id === h.id && (
        <div className="handover-file" data-testid="handover-file">
          <div className="handover-actions">
            <button
              type="button"
              onClick={() =>
                void copyToClipboard(file.markdown).then(setCopied)
              }
            >
              {copied ? t("chat.copied") : t("handover.fileCopy")}
            </button>
            <button type="button" onClick={() => download(file)}>
              {t("handover.fileDownload")}
            </button>
            {file.path && (
              <span className="muted small">
                {t("handover.filed", { path: file.path })}
              </span>
            )}
          </div>
          <Markdown text={file.markdown} />
        </div>
      )}
    </li>
  );

  return (
    <section className="handovers" aria-label={t("handover.title")}>
      {error && (
        <p className="bad" role="alert">
          {error}
        </p>
      )}
      {canStart && !draft && (
        <div className="handover-start">
          <p>
            {incognito !== undefined
              ? t("handover.introIncognito")
              : t("handover.intro")}
          </p>
          <div className="handover-targets" role="group">
            {targets.map((a) => (
              <button
                key={a}
                type="button"
                aria-pressed={target === a}
                className={target === a ? "primary" : undefined}
                onClick={() => setTarget(a)}
              >
                {t(`inbox.apps.${a}`)}
              </button>
            ))}
          </div>
          <textarea
            rows={2}
            value={note}
            aria-label={t("handover.note")}
            placeholder={t("handover.note")}
            onChange={(e) => setNote(e.target.value)}
          />
          <button
            type="button"
            className="primary"
            disabled={!target || busy}
            onClick={() => void prepare()}
          >
            {t("handover.prepare")}
          </button>
        </div>
      )}
      {draft && (
        <div className="handover-dossier" data-testid="dossier">
          <h3>
            {t("handover.dossier")}{" "}
            <AppTag application={draft.from_application} />
            {" → "}
            <AppTag application={draft.to_application} />
          </h3>
          <DossierFields dossier={draft.dossier} />
          <p className="muted small" data-testid="dossier-class">
            {t("handover.class.label")}: {t(`handover.class.${draft.class ?? "public"}`)}
          </p>
          {draft.class === "critical" && (
            <label className="notice-line">
              <input
                type="checkbox"
                checked={approveCritical}
                onChange={(e) => setApproveCritical(e.target.checked)}
                data-testid="approve-critical"
              />{" "}
              {t("handover.criticalHint")}
            </label>
          )}
          <p className="muted small">{t("handover.sendHint")}</p>
          <div className="handover-actions">
            <button
              type="button"
              className="primary"
              disabled={busy || (draft.class === "critical" && !approveCritical)}
              onClick={() => void send()}
            >
              {t("handover.send")}
            </button>
            <button
              type="button"
              disabled={busy}
              onClick={() => void withdraw(draft.id)}
            >
              {t("handover.discard")}
            </button>
          </div>
        </div>
      )}
      <h3>{t("handover.listTitle")}</h3>
      {shown.length === 0 && <p className="muted">{t("handover.empty")}</p>}
      {HANDOVER_GROUPS.map((g) => {
        const items = groups[g];
        if (items.length === 0) return null;
        const heading = (
          <h4 className="handover-group-title">
            {t(`handover.group.${g}`)}{" "}
            <span className="muted small">({items.length})</span>
          </h4>
        );
        const body = <ul className="handover-list">{items.map(row)}</ul>;
        return FOLDED.has(g) ? (
          <details
            key={g}
            className="handover-group fold"
            data-testid={`handover-group-${g}`}
            // a link to one hand-over (an inbox answer, a chat's stored answer) opens its group
            open={(id !== undefined && items.some((h) => h.id === id)) || undefined}
          >
            <summary>{heading}</summary>
            {body}
          </details>
        ) : (
          <div
            key={g}
            className="handover-group"
            data-testid={`handover-group-${g}`}
          >
            {heading}
            {body}
          </div>
        );
      })}
    </section>
  );
}

/** The fields of a request: on the dossier before sending, and folded in each list row. */
function DossierFields({ dossier }: { dossier: Handover["dossier"] }) {
  const { t } = useTranslation();
  return (
    <dl>
      <dt>{t("handover.question")}</dt>
      <dd>{dossier.question}</dd>
      <dt>{t("handover.asked")}</dt>
      <dd>{dossier.asked}</dd>
      <dt>{t("handover.expected")}</dt>
      <dd>{dossier.expected}</dd>
      {dossier.context.length > 0 && (
        <>
          <dt>{t("handover.context")}</dt>
          <dd>
            <ul>
              {dossier.context.map((q, i) => (
                <li key={i}>
                  <em>{q.role}:</em> {q.text}
                </li>
              ))}
            </ul>
          </dd>
        </>
      )}
      <dt>{t("handover.withheld")}</dt>
      <dd>
        {dossier.withheld.length > 0
          ? dossier.withheld.join("; ")
          : t("handover.withheldNone")}
      </dd>
    </dl>
  );
}
