// The theme workshop (2026-11-17): a theme talked into shape with the house model while the
// desktop draws it live. Each message is one patch the hub applied and checked
// (api/theme_workshop.py); the controls change colours and motion at once, without a model. Every
// state is kept (back steps); saving is the person's click: it stores the theme and, when the
// workshop was opened from a task, files it there as the next iteration (project_iterations.py).
import { useEffect, useMemo, useRef, useState } from "react";
import { useTranslation } from "react-i18next";
import { UnauthorizedError, type ApiClient } from "../api/client";
import { describeError, toActionError } from "../errors";
import { previewTheme } from "./preview";
import { themeProblems, type ThemeManifest } from "./theme";
import { COLOUR_VARS, asHex, playOn, readVars, setVar } from "./workshop";

interface Line {
  role: "person" | "workshop" | "hub";
  text: string;
  changed?: string[];
}
interface Step {
  message: string;
  reply: string;
}
interface Draft {
  theme: ThemeManifest;
  lines: Line[];
  steps: Step[];
}

const MOTIONS = ["none", "fade", "slide", "flicker", "zoom"] as const;
const LAYOUTS = ["cascade", "tile", "columns", "rows", "focus", "nest"] as const;
const BARS = ["bottom", "top", "left", "right", "hidden"] as const;
const BASES = ["terminal", "leitstand", "schreibtisch"] as const;
const MAX_UNDO = 60;

function storageKey(task?: string, base?: string, key?: string): string {
  return `topcoat.workshop.${task || key || base || "new"}`;
}

function loadDraft(k: string): Draft | null {
  try {
    const raw = window.localStorage.getItem(k);
    return raw ? (JSON.parse(raw) as Draft) : null;
  } catch {
    return null;
  }
}

function keepDraft(k: string, d: Draft | null): void {
  try {
    if (d) window.localStorage.setItem(k, JSON.stringify(d));
    else window.localStorage.removeItem(k);
  } catch {
    /* a private window: the draft lives only as long as the page */
  }
}

