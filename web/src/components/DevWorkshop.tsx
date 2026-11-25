import { useCallback, useEffect, useRef, useState } from "react";
import { useTranslation } from "react-i18next";
import { UnauthorizedError, type ApiClient } from "../api/client";
import { describeError, toActionError } from "../errors";
import type { DevRepo, NodeSummary, ToolResult } from "../types";
import { PushPanel } from "./PushPanel";
import { askConfirm } from "./askConfirm";

interface Props {
  api: ApiClient;
  nodes: NodeSummary[];
}

type Step = "sync" | "status" | "test" | "builds" | "build" | "reset";

interface Shown {
  step: Step;
  result: ToolResult;
}

/** Shapes of what the dev node answers (see node/internal/workshop). Everything is optional: an
 *  older node may leave something out, and the page then shows less, not a crash. */
interface StatusOut {
  branch?: string;
  head?: string;
  last_commit?: string;
  changed?: string[];
  clean?: boolean;
}
interface DiffOut {
  diff?: string;
  files?: string[];
  truncated?: boolean;
  masked?: number;
}
interface TestOut {
  passed?: boolean;
  timed_out?: boolean;
  exit_code?: number;
  output?: string;
  template?: string;
  // a background run (repo.test_status)
  job_id?: string;
  state?: string;
  running?: boolean;
  elapsed_ms?: number;
  log?: string;
  error?: string;
}
interface Artefact {
  name: string;
  bytes: number;
  sha256: string;
}
interface BuildOut {
  built?: boolean;
  exit_code?: number;
  output?: string;
  folder?: string;
  artefacts?: Artefact[];
  builds?: { version: string; files: Artefact[]; folder: string }[];
}

/** Between two questions about a running test (each one already waits for news). */
const TEST_POLL_PAUSE_MS = 500;

function asObj<T>(v: unknown): T {
  return (v && typeof v === "object" ? v : {}) as T;
}

/** The workshop at a glance (E6-01 S4): per registered repository what the assistant has done in
 *  the scratch worktree (state, diff), and buttons for the steps a person would take next: look
 *  again, run the tests, build the node, throw the change away, review and push. Every button is a
 *  named workshop capability; changing the worktree needs no click, leaving it does (D52). */
export function DevWorkshop({ api, nodes }: Props) {
  const { t, i18n } = useTranslation();
  const devNodes = nodes.filter((n) => (n.roles ?? []).includes("dev"));
  const [nodeId, setNodeId] = useState<string>("");
  const [repos, setRepos] = useState<DevRepo[] | null>(null);
  const [error, setError] = useState<string | null>(null);

  const fail = useCallback(
    (err: unknown) => {
      if (!(err instanceof UnauthorizedError))
        setError(describeError(toActionError(err), t, i18n));
    },
    [t, i18n],
  );
  useEffect(() => {
    api.devRepos().then((l) => setRepos(l.repos), fail);
  }, [api, fail]);

  const node =
    devNodes.find((n) => n.id === nodeId) ??
    devNodes.find((n) => n.connected) ??
    devNodes[0];

  return (
    <section className="dev-workshop">
      <h1>{t("workshop.heading")}</h1>
      <p>{t("workshop.intro")}</p>
      {error && (
        <p className="error" role="alert">
          {error}
        </p>
      )}
      {devNodes.length === 0 ? (
        <p className="muted">{t("workshop.noNode")}</p>
      ) : (
        <p>
          <label>
            {t("workshop.node")}{" "}
            <select
              value={node?.id ?? ""}
              onChange={(e) => setNodeId(e.target.value)}
            >
              {devNodes.map((n) => (
                <option key={n.id} value={n.id}>
                  {n.name}
                  {n.connected ? "" : ` (${t("workshop.offline")})`}
                </option>
              ))}
            </select>
          </label>
        </p>
      )}
      {repos === null ? (
        <p className="muted">{t("app.loading")}</p>
      ) : repos.length === 0 ? (
        <p className="muted">{t("workshop.noRepos")}</p>
      ) : (
        repos.map((r) => (
          <RepoCard
            key={r.id}
            api={api}
            repo={r}
            nodeId={node?.connected ? node.id : null}
          />
        ))
      )}
    </section>
  );
}

