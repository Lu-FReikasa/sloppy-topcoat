import { useCallback, useEffect, useState } from "react";
import { useTranslation } from "react-i18next";
import { UnauthorizedError, type ApiClient } from "../api/client";
import { describeError, toActionError } from "../errors";
import { ChatPolicyPanel } from "./ChatPolicyPanel";
import { formatDateTime } from "../format";
import type { ParamChange, ParamView, ParamsView } from "../types";

/** The parameters an admin changes while the hub runs (E7-03): what each does, its value and its
 *  default, a change that applies at once, a way back, the history and a preset to keep. */

/** A value as the person edits it: numbers and words as text, a structure as JSON. */
export function asText(value: unknown): string {
  if (value !== null && typeof value === "object") return JSON.stringify(value, null, 1);
  return String(value ?? "");
}

type Parsed = { ok: true; value: unknown } | { ok: false; why: "notNumber" | "notJson" };

export function parseInput(p: ParamView, text: string): Parsed {
  if (p.kind === "int" || p.kind === "float") {
    const n = Number(text);
    return text.trim() === "" || Number.isNaN(n) ? { ok: false, why: "notNumber" } : { ok: true, value: n };
  }
  if (p.kind === "json") {
    try {
      return { ok: true, value: JSON.parse(text) as unknown };
    } catch {
      return { ok: false, why: "notJson" };
    }
  }
  return { ok: true, value: text };
}

function rangeText(p: ParamView): string | null {
  return p.low !== null && p.high !== null ? `${p.low} – ${p.high}` : null;
}

function History({ api, name }: { api: ApiClient; name: string }) {
  const { t, i18n } = useTranslation();
  const [rows, setRows] = useState<ParamChange[] | null>(null);
  const load = () => {
    if (rows === null) api.paramHistory(name).then(setRows, () => setRows([]));
  };
  return (
    <details onToggle={(e) => (e.currentTarget as HTMLDetailsElement).open && load()}>
      <summary className="small muted">{t("params.history")}</summary>
      {rows !== null && rows.length === 0 && <p className="small muted">{t("params.historyNone")}</p>}
      <ul className="small">
        {(rows ?? []).map((r) => (
          <li key={r.id}>
            <span className="muted">{formatDateTime(r.at, i18n.language)} </span>
            {r.reset
              ? t("params.historyReset")
              : t("params.historyChange", { old: asText(r.old), new: asText(r.new) })}
          </li>
        ))}
      </ul>
    </details>
  );
}

function Row({ api, p, onChanged }: { api: ApiClient; p: ParamView; onChanged: () => void }) {
  const { t, i18n } = useTranslation();
  // what the person typed, or null while the field shows the value the hub has
  const [edit, setEdit] = useState<string | null>(null);
  const draft = edit ?? asText(p.value);
  const setDraft = setEdit;
  const [note, setNote] = useState<{ ok: boolean; text: string } | null>(null);
  const [busy, setBusy] = useState(false);

  async function save(value: unknown) {
    setBusy(true);
    try {
      await api.setParam(p.key, value);
      setEdit(null);
      setNote({ ok: true, text: t("params.saved") });
      onChanged();
    } catch (err) {
      if (err instanceof UnauthorizedError) return;
      setNote({ ok: false, text: describeError(toActionError(err), t, i18n) });
    } finally {
      setBusy(false);
    }
  }

  function submit() {
    const parsed = parseInput(p, draft);
    if (!parsed.ok) return setNote({ ok: false, text: t(`params.${parsed.why}`) });
    void save(parsed.value);
  }

  async function reset() {
    setBusy(true);
    try {
      await api.resetParam(p.key);
      setEdit(null);
      setNote(null);
      onChanged();
    } catch (err) {
      setNote({ ok: false, text: describeError(toActionError(err), t, i18n) });
    } finally {
      setBusy(false);
    }
  }

  const range = rangeText(p);
  const changed = edit !== null && edit !== asText(p.value);
  return (
    <li className="param-row" data-testid={`param-${p.key}`}>
      <div className="param-head">
        <code>{p.key}</code>
        {p.overridden && <span className="badge">{t("params.overridden")}</span>}
      </div>
      <p className="small muted">{t(`params.text.${p.key}`, { defaultValue: p.text })}</p>
      <div className="param-edit">
        {p.kind === "bool" ? (
          <label>
            <input
              type="checkbox"
              checked={p.value === true}
              disabled={busy}
              onChange={(e) => void save(e.target.checked)}
            />{" "}
            <span className="small">{p.value === true ? t("params.on") : t("params.off")}</span>
          </label>
        ) : p.kind === "enum" ? (
          <select
            value={draft}
            disabled={busy}
            aria-label={p.key}
            onChange={(e) => {
              setDraft(e.target.value);
              void save(e.target.value);
            }}
          >
            {p.choices.map((c) => (
              <option key={c} value={c}>
                {c}
              </option>
            ))}
          </select>
        ) : p.kind === "json" ? (
          <textarea
            rows={4}
            value={draft}
            disabled={busy}
            aria-label={p.key}
            spellCheck={false}
            onChange={(e) => setDraft(e.target.value)}
          />
        ) : (
          <input
            type="text"
            inputMode="decimal"
            value={draft}
            disabled={busy}
            aria-label={p.key}
            onChange={(e) => setDraft(e.target.value)}
            onKeyDown={(e) => e.key === "Enter" && submit()}
          />
        )}
        {p.unit && <span className="small muted">{p.unit}</span>}
        {p.kind !== "bool" && p.kind !== "enum" && (
          <button type="button" disabled={busy || !changed} onClick={submit}>
            {t("params.save")}
          </button>
        )}
        {p.overridden && (
          <button type="button" disabled={busy} onClick={() => void reset()}>
            {t("params.reset")}
          </button>
        )}
      </div>
      <p className="small muted">
        {t("params.default", { value: asText(p.default) })}
        {range ? ` · ${t("params.range", { low: p.low, high: p.high })}` : ""}
      </p>
      {note && (
        <p className={note.ok ? "small" : "error small"} role={note.ok ? "status" : "alert"}>
          {note.text}
        </p>
      )}
      <History api={api} name={p.key} />
    </li>
  );
}

