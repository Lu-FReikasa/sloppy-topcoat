import { useEffect, useRef, useState } from "react";
import { useTranslation } from "react-i18next";
import { UnauthorizedError, type ApiClient } from "../api/client";
import { describeError, toActionError } from "../errors";
import { formatDateTime } from "../format";
import type { PairingCode } from "../types";

interface Props {
  api: ApiClient;
  onClose: () => void;
  /** Origin the node should connect to; defaults to window.location.origin. */
  origin?: string;
}

export type NodeOs = "windows" | "linux";

/** Release asset names (docs/INSTALL-NODE.md); the commands run in the download folder. */
export const NODE_BINARIES: Record<NodeOs, string> = {
  windows: "topcoat-node-windows-amd64.exe",
  linux: "topcoat-node-linux-amd64",
};

/** Node names the hub accepts (pair_request.schema.json). */
export const NODE_NAME_RE = /^[a-z0-9][a-z0-9-]{0,62}$/;

export interface CommandOptions {
  hub: string;
  code: string;
  os: NodeOs;
  fingerprint?: string | null;
  verb?: "pair" | "install";
  /** Omitted: the node uses its sanitized hostname. */
  name?: string;
}

/**
 * Build the node command for `pair` (foreground) or `install` (service).
 * The CA fingerprint pins the hub certificate (D11); a plain-http hub needs the
 * explicit --insecure-http opt-in, which only makes sense for tests and local dev.
 */
export function pairCommand({
  hub,
  code,
  os,
  fingerprint,
  verb = "pair",
  name,
}: CommandOptions): string {
  const exe = os === "windows" ? `.\\${NODE_BINARIES.windows}` : `./${NODE_BINARIES.linux}`;
  const sudo = os === "linux" && verb === "install" ? "sudo " : "";
  const parts = [`${sudo}${exe} ${verb} --hub ${hub} --code ${code}`];
  if (fingerprint) parts.push(`--ca-fingerprint ${fingerprint}`);
  if (name) parts.push(`--name ${name}`);
  if (hub.startsWith("http:")) parts.push("--insecure-http");
  return parts.join(" ");
}

/** What to run once after downloading the binary. */
export function prepareCommand(os: NodeOs): string {
  return os === "windows"
    ? `Unblock-File .\\${NODE_BINARIES.windows}`
    : `chmod +x ${NODE_BINARIES.linux}`;
}

function guessOs(): NodeOs {
  return typeof navigator !== "undefined" && /windows/i.test(navigator.userAgent)
    ? "windows"
    : "linux";
}

type CopyTarget = "install" | "pair";
interface CopyState {
  which: CopyTarget | null;
  state: "idle" | "copied" | "failed";
}

