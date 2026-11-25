import { useCallback, useEffect, useRef, useState } from "react";
import { useTranslation } from "react-i18next";
import type { ApiClient } from "../api/client";
import "./releases.css";
import {
  KeyFileError,
  type SigningKey,
  ed25519Supported,
  importSigningKey,
  parseKeyFile,
  signManifest,
} from "./sign";
import type {
  BuildKind,
  BuildSource,
  ReleaseBuild,
  ReleasePreview,
  ReleasesOverview,
} from "./types";

/** The browser's part of signing. A prop so a test can stand in for Web Crypto. */
export interface Signing {
  supported: () => Promise<boolean>;
  load: (keyFileText: string) => Promise<SigningKey>;
  sign: (key: SigningKey, manifest: string) => Promise<string>;
}

export const BROWSER_SIGNING: Signing = {
  supported: ed25519Supported,
  load: async (text) => importSigningKey(parseKeyFile(text)),
  sign: signManifest,
};

const POLL_MS = 3000;

function mb(bytes: number): string {
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}

/** Administration → Releases: builds the build service made, and the one step that puts a node
 *  release on the update channel: the admin's own signature, made here from a key file chosen on
 *  this page. Nothing on this page can sign without that file, and the file never leaves it. */
export function ReleasesPage({
  api,
  signing = BROWSER_SIGNING,
}: {
  api: ApiClient;
  signing?: Signing;
}) {
  const { t } = useTranslation();
  const [overview, setOverview] = useState<ReleasesOverview | null>(null);
  const [selected, setSelected] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(() => {
    api
      .releases()
      .then(setOverview, (e: unknown) =>
        setError(String((e as Error).message ?? e)),
      );
  }, [api]);

  useEffect(() => {
    load();
  }, [load]);

  const active = overview?.builds.some(
    (b) => b.state === "requested" || b.state === "building",
  );
  useEffect(() => {
    if (!active) return;
    const id = setInterval(load, POLL_MS);
    return () => clearInterval(id);
  }, [active, load]);

  if (!overview) return <p className="muted">{error ?? t("app.loading")}</p>;
  if (!overview.enabled) return <p className="empty">{t("releases.off")}</p>;
  const build = overview.builds.find((b) => b.id === selected) ?? null;
  const online = overview.build_nodes.filter((n) => n.state !== "offline");

  return (
    <section className="releases">
      <h2>{t("releases.title")}</h2>
      {error && (
        <p className="error" role="alert">
          {error}
        </p>
      )}
      {online.length === 0 && <p role="note">{t("releases.noBuildNode")}</p>}
      {overview.key_ids.length === 0 && (
        <p role="note">{t("releases.noKeys")}</p>
      )}
      <StartForm
        api={api}
        disabled={online.length === 0 || !!active}
        onStarted={(b) => {
          setError(null);
          setSelected(b.id);
          load();
        }}
        onError={(m) => setError(m)}
      />
      <h3>{t("releases.builds")}</h3>
      {overview.builds.length === 0 && (
        <p className="muted">{t("releases.noBuilds")}</p>
      )}
      <ul className="release-builds">
        {overview.builds.map((b) => (
          <li key={b.id}>
            <button
              type="button"
              aria-pressed={b.id === selected}
              className={b.id === selected ? "is-selected" : undefined}
              onClick={() => setSelected(b.id)}
            >
              <span className={`state-${b.state}`}>
                {t(`releases.states.${b.state}`)}
              </span>{" "}
              {t(`releases.kinds.${b.kind}`)} {b.version ?? ""} · {b.ref}
              {b.commit && <code> {b.commit.slice(0, 8)}</code>}
            </button>
          </li>
        ))}
      </ul>
      {build && (
        <BuildDetail
          key={build.id}
          api={api}
          build={build}
          keyIds={overview.key_ids}
          signing={signing}
          rolloutNode={overview.rollout_node ?? null}
          onPublished={load}
        />
      )}
      <h3>{t("releases.published")}</h3>
      {overview.published.length === 0 && (
        <p className="muted">{t("releases.nonePublished")}</p>
      )}
      <ul>
        {overview.published.map((r) => (
          <li key={`${r.channel}-${r.os}-${r.arch}-${r.version}`}>
            {r.channel} {r.version} · {r.os}/{r.arch} · {mb(r.size)} ·{" "}
            <code>{r.sha256.slice(0, 12)}</code>
          </li>
        ))}
      </ul>
    </section>
  );
}

