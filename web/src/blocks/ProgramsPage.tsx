import { useCallback, useEffect, useRef, useState } from "react";
import { useTranslation } from "react-i18next";
import type { ApiClient } from "../api/client";
import { describeError, toActionError } from "../errors";
import type { BlockCatalogue, BlockProgramInfo, BlockRunView } from "../types";
import { BlockEditor } from "./BlockEditor";
import { checked, type Program } from "./edit";
import { childGroups, liveByPath, topLevelProgress, type ChainState, type Live } from "./chain";

type Block = Record<string, unknown> & { b: string };

/** The children of a block, as labelled stacks: C-blocks show what they hold. */
export function childStacks(block: Block): { label: string; blocks: Block[] }[] {
  const out: { label: string; blocks: Block[] }[] = [];
  const stack = (key: string, label: string) => {
    const value = block[key];
    if (Array.isArray(value) && (value.length > 0 || key === "then")) {
      out.push({ label, blocks: value as Block[] });
    }
  };
  stack("then", "then");
  stack("else", "else");
  stack("stack", "do");
  if (Array.isArray(block.lanes)) {
    (block.lanes as Block[][]).forEach((lane, i) => out.push({ label: `lane ${i + 1}`, blocks: lane }));
  }
  return out;
}

/** One line that says what a block does, from its fields. */
export function summary(block: Block): string {
  const bits: string[] = [];
  const text = (key: string) => (typeof block[key] === "string" ? (block[key] as string) : "");
  if (text("prompt")) bits.push(`"${text("prompt").slice(0, 60)}"`);
  if (text("in")) bits.push(text("in"));
  if (Array.isArray(block.in)) bits.push((block.in as string[]).join(" + "));
  if (text("draft")) bits.push(text("draft"));
  if (text("cond")) bits.push(text("cond"));
  if (text("mode")) bits.push(text("mode"));
  if (text("value")) bits.push(`"${text("value").slice(0, 40)}"`);
  const pick = block.pick as Record<string, unknown> | undefined;
  if (pick) {
    bits.push(
      "pick " +
        Object.entries(pick)
          .map(([k, v]) => `${k}=${Array.isArray(v) ? v.join(",") : String(v)}`)
          .join(" "),
    );
  }
  const hooks = block.hooks as Record<string, unknown> | undefined;
  if (hooks) {
    bits.push(
      "hooks " +
        Object.entries(hooks)
          .map(([k, v]) => (typeof v === "boolean" ? `${v ? "+" : "-"}${k}` : `${k}=${String(v)}`))
          .join(" "),
    );
  }
  if (typeof block.max === "number" && block.max > 0) bits.push(`max ${block.max}`);
  if (text("out")) bits.push(`→ ${text("out")}`);
  return bits.join(" · ");
}

function BlockTree({ blocks }: { blocks: Block[] }) {
  return (
    <ol className="block-tree">
      {blocks.map((block, i) => (
        <li key={i} data-testid={`block-${block.b}`}>
          <span className="block-kind">{block.b}</span> <span className="muted small">{summary(block)}</span>
          {childStacks(block).map((c) => (
            <div key={c.label} className="block-child">
              <span className="muted small">{c.label}</span>
              <BlockTree blocks={c.blocks} />
            </div>
          ))}
        </li>
      ))}
    </ol>
  );
}

function parse(text: string): { program: Record<string, unknown> | null; error: string } {
  try {
    const value: unknown = JSON.parse(text);
    if (value && typeof value === "object" && !Array.isArray(value)) {
      return { program: value as Record<string, unknown>, error: "" };
    }
    return { program: null, error: "not an object" };
  } catch (err) {
    return { program: null, error: err instanceof Error ? err.message : "invalid JSON" };
  }
}