export function AddNodeDialog({ api, onClose, origin = window.location.origin }: Props) {
  const { t, i18n } = useTranslation();
  const [pairing, setPairing] = useState<PairingCode | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [attempt, setAttempt] = useState(0);
  const [copy, setCopy] = useState<CopyState>({ which: null, state: "idle" });
  const [os, setOs] = useState<NodeOs>(guessOs);
  const [name, setName] = useState("");
  const closeRef = useRef<HTMLButtonElement>(null);

  useEffect(() => {
    let cancelled = false;
    api.createPairingCode().then(
      (p) => {
        if (!cancelled) setPairing(p);
      },
      (err: unknown) => {
        if (cancelled || err instanceof UnauthorizedError) return;
        setError(describeError(toActionError(err), t, i18n));
      },
    );
    return () => {
      cancelled = true;
    };
  }, [api, attempt, t, i18n]);

  useEffect(() => {
    closeRef.current?.focus();
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") onClose();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose]);

  const hub = pairing?.hub_url ?? origin;
  const fingerprint = pairing?.ca_fingerprint ?? null;
  const trimmedName = name.trim();
  const nameValid = trimmedName === "" || NODE_NAME_RE.test(trimmedName);
  const opts = pairing
    ? { hub, code: pairing.code, os, fingerprint, name: trimmedName || undefined }
    : null;
  const installCommand = opts ? pairCommand({ ...opts, verb: "install" }) : "";
  const command = opts ? pairCommand(opts) : "";

  async function copyCommand(which: CopyTarget, text: string) {
    try {
      if (!navigator.clipboard) throw new Error("clipboard unavailable");
      await navigator.clipboard.writeText(text);
      setCopy({ which, state: "copied" });
    } catch {
      setCopy({ which, state: "failed" });
    }
  }

  function copyLabel(which: CopyTarget): string {
    return copy.which === which && copy.state === "copied"
      ? t("pairing.copied")
      : t("pairing.copy");
  }

  function retry() {
    setError(null);
    setPairing(null);
    setAttempt((a) => a + 1);
  }

  return (
    <div className="dialog-backdrop" onClick={onClose}>
      <div
        className="dialog"
        role="dialog"
        aria-modal="true"
        aria-labelledby="add-node-title"
        onClick={(e) => e.stopPropagation()}
      >
        <h2 id="add-node-title">{t("pairing.heading")}</h2>
        {!pairing && !error && <p className="muted">{t("pairing.creating")}</p>}
        {error && (
          <>
            <p className="error" role="alert">
              {t("pairing.error", { message: error })}
            </p>
            <button type="button" onClick={retry}>
              {t("pairing.retry")}
            </button>
          </>
        )}
        {pairing && (
          <>
            <p>{t("pairing.intro")}</p>
            <p>
              <span className="fact-label">{t("pairing.code")}:</span>{" "}
              <strong className="pair-code" data-testid="pair-code">
                {pairing.code}
              </strong>
            </p>
            <p className="muted">
              {t("pairing.expires", {
                time: formatDateTime(pairing.expires_at, i18n.language),
              })}
            </p>
            <fieldset className="os-choice">
              <legend className="fact-label">{t("pairing.os")}</legend>
              {(["windows", "linux"] as const).map((o) => (
                <label key={o}>
                  <input
                    type="radio"
                    name="node-os"
                    value={o}
                    checked={os === o}
                    onChange={() => setOs(o)}
                  />
                  {o === "windows" ? t("pairing.osWindows") : t("pairing.osLinux")}
                </label>
              ))}
            </fieldset>
            <label className="name-field">
              <span className="fact-label">{t("pairing.name")}</span>
              <input
                type="text"
                value={name}
                placeholder={os === "windows" ? "node-amd" : "node-nvidia"}
                spellCheck={false}
                autoComplete="off"
                aria-invalid={!nameValid}
                onChange={(e) => setName(e.target.value)}
              />
            </label>
            {nameValid ? (
              trimmedName === "" && <p className="muted small">{t("pairing.nameHint")}</p>
            ) : (
              <p className="error" data-testid="name-invalid">
                {t("pairing.nameInvalid")}
              </p>
            )}
            <p className="muted">
              {os === "windows" ? t("pairing.prepareWindows") : t("pairing.prepareLinux")}{" "}
              <code data-testid="prepare-command">{prepareCommand(os)}</code>
            </p>
            <p className="fact-label">{t("pairing.installCommand")}:</p>
            <div className="command">
              <code data-testid="install-command">{installCommand}</code>
              <button
                type="button"
                disabled={!nameValid}
                onClick={() => void copyCommand("install", installCommand)}
              >
                {copyLabel("install")}
              </button>
            </div>
            <p className="muted">{t("pairing.installHint")}</p>
            <p className="fact-label">{t("pairing.command")}:</p>
            <div className="command">
              <code data-testid="pair-command">{command}</code>
              <button
                type="button"
                disabled={!nameValid}
                onClick={() => void copyCommand("pair", command)}
              >
                {copyLabel("pair")}
              </button>
            </div>
            {copy.state === "failed" && <p className="error">{t("pairing.copyFailed")}</p>}
            {fingerprint ? (
              <p className="muted" data-testid="ca-info">
                {t("pairing.caFingerprint")}: <code className="fingerprint">{fingerprint}</code>{" "}
                <a href="/api/tls/ca.pem" download>
                  {t("pairing.caDownload")}
                </a>
              </p>
            ) : (
              hub.startsWith("http:") && (
                <p className="error" data-testid="insecure-warning">
                  {t("pairing.insecure")}
                </p>
              )
            )}
          </>
        )}
        <div className="dialog-actions">
          <button type="button" ref={closeRef} onClick={onClose}>
            {t("pairing.close")}
          </button>
        </div>
      </div>
    </div>
  );
}
