import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import type { KeyboardEvent } from "react";
import { useTranslation } from "react-i18next";
import { UnauthorizedError, type ApiClient } from "../api/client";
import { describeError, toActionError } from "../errors";
import type { DevRepo, NodeSummary } from "../types";
import { SHOW_EVENT, takeShow, type ShowRequest } from "./bus";
import { buildTree, findNode, parentsOf, visibleRows } from "./tree";

/** Lines read per call (the node reads at most 400, about 24 KB). */
const CHUNK = 400;
/** Lines of context before a place the assistant points at. */
const CONTEXT = 30;
const MAX_PAGES = 40;

interface Mark {
  from: number;
  to: number;
}

interface Reader {
  path: string;
  /** Number of the first line held. */
  start: number;
  lines: string[];
  total: number;
  more: boolean;
  mark: Mark | null;
}

interface Problem {
  text: string;
  notCloned: boolean;
}

type Answer =
  | { ok: true; out: Record<string, unknown> }
  | { ok: false; message: string; notCloned: boolean };

/** The file reader (read only): the folder tree of a repository next to a text reader, both fed
 *  by the dev node's read capabilities (`repo.list`, `repo.read`, `repo.search`). The development
 *  assistant can point at a place (`repo.show`); the person can always click around on their own. */
export function FilesPage({
  api,
  nodes,
}: {
  api: ApiClient;
  nodes: NodeSummary[];
}) {
  const { t, i18n } = useTranslation();
  const [repos, setRepos] = useState<DevRepo[] | null>(null);
  const [repoName, setRepoName] = useState("");
  // what was listed, for which repository on which node: a change of either shows "loading" at
  // once, without resetting anything from inside an effect
  const [tree, setTree] = useState<{
    key: string;
    files: string[];
    problem: Problem | null;
  } | null>(null);
  const [open, setOpen] = useState<ReadonlySet<string>>(new Set());
  const [cursor, setCursor] = useState<string | null>(null);
  const [filter, setFilter] = useState("");
  const [reader, setReader] = useState<Reader | null>(null);
  const [failure, setFailure] = useState<Problem | null>(null);
  const [readerError, setReaderError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [wanted, setWanted] = useState<ShowRequest | null>(null);
  const markRef = useRef<HTMLDivElement | null>(null);

  const fail = useCallback(
    (err: unknown) => {
      if (!(err instanceof UnauthorizedError))
        setFailure({
          text: describeError(toActionError(err), t, i18n),
          notCloned: false,
        });
    },
    [t, i18n],
  );
  useEffect(() => {
    api.devRepos().then((l) => setRepos(l.repos), fail);
  }, [api, fail]);

  const devNodes = useMemo(
    () => nodes.filter((n) => (n.roles ?? []).includes("dev")),
    [nodes],
  );
  const repo =
    repos?.find(
      (r) => r.name.toLowerCase() === repoName.toLowerCase() && repoName !== "",
    ) ??
    repos?.find((r) => r.kind === "local") ??
    repos?.[0];
  const node = repo?.node_id
    ? nodes.find((n) => n.id === repo.node_id)
    : (devNodes.find((n) => n.connected) ?? devNodes[0]);
  const nodeId = node?.connected ? node.id : null;
  const repoKey = repo?.name ?? "";
  const key = `${nodeId ?? ""}|${repoKey}`;
  const files = tree?.key === key ? tree.files : null;
  const problem = (tree?.key === key ? tree.problem : null) ?? failure;

  const run = useCallback(
    async (
      capability: string,
      input: Record<string, unknown>,
    ): Promise<Answer> => {
      if (!nodeId || !repoKey)
        return { ok: false, message: "offline", notCloned: false };
      const r = await api.runCapability(nodeId, capability, {
        repo: repoKey,
        ...input,
      });
      if (!r.ok) {
        const message = r.error?.message ?? r.error?.code ?? t("files.failed");
        return {
          ok: false,
          message,
          notCloned: /not cloned yet/i.test(message),
        };
      }
      return { ok: true, out: (r.output ?? {}) as Record<string, unknown> };
    },
    [api, nodeId, repoKey, t],
  );

  const loadTree = useCallback(async () => {
    if (!nodeId || !repoKey) return;
    const listed = `${nodeId}|${repoKey}`;
    const all: string[] = [];
    let after = "";
    for (let page = 0; page < MAX_PAGES; page++) {
      const a = await run(
        "repo.list",
        after ? { limit: 1000, after } : { limit: 1000 },
      );
      if (!a.ok) {
        setTree({
          key: listed,
          files: [],
          problem: { text: a.message, notCloned: a.notCloned },
        });
        return;
      }
      all.push(...((a.out.files as string[] | undefined) ?? []));
      const next = a.out.next_after;
      if (typeof next !== "string" || !next) break;
      after = next;
    }
    setTree({ key: listed, files: all, problem: null });
  }, [run, nodeId, repoKey]);
  useEffect(() => {
    // fetched after this render, not inside it; an answer for an earlier repository is ignored
    // by its key
    void Promise.resolve().then(loadTree);
  }, [loadTree]);

  const root = useMemo(() => buildTree(files ?? []), [files]);
  const rows = useMemo(
    () => visibleRows(root, open, filter),
    [root, open, filter],
  );

  async function readChunk(path: string, start: number, end: number) {
    const a = await run("repo.read", {
      path,
      start_line: start,
      end_line: end,
    });
    if (!a.ok) return { error: a.message } as const;
    const text = typeof a.out.text === "string" ? a.out.text : "";
    return {
      start: Number(a.out.start_line ?? start),
      lines: text === "" ? [] : text.split("\n"),
      total: Number(a.out.total_lines ?? 0),
      more: a.out.more_after === true,
    } as const;
  }

  const openFile = useCallback(
    async (
      path: string,
      at?: { from?: number; to?: number; find?: string },
    ) => {
      setCursor(path);
      setOpen((prev) => new Set([...prev, ...parentsOf(path)]));
      setBusy(true);
      setReaderError(null);
      let mark: Mark | null = null;
      if (at?.from)
        mark = { from: at.from, to: Math.max(at.to ?? at.from, at.from) };
      else if (at?.find) {
        const found = await run("repo.search", { query: at.find, path });
        const hit =
          found.ok && typeof found.out.text === "string"
            ? found.out.text.split("\n")[0]
            : "";
        const m = /:(\d+):/.exec(hit ?? "");
        if (m) mark = { from: Number(m[1]), to: Number(m[1]) };
      }
      const start = mark ? Math.max(1, mark.from - CONTEXT) : 1;
      const got = await readChunk(path, start, start + CHUNK - 1);
      setBusy(false);
      if ("error" in got) {
        setReader(null);
        setReaderError(got.error ?? t("files.failed"));
        return;
      }
      setReader({ path, ...got, mark });
    },
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [run, t],
  );

  // what the assistant asked to be shown (`repo.show`), once the tree is there
  useEffect(() => {
    const take = () => {
      const request = takeShow();
      if (!request) return;
      setWanted(request);
      if (request.repo) setRepoName(request.repo); // that repository's tree is loaded next
    };
    take();
    window.addEventListener(SHOW_EVENT, take);
    return () => window.removeEventListener(SHOW_EVENT, take);
  }, []);
  useEffect(() => {
    if (!wanted || !repos) return;
    const named = wanted.repo
      ? repos.find((r) => r.name.toLowerCase() === wanted.repo?.toLowerCase())
      : undefined;
    if (named && named.name !== repoKey) return; // not switched yet
    if (files === null) return; // its tree is not there yet
    let live = true;
    // after this render, not inside it
    void Promise.resolve().then(() => {
      if (!live) return;
      setWanted(null);
      const target = findNode(root, wanted.path);
      if (target?.dir) {
        setOpen(
          (prev) => new Set([...prev, ...parentsOf(wanted.path), wanted.path]),
        );
        setCursor(wanted.path);
        setReader(null);
        setReaderError(null);
      } else if (target) {
        void openFile(wanted.path, wanted);
      } else {
        setReader(null);
        setReaderError(t("files.notFound", { path: wanted.path }));
      }
    });
    return () => {
      live = false;
    };
  }, [wanted, repos, repoKey, files, root, openFile, t]);

  useEffect(() => {
    markRef.current?.scrollIntoView?.({ block: "center" });
  }, [reader?.path, reader?.mark?.from, reader?.start]);

  async function earlier() {
    if (!reader || reader.start <= 1) return;
    const from = Math.max(1, reader.start - CHUNK);
    const got = await readChunk(reader.path, from, reader.start - 1);
    if ("error" in got) return setReaderError(got.error ?? t("files.failed"));
    setReader({
      ...reader,
      start: got.start,
      lines: [...got.lines, ...reader.lines],
    });
  }
  async function later() {
    if (!reader) return;
    const from = reader.start + reader.lines.length;
    const got = await readChunk(reader.path, from, from + CHUNK - 1);
    if ("error" in got) return setReaderError(got.error ?? t("files.failed"));
    setReader({
      ...reader,
      lines: [...reader.lines, ...got.lines],
      more: got.more,
      total: got.total,
    });
  }

  function toggle(path: string) {
    setOpen((prev) => {
      const next = new Set(prev);
      if (next.has(path)) next.delete(path);
      else next.add(path);
      return next;
    });
  }
  function activate(path: string, dir: boolean) {
    setCursor(path);
    if (dir) toggle(path);
    else void openFile(path);
  }
  function onKey(e: KeyboardEvent<HTMLDivElement>) {
    if (rows.length === 0) return;
    const at = Math.max(
      0,
      rows.findIndex((r) => r.node.path === cursor),
    );
    const row = rows[at]!;
    const move = (to: number) => {
      e.preventDefault();
      setCursor(rows[Math.min(Math.max(to, 0), rows.length - 1)]!.node.path);
    };
    if (e.key === "ArrowDown") move(at + 1);
    else if (e.key === "ArrowUp") move(at - 1);
    else if (e.key === "Home") move(0);
    else if (e.key === "End") move(rows.length - 1);
    else if (
      e.key === "ArrowRight" &&
      row.node.dir &&
      !open.has(row.node.path)
    ) {
      e.preventDefault();
      toggle(row.node.path);
    } else if (e.key === "ArrowLeft") {
      e.preventDefault();
      if (row.node.dir && open.has(row.node.path)) toggle(row.node.path);
      else {
        const parent = parentsOf(row.node.path).at(-1);
        if (parent !== undefined) setCursor(parent);
      }
    } else if (e.key === "Enter" || e.key === " ") {
      e.preventDefault();
      activate(row.node.path, row.node.dir);
    }
  }

  if (repos !== null && devNodes.length === 0 && !repos.some((r) => r.node_id))
    return (
      <section className="files">
        <h1>{t("files.heading")}</h1>
        <p className="muted">{t("files.noNode")}</p>
      </section>
    );
  if (repos !== null && repos.length === 0)
    return (
      <section className="files">
        <h1>{t("files.heading")}</h1>
        <p className="muted">{t("files.noRepos")}</p>
      </section>
    );

  const last = reader ? reader.start + reader.lines.length - 1 : 0;
  return (
    <section className="files" data-testid="files">
      <header className="files-head">
        <h1>{t("files.heading")}</h1>
        {repos && repos.length > 1 && (
          <select
            aria-label={t("files.repo")}
            value={repoKey}
            onChange={(e) => {
              setRepoName(e.target.value);
              setReader(null);
              setCursor(null);
              setOpen(new Set());
            }}
          >
            {repos.map((r) => (
              <option key={r.id} value={r.name}>
                {r.name}
              </option>
            ))}
          </select>
        )}
        {repo && repos && repos.length === 1 && <code>{repo.name}</code>}
        {node && (
          <span className="muted small">
            {node.name}
            {node.connected ? "" : ` (${t("files.offline")})`}
          </span>
        )}
        <button
          type="button"
          disabled={!nodeId}
          onClick={() => {
            setTree(null);
            setFailure(null);
            void loadTree();
          }}
        >
          {t("files.reload")}
        </button>
      </header>
      {problem && (
        <p className="error" role="alert">
          {problem.notCloned ? t("files.notSynced") : problem.text}{" "}
          {problem.notCloned && (
            <button
              type="button"
              onClick={() =>
                void run("repo.sync", {}).then((a) =>
                  a.ok
                    ? loadTree()
                    : setFailure({ text: a.message, notCloned: false }),
                )
              }
            >
              {t("files.sync")}
            </button>
          )}
        </p>
      )}
      <div className="files-panes">
        <div className="files-treepane">
          <input
            type="search"
            className="files-filter"
            placeholder={t("files.filter")}
            aria-label={t("files.filter")}
            value={filter}
            onChange={(e) => setFilter(e.target.value)}
          />
          <div
            className="files-tree"
            role="tree"
            tabIndex={0}
            data-testid="files-tree"
            aria-label={t("files.heading")}
            onKeyDown={onKey}
          >
            {files === null && <p className="muted">{t("files.loading")}</p>}
            {files !== null && rows.length === 0 && !problem && (
              <p className="muted">{t("files.empty")}</p>
            )}
            {rows.map(({ node: n, depth }) => (
              <div
                key={n.path}
                role="treeitem"
                aria-selected={cursor === n.path}
                aria-expanded={
                  n.dir ? open.has(n.path) || filter !== "" : undefined
                }
                className={`files-row${cursor === n.path ? " files-cursor" : ""}${n.dir ? " files-dir" : ""}`}
                style={{ paddingLeft: `${0.4 + depth * 1.1}rem` }}
                data-testid="files-row"
                onClick={() => activate(n.path, n.dir)}
              >
                <span aria-hidden="true" className="files-mark">
                  {n.dir ? (open.has(n.path) || filter ? "▾" : "▸") : "·"}
                </span>{" "}
                {n.name}
                {n.dir ? "/" : ""}
              </div>
            ))}
          </div>
        </div>
        <div className="files-reader" data-testid="files-reader">
          {readerError && (
            <p className="error" role="alert">
              {readerError}
            </p>
          )}
          {!reader && !readerError && (
            <p className="muted">
              {busy ? t("files.loading") : t("files.pick")}
            </p>
          )}
          {reader && (
            <>
              <div className="files-readerhead">
                <code>{reader.path}</code>
                <span className="muted small">
                  {t("files.lines", {
                    from: reader.start,
                    to: last,
                    total: reader.total,
                  })}
                </span>
                <button
                  type="button"
                  onClick={() =>
                    void navigator.clipboard?.writeText(reader.path)
                  }
                >
                  {t("files.copyPath")}
                </button>
              </div>
              {reader.start > 1 && (
                <button type="button" onClick={() => void earlier()}>
                  {t("files.earlier")}
                </button>
              )}
              <div
                className="files-code"
                role="region"
                aria-label={reader.path}
              >
                {reader.lines.map((text, i) => {
                  const n = reader.start + i;
                  const marked =
                    reader.mark && n >= reader.mark.from && n <= reader.mark.to;
                  return (
                    <div
                      key={n}
                      ref={
                        marked && n === reader.mark?.from ? markRef : undefined
                      }
                      className={`files-line${marked ? " files-marked" : ""}`}
                      data-testid={marked ? "files-marked" : "files-line"}
                    >
                      <span className="files-ln" aria-hidden="true">
                        {n}
                      </span>
                      <span className="files-tx">
                        {text === "" ? " " : text}
                      </span>
                    </div>
                  );
                })}
              </div>
              {reader.more && (
                <button type="button" onClick={() => void later()}>
                  {t("files.later")}
                </button>
              )}
            </>
          )}
        </div>
      </div>
    </section>
  );
}
