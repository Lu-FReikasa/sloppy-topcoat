import { useCallback, useEffect, useState } from "react";
import { useTranslation } from "react-i18next";
import { UnauthorizedError, type ApiClient } from "../api/client";
import { ProposalCard } from "../agent/Proposals";
import { Dialog } from "../components/Dialog";
import { describeError, toActionError } from "../errors";
import type { AgentProposal } from "../types";

/** What a button of the inventory is about: one container, one stack (compose project) or one unit. */
export interface Target {
  kind: "container" | "stack" | "service";
  name: string;
}

/** The capability and its input for each kind. */
export function logsCall(target: Target, lines: number, contains: string) {
  if (target.kind === "stack") {
    return {
      capability: "compose.logs",
      input: { project: target.name, lines, ...(contains ? { contains } : {}) },
    };
  }
  return { capability: "docker.logs", input: { container: target.name, lines } };
}

export function changeCall(target: Target): { capability: string; input: Record<string, unknown> } {
  if (target.kind === "stack") return { capability: "compose.restart", input: { project: target.name } };
  if (target.kind === "service") return { capability: "service.restart", input: { unit: target.name } };
  return { capability: "docker.restart", input: { container: target.name } };
}

const LINE_CHOICES = [50, 100, 200];

/** The logs of one container or one whole stack, read through the same read-only capability the
 *  assistant uses (docker.logs / compose.logs): with the rights of the signed-in user, audited. A
 *  stack's logs are merged by time with the service in front of each line. */
export function LogsDialog({
  api,
  nodeId,
  target,
  onClose,
}: {
  api: ApiClient;
  nodeId: string;
  target: Target;
  onClose: () => void;
}) {
  const { t, i18n } = useTranslation();
  // what the form holds, and the request that was last sent (the effect reads only the latter, so
  // typing in the filter does not read the log on every keystroke)
  const [lines, setLines] = useState(100);
  const [contains, setContains] = useState("");
  const [request, setRequest] = useState({ n: 0, lines: 100, contains: "" });
  const [busy, setBusy] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [log, setLog] = useState<{ text: string; truncated: boolean; count: number; masked: number } | null>(null);

  const send = (nextLines: number) => {
    setBusy(true);
    setRequest((r) => ({ n: r.n + 1, lines: nextLines, contains: contains.trim() }));
  };

  useEffect(() => {
    let alive = true;
    const call = logsCall(target, request.lines, request.contains);
    api.runCapability(nodeId, call.capability, call.input).then(
      (res) => {
        if (!alive) return;
        setBusy(false);
        if (!res.ok) {
          setError(res.error?.message ?? t("inventory.actions.logsFailed"));
          setLog(null);
          return;
        }
        setError(null);
        const out = (res.output ?? {}) as Record<string, unknown>;
        setLog({
          text: typeof out.log === "string" ? out.log : "",
          truncated: out.truncated === true,
          count: typeof out.lines === "number" ? out.lines : 0,
          masked: typeof out.masked === "number" ? out.masked : 0,
        });
      },
      (err: unknown) => {
        if (!alive) return;
        setBusy(false);
        if (!(err instanceof UnauthorizedError)) setError(describeError(toActionError(err), t, i18n));
      },
    );
    return () => {
      alive = false;
    };
  }, [api, nodeId, target, request, t, i18n]);

  return (
    <Dialog
      title={t(target.kind === "stack" ? "inventory.actions.logsStackTitle" : "inventory.actions.logsTitle", {
        name: target.name,
      })}
      onClose={onClose}
    >
      <form
        className="log-controls"
        onSubmit={(e) => {
          e.preventDefault();
          send(lines);
        }}
      >
        <label>
          {t("inventory.actions.lines")}{" "}
          <select
            value={lines}
            onChange={(e) => {
              setLines(Number(e.target.value));
              send(Number(e.target.value));
            }}
          >
            {LINE_CHOICES.map((n) => (
              <option key={n} value={n}>
                {n}
              </option>
            ))}
          </select>
        </label>
        {target.kind === "stack" && (
          <label>
            {t("inventory.actions.contains")}{" "}
            <input
              type="text"
              maxLength={100}
              value={contains}
              placeholder={t("inventory.actions.containsPlaceholder")}
              onChange={(e) => setContains(e.target.value)}
            />
          </label>
        )}
        <button type="submit" disabled={busy}>
          {busy ? t("inventory.actions.loading") : t("inventory.actions.reload")}
        </button>
      </form>
      {error && (
        <p className="error" role="alert">
          {error}
        </p>
      )}
      {log && (
        <>
          <p className="muted small" data-testid="log-meta">
            {t("inventory.actions.logMeta", { count: log.count })}
            {log.truncated ? ` ${t("inventory.actions.logTruncated")}` : ""}
            {log.masked > 0 ? ` ${t("inventory.actions.logMasked", { count: log.masked })}` : ""}
          </p>
          <pre className="log-view" data-testid="log-text">
            {log.text || t("inventory.actions.logEmpty")}
          </pre>
        </>
      )}
      <div className="dialog-actions">
        <button type="button" onClick={onClose}>
          {t("inventory.actions.close")}
        </button>
      </div>
    </Dialog>
  );
}

/** A restart the person asks for: the hub answers with a proposal that says exactly what it does,
 *  and nothing runs until the person clicks "Run" on that card (the same card and the same click
 *  as for the agent's proposals). Closing the dialog leaves nothing behind that can run by itself:
 *  an unconfirmed proposal expires. */
export function ChangeDialog({
  api,
  nodeId,
  target,
  onClose,
  onDone,
}: {
  api: ApiClient;
  nodeId: string;
  target: Target;
  onClose: () => void;
  onDone: () => void;
}) {
  const { t, i18n } = useTranslation();
  const [proposal, setProposal] = useState<AgentProposal | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let alive = true;
    const call = changeCall(target);
    api.manualProposal(nodeId, call.capability, call.input).then(
      (p) => alive && setProposal(p),
      (err: unknown) => {
        if (alive && !(err instanceof UnauthorizedError)) setError(describeError(toActionError(err), t, i18n));
      },
    );
    return () => {
      alive = false;
    };
  }, [api, nodeId, target, t, i18n]);

  const reload = useCallback(() => {
    if (!proposal) return;
    api.proposal(proposal.id).then(
      (p) => {
        setProposal(p);
        if (p.status === "executed") onDone();
      },
      () => undefined,
    );
  }, [api, proposal, onDone]);

  return (
    <Dialog
      title={t(
        target.kind === "stack"
          ? "inventory.actions.restartStackTitle"
          : target.kind === "service"
            ? "inventory.actions.restartServiceTitle"
            : "inventory.actions.restartTitle",
        { name: target.name },
      )}
      onClose={onClose}
    >
      {error && (
        <p className="error" role="alert">
          {error}
        </p>
      )}
      {!proposal && !error && <p className="muted">{t("inventory.actions.preparing")}</p>}
      {proposal && <ProposalCard api={api} proposal={proposal} onChanged={reload} />}
      <div className="dialog-actions">
        <button type="button" onClick={onClose}>
          {t("inventory.actions.close")}
        </button>
      </div>
    </Dialog>
  );
}
