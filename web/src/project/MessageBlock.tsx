import { useState } from "react";
import { useTranslation } from "react-i18next";
import type { ApiClient } from "../api/client";
import { messageLabel, messageType } from "./messages";
import type { OrderRelease, ProjectMessageInfo, ProjectNode } from "./types";

/** A value of a payload as plain text: numbers and words as they are, lists one item per line, and
 *  anything nested as compact JSON. Never markup: a message may come from a stranger, so it is shown
 *  as data and nothing in it is acted on. */
export function plain(value: unknown): string {
  if (value === null || value === undefined) return "";
  if (typeof value === "string") return value;
  if (typeof value === "number" || typeof value === "boolean")
    return String(value);
  if (Array.isArray(value))
    return value
      .map((v) => (typeof v === "string" ? v : JSON.stringify(v)))
      .join("\n");
  return JSON.stringify(value);
}

const ORDER = "project.order";

/** What a message adds to its node: its type, from where to where, its state and round, how far to
 *  trust it, what the code checked, and the payload as the registry saw it. */
export function MessagePanel({ info }: { info: ProjectMessageInfo }) {
  const { t, i18n } = useTranslation();
  const spec = messageType(info.type);
  return (
    <fieldset
      className="project-extra project-message"
      data-trust={info.trust ?? undefined}
    >
      <legend>{t("project.message")}</legend>
      <dl className="project-facts">
        <dt>{t("project.messageType")}</dt>
        <dd>{messageLabel(info.type, i18n.language)}</dd>
        <dt>{t("project.messageRoute")}</dt>
        <dd>
          {t(`project.apps.${info.from}`, { defaultValue: info.from })} →{" "}
          {t(`project.apps.${info.to}`, { defaultValue: info.to })}
        </dd>
        <dt>{t("project.messageState")}</dt>
        <dd>
          {t(`project.messageStates.${info.state}`, {
            defaultValue: info.state,
          })}
        </dd>
        <dt>{t("project.messageRound")}</dt>
        <dd>{info.round}</dd>
        {info.trust && (
          <>
            <dt>{t("project.messageTrust")}</dt>
            <dd>{t(`project.trusts.${info.trust}`)}</dd>
          </>
        )}
        {spec?.needsReview && (
          <>
            <dt>{t("project.messageReview")}</dt>
            <dd>{t("project.messageNeedsReview")}</dd>
          </>
        )}
      </dl>
      {info.trust === "foreign" && (
        <p role="note" className="project-muted">
          {t("project.messageForeign")}
        </p>
      )}
      {info.verification && (
        <p
          role="status"
          className={info.verification.ok ? "project-ok" : "project-error"}
        >
          {info.verification.ok
            ? t("project.verified")
            : t("project.notVerified", {
                problems: (info.verification.issues ?? []).join("; "),
              })}
        </p>
      )}
      {info.payload && Object.keys(info.payload).length > 0 && (
        <dl
          className="project-payload"
          aria-label={t("project.messagePayload")}
        >
          {Object.entries(info.payload).map(([key, value]) => (
            <div key={key}>
              <dt>{key}</dt>
              <dd>
                <pre>{plain(value)}</pre>
              </dd>
            </div>
          ))}
        </dl>
      )}
    </fieldset>
  );
}

/** The card of an order that waits. It shows exactly what a mandate would cover, taken from the
 *  order itself; the person may only choose how long it runs, add their words and say whether the
 *  hub may push the branch. */