function StartForm({
  api,
  disabled,
  onStarted,
  onError,
}: {
  api: ApiClient;
  disabled: boolean;
  onStarted: (b: ReleaseBuild) => void;
  onError: (message: string) => void;
}) {
  const { t } = useTranslation();
  const [kind, setKind] = useState<BuildKind>("node_release");
  const [ref, setRef] = useState("main");
  const [version, setVersion] = useState("");
  const [source, setSource] = useState<BuildSource>("release_repo");
  const ready =
    ref.trim() !== "" &&
    (kind !== "node_release" ||
      /^\d+\.\d+\.\d+(-[0-9A-Za-z.-]+)?$/.test(version));
  return (
    <form
      className="release-start"
      onSubmit={(e) => {
        e.preventDefault();
        api
          .startReleaseBuild({
            kind,
            ref: ref.trim(),
            version: kind === "node_release" ? version : null,
            source,
          })
          .then(onStarted, (err: unknown) =>
            onError(String((err as Error).message ?? err)),
          );
      }}
    >
      <label>
        {t("releases.kind")}
        <select
          value={kind}
          onChange={(e) => setKind(e.target.value as BuildKind)}
        >
          <option value="node_release">
            {t("releases.kinds.node_release")}
          </option>
          <option value="hub_image">{t("releases.kinds.hub_image")}</option>
        </select>
      </label>
      <label>
        {t("releases.ref")}
        <input
          value={ref}
          onChange={(e) => setRef(e.target.value)}
          maxLength={200}
        />
      </label>
      {kind === "node_release" && (
        <label>
          {t("releases.version")}
          <input
            value={version}
            placeholder="0.3.0"
            onChange={(e) => setVersion(e.target.value)}
          />
        </label>
      )}
      <label>
        {t("releases.source")}
        <select
          value={source}
          onChange={(e) => setSource(e.target.value as BuildSource)}
        >
          <option value="release_repo">
            {t("releases.sources.release_repo")}
          </option>
          <option value="dev_mirror">{t("releases.sources.dev_mirror")}</option>
        </select>
      </label>
      <button type="submit" disabled={disabled || !ready}>
        {t("releases.start")}
      </button>
    </form>
  );
}