function RepoCard({
  api,
  repo,
  nodeId,
}: {
  api: ApiClient;
  repo: DevRepo;
  nodeId: string | null;
}) {
  const { t } = useTranslation();
  const [busy, setBusy] = useState<Step | null>(null);
  const [shown, setShown] = useState<Shown | null>(null);
  const [diff, setDiff] = useState<ToolResult | null>(null);
  const [version, setVersion] = useState("");
  const [pushing, setPushing] = useState(false);
  const [jobId, setJobId] = useState<string | null>(null);
  const alive = useRef(true);
  useEffect(() => {
    alive.current = true;
    return () => {
      alive.current = false;
    };
  }, []);
  const writable = repo.mode === "test-branch";

  async function call(step: Step, capability: string, input: object) {
    if (!nodeId) return;
    setBusy(step);
    try {
      const result = await api.runCapability(nodeId, capability, {
        repo: repo.name,
        ...input,
      });
      setShown({ step, result });
      return result;
    } finally {
      setBusy(null);
    }
  }

  /** A test run takes minutes and one call is cut off after seconds: start it in the background
   *  and ask for its state until it ends (the same way the assistant does). */
  async function runTests() {
    if (!nodeId) return;
    setBusy("test");
    try {
      const started = await api.runCapability(nodeId, "repo.test_start", {
        repo: repo.name,
      });
      const id = asObj<TestOut>(started.output).job_id;
      if (!started.ok || !id) {
        setShown({ step: "test", result: started });
        return;
      }
      setJobId(id);
      while (alive.current) {
        const state = await api.runCapability(nodeId, "repo.test_status", {
          repo: repo.name,
          job_id: id,
          wait_seconds: 10,
        });
        if (!alive.current) return;
        setShown({ step: "test", result: state });
        if (!state.ok || !asObj<TestOut>(state.output).running) return;
        await new Promise((r) => setTimeout(r, TEST_POLL_PAUSE_MS));
      }
    } finally {
      setJobId(null);
      if (alive.current) setBusy(null);
    }
  }

  async function cancelTests() {
    if (nodeId && jobId)
      await api.runCapability(nodeId, "repo.test_cancel", {
        repo: repo.name,
        job_id: jobId,
      });
  }

  async function status() {
    await call("status", "repo.status", {});
    if (nodeId)
      setDiff(
        await api.runCapability(nodeId, "repo.diff", { repo: repo.name }),
      );
  }

  async function reset() {
    if (!await askConfirm(t("workshop.confirmReset", { name: repo.name })))
      return;
    await call("reset", "repo.reset", {});
    setDiff(null);
  }

  const disabled = busy !== null || nodeId === null;
  return (
    <article className="proposal" data-testid="workshop-repo">
      <div className="detail-head">
        <h2>
          {repo.name} <code>{repo.full_name}</code>
        </h2>
        <span className="chip">
          {t(`repos.mode${repo.mode}`, { defaultValue: repo.mode })}
        </span>
      </div>
      {nodeId === null && <p className="muted">{t("workshop.nodeOffline")}</p>}
      <div className="dialog-actions">
        <button
          type="button"
          disabled={disabled}
          onClick={() => void call("sync", "repo.sync", {})}
        >
          {t("workshop.sync")}
        </button>
        <button type="button" disabled={disabled} onClick={() => void status()}>
          {t("workshop.status")}
        </button>
        {writable && (
          <>
            <button
              type="button"
              disabled={disabled}
              onClick={() => void runTests()}
            >
              {busy === "test" ? t("workshop.testing") : t("workshop.test")}
            </button>
            {busy === "test" && jobId && (
              <button type="button" onClick={() => void cancelTests()}>
                {t("workshop.testCancel")}
              </button>
            )}
            <button
              type="button"
              disabled={disabled}
              onClick={() => void reset()}
            >
              {t("workshop.reset")}
            </button>
            <button type="button" onClick={() => setPushing(!pushing)}>
              {t("repos.push")}
            </button>
          </>
        )}
        <button
          type="button"
          disabled={disabled}
          onClick={() => void call("builds", "repo.build_list", {})}
        >
          {t("workshop.builds")}
        </button>
      </div>
      {writable && (
        <form
          className="inline-form"
          onSubmit={(e) => {
            e.preventDefault();
            if (version.trim())
              void call("build", "repo.build_node", {
                version: version.trim(),
              });
          }}
        >
          <label>
            {t("workshop.version")}{" "}
            <input
              type="text"
              value={version}
              maxLength={40}
              placeholder="0.1.6-dev1"
              onChange={(e) => setVersion(e.target.value)}
            />
          </label>{" "}
          <button type="submit" disabled={disabled || !version.trim()}>
            {busy === "build" ? t("workshop.building") : t("workshop.build")}
          </button>
          <span className="muted small"> {t("workshop.buildHint")}</span>
        </form>
      )}
      {busy && (
        <p className="notice" role="status">
          {t("workshop.working")}
        </p>
      )}
      {shown && <Result shown={shown} diff={diff} />}
      {pushing && writable && <PushPanel api={api} repo={repo} />}
    </article>
  );
}