export function ProgramsPage({ api }: { api: ApiClient }) {
  const { t, i18n } = useTranslation();
  const [programs, setPrograms] = useState<BlockProgramInfo[]>([]);
  const [selected, setSelected] = useState<string>("");
  const [text, setText] = useState("");
  const [message, setMessage] = useState<{ kind: "ok" | "bad"; text: string } | null>(null);
  const [saveAs, setSaveAs] = useState("");
  const [inputs, setInputs] = useState<Record<string, string>>({});
  const [run, setRun] = useState<BlockRunView | null>(null);
  const [catalogue, setCatalogue] = useState<BlockCatalogue | null>(null);
  const [history, setHistory] = useState<string[]>([]); // earlier texts of the source, for Undo
  const timer = useRef<ReturnType<typeof setInterval> | null>(null);

  const fail = useCallback(
    (err: unknown) => setMessage({ kind: "bad", text: describeError(toActionError(err), t, i18n) }),
    [t, i18n],
  );

  const load = useCallback(
    (keep?: string) =>
      api.getBlockPrograms().then((list) => {
        setPrograms(list);
        const pick = list.find((p) => p.name === (keep ?? selected)) ?? list[0];
        if (pick) {
          setSelected(pick.name);
          setText(JSON.stringify(pick.program, null, 2));
          setSaveAs(pick.builtin ? `${pick.name}_copy` : pick.name);
        }
      }, fail),
    // selected is read once on first load only
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [api, fail],
  );
  useEffect(() => {
    void load();
  }, [load]);
  useEffect(() => {
    api.getBlockCatalogue().then(setCatalogue, () => undefined); // the editor works without it
  }, [api]);
  useEffect(() => () => {
    if (timer.current) clearInterval(timer.current);
  }, []);

  const current = programs.find((p) => p.name === selected);
  const parsed = parse(text);
  const stack = Array.isArray(parsed.program?.stack) ? (parsed.program?.stack as Block[]) : [];
  const inputNames = Object.keys((parsed.program?.inputs as Record<string, string>) ?? {});

  function choose(p: BlockProgramInfo) {
    setSelected(p.name);
    setText(JSON.stringify(p.program, null, 2));
    setSaveAs(p.builtin ? `${p.name}_copy` : p.name);
    setMessage(null);
    setRun(null);
    setHistory([]);
  }

  // The program as the editor edits it (null while the source is not valid JSON of that shape).
  const editable =
    parsed.program && Array.isArray(parsed.program.stack) && parsed.program.inputs
      ? (parsed.program as unknown as Program)
      : null;

  /** A change from the editor. A change of structure that would leave more problems than there
   *  were (a read before the write, the wrong type) is refused and says why. */
  function commit(next: Program, structural: boolean): boolean {
    if (structural && editable) {
      const verdict = checked(editable, next);
      if (!verdict.ok) {
        setMessage({ kind: "bad", text: t("blocksPage.refused", { why: verdict.message }) });
        return false;
      }
    }
    setHistory((h) => [...h.slice(-49), text]);
    setText(JSON.stringify(next, null, 2));
    setMessage(null);
    return true;
  }

  function undo() {
    const last = history[history.length - 1];
    if (last === undefined) return;
    setHistory(history.slice(0, -1));
    setText(last);
    setMessage(null);
  }

  async function validate() {
    if (!parsed.program) return setMessage({ kind: "bad", text: parsed.error });
    try {
      const r = await api.validateBlockProgram(parsed.program);
      setMessage(r.ok ? { kind: "ok", text: t("blocksPage.valid") } : { kind: "bad", text: r.error });
    } catch (err) {
      fail(err);
    }
  }

  async function save() {
    if (!parsed.program) return setMessage({ kind: "bad", text: parsed.error });
    const name = saveAs.trim();
    try {
      await api.saveBlockProgram(name, { ...parsed.program, name });
      setMessage({ kind: "ok", text: t("blocksPage.saved", { name }) });
      await load(name);
    } catch (err) {
      fail(err);
    }
  }

  async function remove() {
    if (!current || current.builtin) return;
    try {
      await api.deleteBlockProgram(current.name);
      setMessage({ kind: "ok", text: t("blocksPage.deleted", { name: current.name }) });
      setSelected("");
      await load("");
    } catch (err) {
      fail(err);
    }
  }

  async function start() {
    if (!parsed.program) return setMessage({ kind: "bad", text: parsed.error });
    try {
      const started = await api.startBlockRun(parsed.program, inputs);
      setRun(started);
      setMessage(null);
      if (timer.current) clearInterval(timer.current);
      timer.current = setInterval(() => {
        api.getBlockRun(started.id).then(
          (view) => {
            setRun(view);
            if (view.status !== "running" && timer.current) clearInterval(timer.current);
          },
          () => timer.current && clearInterval(timer.current),
        );
      }, 2000);
    } catch (err) {
      fail(err);
    }
  }

  async function stop() {
    if (!run) return;
    try {
      await api.stopBlockRun(run.id);
    } catch (err) {
      fail(err);
    }
  }

  const steps = (run?.steps ?? []).filter((s) => s.event === "step" && s.state === "done" || s.event === "pick" || s.event === "loop_end");
  // the live view: the state of every block of the program, from the run's events
  const live = liveByPath(run?.steps ?? [], run?.status === "failed");
  const progress = topLevelProgress(stack, live);

  /** The program as a chain: each block with its state, and for the one running now: which node
   *  and model it uses and what it does (the agent's plan, its last call, a wait). */
  function renderChain(blocks: Block[], base: string) {
    return (
      <ol className="chain">
        {blocks.map((block, i) => {
          const path = `${base}[${i}]`;
          const l: Live = live.get(path) ?? { state: "waiting" as ChainState };
          const active = l.state === "active";
          return (
            <li
              key={path}
              className={`chain-block chain-${l.state}`}
              data-testid={`chain-${path}`}
              aria-current={active ? "step" : undefined}
            >
              <div className="chain-head">
                <span className="chain-state">{t(`blocksPage.live.state.${l.state}`)}</span>{" "}
                <span className="block-kind">{block.b}</span>{" "}
                <span className="muted small">{summary(block)}</span>
                {l.state === "done" && l.started !== undefined && l.ended !== undefined && (
                  <span className="muted small"> · {Math.max(0, l.ended - l.started).toFixed(1)} s</span>
                )}
              </div>
              {active && (
                <div className="chain-live small">
                  {(l.node || l.model) && (
                    <span>
                      {t("blocksPage.live.where", { node: l.node || "–", model: l.model || "–" })}
                    </span>
                  )}
                  {l.activity && (
                    <span> · {t(`blocksPage.live.act.${l.activity.key}`, l.activity.values)}</span>
                  )}
                </div>
              )}
              {l.loop && (
                <div className="muted small">
                  {t("blocksPage.loopEnd", { reason: l.loop.reason, rounds: l.loop.rounds })}
                </div>
              )}
              {childGroups(path, block).map((g) => (
                <div key={g.base} className="chain-child">
                  <span className="muted small">{g.label}</span>
                  {renderChain(g.blocks as Block[], g.base)}
                </div>
              ))}
            </li>
          );
        })}
      </ol>
    );
  }

  return (
    <section className="blocks-page" data-testid="programs-page">
      <h1>{t("blocksPage.title")}</h1>
      <p className="muted">{t("blocksPage.intro")}</p>

      <div className="card">
        <h2>{t("blocksPage.list")}</h2>
        <ul className="plain" role="list">
          {programs.map((p) => (
            <li key={p.name}>
              <button
                type="button"
                className={p.name === selected ? "primary" : ""}
                aria-pressed={p.name === selected}
                onClick={() => choose(p)}
              >
                {p.title}
              </button>{" "}
              <span className="muted small">{p.builtin ? t("blocksPage.shipped") : t("blocksPage.mine")}</span>
            </li>
          ))}
        </ul>
      </div>

      {current && (
        <>
          {message && (
            <p
              className={message.kind === "ok" ? "muted small" : "bad"}
              role={message.kind === "ok" ? "status" : "alert"}
            >
              {message.text}
            </p>
          )}
          <div className="card">
            <h2>{t("blocksPage.picture")}</h2>
            {editable ? (
              <BlockEditor
                program={editable}
                catalogue={catalogue}
                onStructure={(next) => commit(next, true)}
                onFields={(next) => void commit(next, false)}
                onRefuse={(why) => setMessage({ kind: "bad", text: why })}
              />
            ) : parsed.program ? (
              <BlockTree blocks={stack} />
            ) : (
              <p className="bad">{parsed.error}</p>
            )}
            <div className="actions">
              <button type="button" disabled={history.length === 0} onClick={undo}>
                {t("blocksPage.undo")}
              </button>
            </div>
          </div>

          <div className="card">
            <h2>{t("blocksPage.source")}</h2>
            <textarea
              aria-label={t("blocksPage.source")}
              rows={18}
              spellCheck={false}
              className="mono"
              value={text}
              onChange={(e) => setText(e.target.value)}
            />
            <div className="actions">
              <button type="button" onClick={() => void validate()}>
                {t("blocksPage.validate")}
              </button>
              <label>
                {t("blocksPage.saveAs")}{" "}
                <input value={saveAs} onChange={(e) => setSaveAs(e.target.value)} />
              </label>
              <button type="button" className="primary" onClick={() => void save()}>
                {t("blocksPage.save")}
              </button>
              {!current.builtin && (
                <button type="button" onClick={() => void remove()}>
                  {t("blocksPage.delete")}
                </button>
              )}
            </div>
          </div>

          <div className="card">
            <h2>{t("blocksPage.run")}</h2>
            {inputNames.map((name) => (
              <label key={name} className="form-grid">
                <span>{name}</span>
                <textarea
                  aria-label={name}
                  rows={2}
                  value={inputs[name] ?? ""}
                  onChange={(e) => setInputs({ ...inputs, [name]: e.target.value })}
                />
              </label>
            ))}
            <div className="actions">
              <button type="button" className="primary" onClick={() => void start()} disabled={run?.status === "running"}>
                {t("blocksPage.start")}
              </button>
              {run?.status === "running" && (
                <button type="button" onClick={() => void stop()}>
                  {t("blocksPage.stop")}
                </button>
              )}
            </div>
            {run && (
              <div data-testid="block-run">
                <p>
                  <strong>{t(`blocksPage.status.${run.status}`, { defaultValue: run.status })}</strong>
                  {run.error && <span className="bad"> {run.error}</span>}
                </p>
                {run.notes.map((n) => (
                  <p key={n} className="muted small">
                    {n}
                  </p>
                ))}
                <div className="chain-card" data-testid="block-chain">
                  <h3>{t("blocksPage.live.title")}</h3>
                  <p className="small">{t("blocksPage.live.progress", { done: progress.done, total: progress.total })}</p>
                  {renderChain(stack, "stack")}
                </div>
                <details>
                  <summary>{t("blocksPage.live.log")}</summary>
                <ol className="block-steps">
                  {steps.map((s, i) => (
                    <li key={i} className="small">
                      {s.event === "pick"
                        ? `${t("blocksPage.picked")}: ${String(s.model)}${s.node ? ` @ ${String(s.node)}` : ""}`
                        : s.event === "loop_end"
                          ? `${String(s.path)}: ${t("blocksPage.loopEnd", { reason: String(s.reason), rounds: Number(s.rounds) })}`
                          : `${String(s.path)} ${String(s.b)}${s.preview ? ` — ${String(s.preview)}` : ""}`}
                    </li>
                  ))}
                </ol>
                </details>
                {Object.entries(run.variables).map(([name, value]) => (
                  <details key={name}>
                    <summary>
                      {name}
                      {run.authors[name] ? ` (${run.authors[name]})` : ""}
                    </summary>
                    <pre className="small">{typeof value === "string" ? value : JSON.stringify(value, null, 2)}</pre>
                  </details>
                ))}
                {run.filed.length > 0 && (
                  <p className="small">{t("blocksPage.filed", { ids: run.filed.join(", ") })}</p>
                )}
              </div>
            )}
          </div>
        </>
      )}
    </section>
  );
}