function BuildDetail({
  api,
  build,
  keyIds,
  signing,
  rolloutNode,
  onPublished,
}: {
  api: ApiClient;
  build: ReleaseBuild;
  keyIds: string[];
  signing: Signing;
  rolloutNode: string | null;
  onPublished: () => void;
}) {
  const { t } = useTranslation();
  return (
    <article className="release-detail" aria-label={t("releases.detail")}>
      <dl className="project-facts">
        <dt>{t("releases.commit")}</dt>
        <dd>{build.commit ? <code>{build.commit}</code> : "–"}</dd>
        <dt>{t("releases.node")}</dt>
        <dd>{build.node ?? "–"}</dd>
        {build.channel && (
          <>
            <dt>{t("releases.channel")}</dt>
            <dd>
              {build.channel} ·{" "}
              {t("releases.signedWith", { key: build.key_id ?? "?" })}
            </dd>
          </>
        )}
      </dl>
      {build.error && (
        <p className="error" role="alert">
          {build.error}
        </p>
      )}
      {build.smoke && (
        <p
          role="status"
          className={build.smoke.ok ? "project-ok" : "project-error"}
        >
          {t(build.smoke.ok ? "releases.smokeOk" : "releases.smokeFailed", {
            summary: build.smoke.summary,
          })}
        </p>
      )}
      {build.changes && (
        <>
          <h4>{t("releases.changes")}</h4>
          <pre className="release-changes">{build.changes}</pre>
        </>
      )}
      {build.artifacts.length > 0 && (
        <table className="release-artifacts">
          <thead>
            <tr>
              <th>{t("releases.artifact")}</th>
              <th>{t("releases.platform")}</th>
              <th>{t("releases.size")}</th>
              <th>SHA-256</th>
            </tr>
          </thead>
          <tbody>
            {build.artifacts.map((a) => (
              <tr key={a.name}>
                <td>{a.name}</td>
                <td>{a.os ? `${a.os}/${a.arch}` : "–"}</td>
                <td>{a.size ? mb(a.size) : "–"}</td>
                <td>
                  <code>{a.sha256 ?? a.digest ?? ""}</code>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
      {build.log && (
        <details>
          <summary>{t("releases.log")}</summary>
          <pre>{build.log}</pre>
        </details>
      )}
      {build.kind === "node_release" && build.state === "built" && (
        <SigningPanel
          api={api}
          build={build}
          signing={signing}
          knownKeys={keyIds}
          onPublished={onPublished}
        />
      )}
      {build.kind === "hub_image" && build.state === "built" && (
        <RolloutPanel
          api={api}
          build={build}
          node={rolloutNode}
          onDone={onPublished}
        />
      )}
    </article>
  );
}

type KeyState =
  | { state: "none" }
  | { state: "bad"; message: string }
  | { state: "wrong"; keyId: string }
  | { state: "ok"; key: SigningKey };

function SigningPanel({
  api,
  build,
  signing,
  knownKeys,
  onPublished,
}: {
  api: ApiClient;
  build: ReleaseBuild;
  signing: Signing;
  knownKeys: string[];
  onPublished: () => void;
}) {
  const { t } = useTranslation();
  const [channel, setChannel] = useState("stable");
  const [preview, setPreview] = useState<ReleasePreview | null>(null);
  const [key, setKey] = useState<KeyState>({ state: "none" });
  const [checked, setChecked] = useState(false);
  const [busy, setBusy] = useState(false);
  const [problem, setProblem] = useState<string | null>(null);
  const [supported, setSupported] = useState<boolean | null>(null);
  const keyRef = useRef<SigningKey | null>(null);

  useEffect(() => {
    void signing.supported().then(setSupported);
  }, [signing]);

  const prepare = () => {
    setProblem(null);
    setChecked(false);
    api
      .releaseManifests(build.id, channel)
      .then(setPreview, (e: unknown) =>
        setProblem(String((e as Error).message ?? e)),
      );
  };

  const chooseFile = async (file: File | undefined) => {
    keyRef.current = null;
    setKey({ state: "none" });
    if (!file) return;
    try {
      const loaded = await signing.load(await file.text());
      if (!knownKeys.includes(loaded.keyId)) {
        setKey({ state: "wrong", keyId: loaded.keyId });
        return;
      }
      keyRef.current = loaded;
      setKey({ state: "ok", key: loaded });
    } catch (e) {
      setKey({
        state: "bad",
        message:
          e instanceof KeyFileError ? e.message : t("releases.keyUnreadable"),
      });
    }
  };

  const publish = async () => {
    if (!preview || !keyRef.current) return;
    setBusy(true);
    setProblem(null);
    try {
      const signatures: Record<string, string> = {};
      for (const m of preview.manifests)
        signatures[m.platform] = await signing.sign(keyRef.current, m.manifest);
      await api.publishRelease(build.id, {
        channel: preview.channel,
        created: preview.created,
        signatures,
      });
      keyRef.current = null;
      setKey({ state: "none" });
      setPreview(null);
      onPublished();
    } catch (e) {
      setProblem(String((e as Error).message ?? e));
    } finally {
      setBusy(false);
    }
  };

  return (
    <section className="release-sign" aria-label={t("releases.signTitle")}>
      <h4>{t("releases.signTitle")}</h4>
      <p className="muted">{t("releases.signHint")}</p>
      {supported === false && (
        <p role="alert" className="error">
          {t("releases.noEd25519")}
        </p>
      )}
      <label>
        {t("releases.channel")}
        <input
          value={channel}
          onChange={(e) => setChannel(e.target.value)}
          maxLength={32}
        />
      </label>
      <button
        type="button"
        onClick={prepare}
        disabled={!/^[a-z][a-z0-9-]{0,31}$/.test(channel)}
      >
        {t("releases.prepare")}
      </button>
      {preview && (
        <div className="release-preview">
          <p>
            {t("releases.willPublish", {
              version: build.version ?? "",
              channel: preview.channel,
              previous: preview.previous_version ?? t("releases.noPrevious"),
            })}
          </p>
          <p>
            {t("releases.commit")}: <code>{build.commit}</code>
          </p>
          <ul>
            {preview.manifests.map((m) => (
              <li key={m.platform}>
                {m.platform} · {mb(m.size)} · <code>{m.sha256}</code>
              </li>
            ))}
          </ul>
          <label>
            {t("releases.keyFile")}
            <input
              type="file"
              aria-label={t("releases.keyFile")}
              onChange={(e) => void chooseFile(e.target.files?.[0])}
            />
          </label>
          {key.state === "bad" && (
            <p role="alert" className="error">
              {key.message}
            </p>
          )}
          {key.state === "wrong" && (
            <p role="alert" className="error">
              {t("releases.keyNotTrusted", {
                key: key.keyId,
                known: knownKeys.join(", "),
              })}
            </p>
          )}
          {key.state === "ok" && (
            <p role="status" className="project-ok">
              {t("releases.keyOk", { key: key.key.keyId })}
            </p>
          )}
          <label>
            <input
              type="checkbox"
              checked={checked}
              onChange={(e) => setChecked(e.target.checked)}
            />
            {t("releases.iChecked")}
          </label>
          <button
            type="button"
            disabled={busy || !checked || key.state !== "ok"}
            onClick={() => void publish()}
          >
            {t("releases.signAndPublish")}
          </button>
        </div>
      )}
      {problem && (
        <p className="error" role="alert">
          {problem}
        </p>
      )}
    </section>
  );
}

function RolloutPanel({
  api,
  build,
  node,
  onDone,
}: {
  api: ApiClient;
  build: ReleaseBuild;
  node: string | null;
  onDone: () => void;
}) {
  const { t } = useTranslation();
  const [busy, setBusy] = useState(false);
  const [problem, setProblem] = useState<string | null>(null);
  const [confirmed, setConfirmed] = useState(false);
  const checked = build.rollout.some((r) => r.dry_run && r.ok);
  const run = (dryRun: boolean) => {
    setBusy(true);
    setProblem(null);
    api
      .rolloutRelease(build.id, dryRun)
      .then(
        () => {
          setConfirmed(false);
          onDone();
        },
        (e: unknown) => setProblem(String((e as Error).message ?? e)),
      )
      .finally(() => setBusy(false));
  };
  if (!node) return <p className="muted">{t("releases.imageNote")}</p>;
  return (
    <section className="release-sign" aria-label={t("releases.rolloutTitle")}>
      <h4>{t("releases.rolloutTitle")}</h4>
      <p className="muted">{t("releases.rolloutHint", { node })}</p>
      <button type="button" disabled={busy} onClick={() => run(true)}>
        {t("releases.rolloutCheck")}
      </button>
      <label>
        <input
          type="checkbox"
          checked={confirmed}
          onChange={(e) => setConfirmed(e.target.checked)}
        />
        {t("releases.rolloutConfirm")}
      </label>
      <button
        type="button"
        disabled={busy || !checked || !confirmed}
        onClick={() => run(false)}
      >
        {t("releases.rolloutGo")}
      </button>
      {!checked && <p className="muted">{t("releases.rolloutCheckFirst")}</p>}
      {problem && (
        <p className="error" role="alert">
          {problem}
        </p>
      )}
      <ul className="release-rollouts">
        {build.rollout.map((r) => (
          <li key={r.at} className={r.ok ? "project-ok" : "project-error"}>
            {t(r.dry_run ? "releases.rolloutDry" : "releases.rolloutReal")} ·{" "}
            {r.node} ·{" "}
            {r.ok
              ? t("releases.rolloutOk")
              : t("releases.rolloutFailed", { error: r.error ?? "" })}
            {r.output && (
              <details>
                <summary>{t("releases.log")}</summary>
                <pre>{r.output}</pre>
              </details>
            )}
          </li>
        ))}
      </ul>
    </section>
  );
}