export function ThemeWorkshop({
  api,
  task,
  base,
  themeKey,
}: {
  api: ApiClient;
  task?: string;
  base?: string;
  themeKey?: string;
}) {
  const { t, i18n } = useTranslation();
  const store = storageKey(task, base, themeKey);
  const [theme, setTheme] = useState<ThemeManifest | null>(null);
  const [lines, setLines] = useState<Line[]>([]);
  const [steps, setSteps] = useState<Step[]>([]);
  const [undo, setUndo] = useState<ThemeManifest[]>([]);
  const [lint, setLint] = useState<string[]>([]);
  const [message, setMessage] = useState("");
  const [keepAsRequirement, setKeepAsRequirement] = useState(false);
  const [requirements, setRequirements] = useState<string[]>([]);
  const [taskInfo, setTaskInfo] = useState<{ title?: string; number?: string; revision?: number } | null>(null);
  const [live, setLive] = useState(true);
  const [busy, setBusy] = useState(false);
  const [notice, setNotice] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const root = useRef<HTMLDivElement>(null);

  const fail = (err: unknown) => {
    if (!(err instanceof UnauthorizedError)) setError(describeError(toActionError(err), t, i18n));
  };

  // the start: a draft of this workshop the browser kept, else the theme of the result / stored
  // theme it was opened with, else an empty one
  useEffect(() => {
    let gone = false;
    const kept = loadDraft(store);
    if (kept?.theme) {
      // eslint-disable-next-line react-hooks/set-state-in-effect -- the browser-kept draft is read once at the start
      setTheme(kept.theme);
      setLines(kept.lines ?? []);
      setSteps(kept.steps ?? []);
      setNotice(t("themeWorkshop.restored"));
    } else {
      api
        .workshopStart({ base, key: themeKey })
        .then((s) => {
          if (gone) return;
          setTheme(s.theme);
          setLint(s.lint);
        })
        .catch(fail);
    }
    if (task) {
      api
        .projectIterations(task)
        .then((d) => {
          if (gone) return;
          setRequirements(d.requirements.map((r) => r.text));
          setTaskInfo({ title: d.title, number: d.number, revision: d.revision });
        })
        .catch(fail);
    }
    return () => {
      gone = true;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [store]);

  const problems = useMemo(() => (theme ? themeProblems(theme) : []), [theme]);

  // the desktop draws the draft while the workshop is open (nothing is stored)
  useEffect(() => {
    previewTheme(live && theme && problems.length === 0 ? theme : null);
  }, [theme, live, problems.length]);
  useEffect(() => () => previewTheme(null), []);

  useEffect(() => {
    if (theme) keepDraft(store, { theme, lines: lines.slice(-80), steps });
  }, [store, theme, lines, steps]);

  const change = (next: ThemeManifest, step?: Step, coalesce?: string) => {
    if (!theme) return;
    setUndo((u) => [...u, theme].slice(-MAX_UNDO));
    setTheme(next);
    if (step) {
      setSteps((s) => {
        const last = s.at(-1);
        if (coalesce && last?.message.startsWith(coalesce)) return [...s.slice(0, -1), step];
        return [...s, step];
      });
    }
  };

  async function send() {
    if (!theme || !message.trim()) return;
    const text = message.trim();
    setBusy(true);
    setError(null);
    setLines((l) => [...l, { role: "person", text }]);
    setMessage("");
    try {
      const history = lines
        .filter((l) => l.role !== "hub")
        .slice(-12)
        .map((l) => ({ role: l.role as "person" | "workshop", text: l.text }));
      const out = await api.workshopTurn({ theme, message: text, history, requirements });
      if (out.applied) change(out.theme, { message: text, reply: out.reply });
      setLint(out.lint);
      const extra: Line[] = [];
      if (!out.applied && out.problems.length > 0)
        extra.push({ role: "hub", text: t("themeWorkshop.notApplied", { list: out.problems.join("; ") }) });
      setLines((l) => [
        ...l,
        { role: "workshop", text: out.reply || t("themeWorkshop.noReply"), changed: out.changed },
        ...extra,
      ]);
      if (keepAsRequirement && task && taskInfo?.revision !== undefined) {
        const items = [...requirements, text];
        const node = await api.putProjectRequirements(task, { revision: taskInfo.revision, items });
        setRequirements((node.requirements ?? []).map((r) => r.text));
        setTaskInfo((i) => ({ ...i, revision: node.revision }));
        setKeepAsRequirement(false);
      }
    } catch (err) {
      fail(err);
    } finally {
      setBusy(false);
    }
  }

  async function fixContrast() {
    if (!theme) return;
    setBusy(true);
    setError(null);
    try {
      const out = await api.workshopFixContrast(theme);
      if (out.changed.length > 0) {
        change(out.theme, { message: t("themeWorkshop.fixContrast"), reply: out.changed.join(", ") });
        setLines((l) => [...l, { role: "hub", text: t("themeWorkshop.contrastFixed"), changed: out.changed }]);
      }
      setLint(out.lint);
    } catch (err) {
      fail(err);
    } finally {
      setBusy(false);
    }
  }

  async function save() {
    if (!theme) return;
    setBusy(true);
    setError(null);
    try {
      const out = await api.workshopSave({ theme, task_id: task ?? null, based_on: base ?? null, steps, store: true });
      keepDraft(store, null);
      setNotice(
        out.iteration
          ? t("themeWorkshop.savedIteration", { name: theme.name, n: out.iteration, number: out.number })
          : t("themeWorkshop.saved", { name: theme.name }),
      );
      setSteps([]);
    } catch (err) {
      fail(err);
    } finally {
      setBusy(false);
    }
  }

  if (!theme) {
    return (
      <div className="workshop" ref={root}>
        {error ? <p className="error">{error}</p> : <p className="muted">{t("themeWorkshop.loading")}</p>}
      </div>
    );
  }

  const vars = new Map(readVars(theme.css));
  const colourNames = [...new Set([...COLOUR_VARS, ...[...vars.keys()].filter((k) => asHex(vars.get(k)))])];
  const windowEl = () => root.current?.closest<HTMLElement>(".dt-program") ?? null;
  const setMotion = (patch: Partial<ThemeManifest["motion"]>, what: string) =>
    change({ ...theme, motion: { ...theme.motion, ...patch } }, { message: `${t("themeWorkshop.control")}: ${what}`, reply: "" }, `${t("themeWorkshop.control")}: ${what.split("=")[0]}`);

  return (
    <div className="workshop" ref={root} data-testid="theme-workshop">
      <header className="workshop-head">
        <input
          aria-label={t("themeWorkshop.name")}
          value={theme.name}
          maxLength={60}
          onChange={(e) => change({ ...theme, name: e.target.value })}
        />
        <code>{theme.key}</code>
        {taskInfo?.number && (
          <span className="muted small">
            {t("themeWorkshop.forTask", { number: taskInfo.number, title: taskInfo.title ?? "" })}
          </span>
        )}
        <label className="small">
          <input type="checkbox" checked={live} onChange={(e) => setLive(e.target.checked)} /> {t("themeWorkshop.live")}
        </label>
        <button
          type="button"
          disabled={undo.length === 0}
          onClick={() => {
            const prev = undo.at(-1);
            if (!prev) return;
            setUndo((u) => u.slice(0, -1));
            setTheme(prev);
            setSteps((s) => s.slice(0, -1));
          }}
        >
          {t("themeWorkshop.undo")}
        </button>
        <button type="button" className="primary" disabled={busy || problems.length > 0} onClick={() => void save()}>
          {task ? t("themeWorkshop.saveIteration") : t("themeWorkshop.save")}
        </button>
      </header>
      {notice && <p role="status" className="small">{notice}</p>}
      {error && (
        <p className="error small" role="alert">
          {error}
        </p>
      )}

      <div className="workshop-cols">
        <section className="workshop-chat" aria-label={t("themeWorkshop.chat")}>
          <ol className="workshop-lines">
            {lines.length === 0 && <li className="muted">{t("themeWorkshop.hello")}</li>}
            {lines.map((l, i) => (
              <li key={i} className={`workshop-line workshop-${l.role}`}>
                {l.text}
                {l.changed && l.changed.length > 0 && <span className="muted small"> ({l.changed.join(", ")})</span>}
              </li>
            ))}
            {busy && <li className="muted">{t("themeWorkshop.working")}</li>}
          </ol>
          <form
            onSubmit={(e) => {
              e.preventDefault();
              void send();
            }}
          >
            <textarea
              aria-label={t("themeWorkshop.message")}
              rows={3}
              value={message}
              placeholder={t("themeWorkshop.placeholder")}
              maxLength={4000}
              onChange={(e) => setMessage(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === "Enter" && !e.shiftKey) {
                  e.preventDefault();
                  void send();
                }
              }}
            />
            <div className="workshop-send">
              {task && (
                <label className="small">
                  <input
                    type="checkbox"
                    checked={keepAsRequirement}
                    onChange={(e) => setKeepAsRequirement(e.target.checked)}
                  />{" "}
                  {t("themeWorkshop.keep")}
                </label>
              )}
              <button type="submit" disabled={busy || !message.trim()}>
                {t("themeWorkshop.send")}
              </button>
            </div>
          </form>
        </section>

        <aside className="workshop-controls" aria-label={t("themeWorkshop.controls")}>
          {requirements.length > 0 && (
            <details open>
              <summary>{t("themeWorkshop.requirements")}</summary>
              <ul className="small">
                {requirements.map((r) => (
                  <li key={r}>{r}</li>
                ))}
              </ul>
            </details>
          )}
          <details open>
            <summary>{t("themeWorkshop.colours")}</summary>
            <div className="workshop-colours">
              {colourNames.map((name) => {
                const value = vars.get(name);
                return (
                  <label key={name} className="small">
                    <input
                      type="color"
                      aria-label={name}
                      value={asHex(value) ?? "#000000"}
                      onChange={(e) =>
                        change(
                          { ...theme, css: setVar(theme.css, name, e.target.value) },
                          { message: `${t("themeWorkshop.control")}: ${name} = ${e.target.value}`, reply: "" },
                          `${t("themeWorkshop.control")}: ${name} `,
                        )
                      }
                    />{" "}
                    <code>{name}</code> {value && !asHex(value) ? <span className="muted">{value}</span> : null}
                  </label>
                );
              })}
            </div>
          </details>
          <details open>
            <summary>{t("themeWorkshop.motion")}</summary>
            {(["style", "close", "overlay"] as const).map((field) => (
              <label key={field} className="small">
                {t(`themeWorkshop.motionField.${field}`)}{" "}
                <select
                  value={theme.motion[field] ?? (field === "style" ? "fade" : "")}
                  onChange={(e) => setMotion({ [field]: e.target.value || undefined }, `${field}=${e.target.value}`)}
                >
                  {field !== "style" && <option value="">{t("themeWorkshop.likeOpen")}</option>}
                  {MOTIONS.map((m) => (
                    <option key={m} value={m}>
                      {m}
                    </option>
                  ))}
                </select>
              </label>
            ))}
            <label className="small">
              {t("themeWorkshop.ms", { ms: theme.motion.ms })}
              <input
                type="range"
                min={0}
                max={1200}
                step={10}
                value={theme.motion.ms}
                onChange={(e) => setMotion({ ms: Number(e.target.value) }, `ms=${e.target.value}`)}
              />
            </label>
            <div className="workshop-play">
              {(["open", "close", "focus"] as const).map((what) => (
                <button key={what} type="button" onClick={() => playOn(windowEl(), what, theme.motion.ms)}>
                  {t(`themeWorkshop.play.${what}`)}
                </button>
              ))}
            </div>
          </details>
          <details>
            <summary>{t("themeWorkshop.shape")}</summary>
            <label className="small">
              {t("themeWorkshop.base")}{" "}
              <select value={theme.base ?? "terminal"} onChange={(e) => change({ ...theme, base: e.target.value as ThemeManifest["base"] })}>
                {BASES.map((b) => (
                  <option key={b}>{b}</option>
                ))}
              </select>
            </label>
            <label className="small">
              {t("themeWorkshop.layout")}{" "}
              <select
                value={theme.layout.kind}
                onChange={(e) => change({ ...theme, layout: { kind: e.target.value as ThemeManifest["layout"]["kind"] } })}
              >
                {LAYOUTS.map((b) => (
                  <option key={b}>{b}</option>
                ))}
              </select>
            </label>
            <label className="small">
              {t("themeWorkshop.bar")}{" "}
              <select
                value={theme.bar.position}
                onChange={(e) =>
                  change({ ...theme, bar: { ...theme.bar, position: e.target.value as ThemeManifest["bar"]["position"] } })
                }
              >
                {BARS.map((b) => (
                  <option key={b}>{b}</option>
                ))}
              </select>
            </label>
          </details>
          {(problems.length > 0 || lint.length > 0) && (
            <details open>
              <summary>{t("themeWorkshop.findings")}</summary>
              <ul className="small">
                {problems.map((p) => (
                  <li key={p} className="error">
                    {p}
                  </li>
                ))}
                {lint.map((p) => (
                  <li key={p}>{p}</li>
                ))}
              </ul>
              {lint.some((p) => p.includes("light background") || p.includes("dark text")) && (
                <button type="button" disabled={busy} onClick={() => void fixContrast()}>
                  {t("themeWorkshop.fixContrast")}
                </button>
              )}
            </details>
          )}
          <details>
            <summary>{t("themeWorkshop.css")}</summary>
            <textarea
              aria-label={t("themeWorkshop.css")}
              className="workshop-css"
              rows={12}
              defaultValue={theme.css ?? ""}
              key={theme.css ?? ""}
              onBlur={(e) => {
                if (e.target.value !== (theme.css ?? ""))
                  change({ ...theme, css: e.target.value }, { message: t("themeWorkshop.cssByHand"), reply: "" });
              }}
            />
          </details>
        </aside>
      </div>
    </div>
  );
}