function Result({ shown, diff }: { shown: Shown; diff: ToolResult | null }) {
  const { t } = useTranslation();
  const { step, result } = shown;
  if (!result.ok) {
    return (
      <p className="error" role="alert">
        {result.error?.message ?? result.error?.code ?? t("workshop.failed")}
      </p>
    );
  }
  if (step === "status") {
    const s = asObj<StatusOut>(result.output);
    const d = asObj<DiffOut>(diff?.ok ? diff.output : undefined);
    return (
      <div>
        <p>
          {t("workshop.onBranch", {
            branch: s.branch ?? "?",
            head: s.head ?? "",
          })}
          {s.last_commit && <span className="muted"> {s.last_commit}</span>}
        </p>
        {(s.changed?.length ?? 0) === 0 ? (
          <p className="muted">{t("workshop.noChange")}</p>
        ) : (
          <>
            <p>{t("workshop.changed", { count: s.changed?.length ?? 0 })}</p>
            {d.masked ? (
              <p className="error">
                {t("workshop.secrets", { count: d.masked })}
              </p>
            ) : null}
            {d.truncated && <p className="muted">{t("workshop.truncated")}</p>}
            {d.diff && <pre className="diff">{d.diff}</pre>}
          </>
        )}
      </div>
    );
  }
  if (step === "test") {
    const o = asObj<TestOut>(result.output);
    const text = o.log ?? o.output;
    if (o.running)
      return (
        <div>
          <p className="notice" role="status">
            {t("workshop.testRunning", {
              seconds: Math.round((o.elapsed_ms ?? 0) / 1000),
            })}
          </p>
          {text && <pre>{text}</pre>}
        </div>
      );
    return (
      <div>
        <p className={o.passed ? "notice" : "error"}>
          {o.passed
            ? t("workshop.testPassed")
            : o.state === "canceled"
              ? t("workshop.testCanceled")
              : o.timed_out
                ? t("workshop.testTimeout")
                : o.error && o.exit_code === undefined
                  ? o.error
                  : t("workshop.testFailed", { code: o.exit_code ?? "?" })}
        </p>
        {text && <pre>{text}</pre>}
      </div>
    );
  }
  if (step === "build" || step === "builds") {
    const o = asObj<BuildOut>(result.output);
    const rows =
      step === "build"
        ? [{ version: "", files: o.artefacts ?? [], folder: o.folder ?? "" }]
        : (o.builds ?? []);
    return (
      <div>
        {step === "build" && (
          <p className={o.built ? "notice" : "error"}>
            {o.built
              ? t("workshop.buildDone")
              : t("workshop.buildFailed", { code: o.exit_code ?? "?" })}
          </p>
        )}
        {step === "builds" && rows.length === 0 && (
          <p className="muted">{t("workshop.noBuilds")}</p>
        )}
        {rows.map((b) => (
          <div key={`${b.version}${b.folder}`}>
            <p>
              {b.version && <strong>{b.version} </strong>}
              <code>{b.folder}</code>
            </p>
            <ul>
              {b.files.map((f) => (
                <li key={f.name}>
                  <code>{f.name}</code>{" "}
                  <span className="muted">
                    {Math.round(f.bytes / 1024)} KiB
                  </span>{" "}
                  <code className="small">{f.sha256.slice(0, 16)}…</code>
                </li>
              ))}
            </ul>
          </div>
        ))}
        {rows.length > 0 && (
          <p className="muted small">{t("workshop.unsigned")}</p>
        )}
        {step === "build" && !o.built && o.output && <pre>{o.output}</pre>}
      </div>
    );
  }
  return <pre>{JSON.stringify(result.output ?? {}, null, 2)}</pre>;
}