function Preset({ api, onApplied }: { api: ApiClient; onApplied: () => void }) {
  const { t, i18n } = useTranslation();
  const [text, setText] = useState("");
  const [note, setNote] = useState<{ ok: boolean; text: string } | null>(null);

  async function show() {
    try {
      setText(JSON.stringify(await api.exportParams(), null, 2));
      setNote(null);
    } catch (err) {
      setNote({ ok: false, text: describeError(toActionError(err), t, i18n) });
    }
  }

  async function apply() {
    let doc: unknown;
    try {
      doc = JSON.parse(text);
    } catch {
      return setNote({ ok: false, text: t("params.preset.invalid") });
    }
    const values = (doc as { values?: unknown } | null)?.values;
    if (!values || typeof values !== "object" || Array.isArray(values)) {
      return setNote({ ok: false, text: t("params.preset.invalid") });
    }
    try {
      const done = await api.importParams({ version: 1, values: values as Record<string, unknown> });
      setNote({ ok: true, text: t("params.preset.applied", { count: done.applied.length }) });
      onApplied();
    } catch (err) {
      setNote({ ok: false, text: describeError(toActionError(err), t, i18n) });
    }
  }

  return (
    <section className="param-preset" aria-label={t("params.preset.title")}>
      <h3>{t("params.preset.title")}</h3>
      <p className="small muted">{t("params.preset.hint")}</p>
      <textarea
        rows={5}
        value={text}
        spellCheck={false}
        aria-label={t("params.preset.label")}
        onChange={(e) => setText(e.target.value)}
      />
      <p>
        <button type="button" onClick={() => void show()}>
          {t("params.preset.export")}
        </button>{" "}
        <button type="button" disabled={text.trim() === ""} onClick={() => void apply()}>
          {t("params.preset.apply")}
        </button>
      </p>
      {note && (
        <p className={note.ok ? "small" : "error small"} role={note.ok ? "status" : "alert"}>
          {note.text}
        </p>
      )}
    </section>
  );
}

export function ParamsPage({ api }: { api: ApiClient }) {
  const { t } = useTranslation();
  const [view, setView] = useState<ParamsView | null>(null);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(() => {
    api.params().then(
      (v) => {
        setView(v);
        setError(null);
      },
      (err: unknown) => {
        if (!(err instanceof UnauthorizedError)) setError(toActionError(err).message);
      },
    );
  }, [api]);
  useEffect(load, [load]);

  if (error) return <p className="error" role="alert">{error}</p>;
  if (!view) return <p className="muted">{t("params.loading")}</p>;
  return (
    <div className="params-page">
      <ChatPolicyPanel api={api} />
      <h2>{t("params.title")}</h2>
      <p className="muted">{t("params.intro")}</p>
      {view.groups.map((g) => (
        <section key={g} aria-label={t(`params.group.${g}`)}>
          <h3>{t(`params.group.${g}`, { defaultValue: g })}</h3>
          <ul className="param-list">
            {view.params
              .filter((p) => p.group === g)
              .map((p) => (
                <Row key={p.key} api={api} p={p} onChanged={load} />
              ))}
          </ul>
        </section>
      ))}
      <Preset api={api} onApplied={load} />
    </div>
  );
}