function OrderCard({
  node,
  info,
  onRelease,
  onDecide,
}: {
  node: ProjectNode;
  info: ProjectMessageInfo;
  onRelease: (body: OrderRelease) => void;
  onDecide: (confirm: boolean) => void;
}) {
  const { t } = useTranslation();
  const [days, setDays] = useState(14);
  const [text, setText] = useState("");
  const [push, setPush] = useState(true);
  const order = (info.payload ?? {}) as { repo?: string; branch?: string };
  return (
    <div
      className="project-form project-order-card"
      aria-label={t("project.orderCard")}
    >
      <h4>{t("project.orderCard")}</h4>
      <dl className="project-facts">
        <dt>{t("project.mandateRepo")}</dt>
        <dd>{order.repo}</dd>
        <dt>{t("project.mandateBranch")}</dt>
        <dd>
          <code>{order.branch}</code>
        </dd>
        <dt>{t("project.mandateActions")}</dt>
        <dd>create_prototype, fix</dd>
      </dl>
      <p className="project-muted">{t("project.mandateNotCovered")}</p>
      <div className="project-actions">
        <button type="button" onClick={() => onDecide(true)}>
          {t("project.releaseOnce")}
        </button>
        <button type="button" onClick={() => onDecide(false)}>
          {t("project.decline")}
        </button>
      </div>
      <fieldset>
        <legend>{t("project.mandateGive")}</legend>
        <label>
          {t("project.mandateDays")}
          <input
            type="number"
            min={1}
            max={90}
            value={days}
            onChange={(e) =>
              setDays(Math.min(90, Math.max(1, Number(e.target.value) || 1)))
            }
          />
        </label>
        <label>
          <input
            type="checkbox"
            checked={push}
            onChange={(e) => setPush(e.target.checked)}
          />
          {t("project.mandatePush")}
        </label>
        <input
          aria-label={t("project.mandateText")}
          placeholder={t("project.mandateText")}
          value={text}
          maxLength={2000}
          onChange={(e) => setText(e.target.value)}
        />
        <button
          type="button"
          data-node={node.id}
          onClick={() => onRelease({ days, text, push })}
        >
          {t("project.mandateRelease")}
        </button>
      </fieldset>
    </div>
  );
}

/** The message of a node and the clicks it may wait for: a request or order that awaits the
 *  person, a proposal an application made on its own. Every click goes to the hub, which checks it
 *  again; nothing here decides anything. */
export function MessageBlock({
  api,
  node,
  onChanged,
  onError,
}: {
  api: ApiClient;
  node: ProjectNode;
  onChanged: () => void;
  onError: (err: unknown) => void;
}) {
  const { t } = useTranslation();
  const info = node.message;
  if (!info) return null;
  const run = (call: () => Promise<unknown>) =>
    void call().then(onChanged, onError);
  const waiting = info.state === "awaiting_click";
  const isRequest = info.type === ORDER || info.type.endsWith(".request");
  const isProposal = info.type.endsWith(".proposal");
  return (
    <>
      <MessagePanel info={info} />
      {info.task_id && (
        <p className="project-muted">
          {t("project.queued", { task: info.task_id.slice(0, 8) })}
        </p>
      )}
      {waiting && info.type === ORDER && (
        <OrderCard
          node={node}
          info={info}
          onRelease={(body) =>
            run(() => api.releaseProjectOrder(node.id, body))
          }
          onDecide={(confirm) =>
            run(() => api.decideProjectRequest(node.id, confirm))
          }
        />
      )}
      {waiting && isRequest && info.type !== ORDER && (
        <div className="project-actions">
          <button
            type="button"
            onClick={() => run(() => api.decideProjectRequest(node.id, true))}
          >
            {t("project.confirm")}
          </button>
          <button
            type="button"
            onClick={() => run(() => api.decideProjectRequest(node.id, false))}
          >
            {t("project.decline")}
          </button>
        </div>
      )}
      {waiting && isProposal && (
        <div className="project-actions">
          <button
            type="button"
            onClick={() => run(() => api.decideProjectProposal(node.id, true))}
          >
            {t("project.proposalAccept")}
          </button>
          <button
            type="button"
            onClick={() => run(() => api.decideProjectProposal(node.id, false))}
          >
            {t("project.decline")}
          </button>
        </div>
      )}
    </>
  );
}
