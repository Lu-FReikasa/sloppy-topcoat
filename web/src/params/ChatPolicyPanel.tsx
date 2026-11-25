import { useCallback, useEffect, useState } from "react";
import { useTranslation } from "react-i18next";
import type { ApiClient } from "../api/client";
import { describeError, toActionError } from "../errors";
import type { ChatPolicyView } from "../types";

const LEVELS = ["fast", "balanced", "thorough"] as const;
const NUMBERS = ["max_turns", "max_tool_calls", "time_budget"] as const;
const SWITCHES = ["recall", "write_memory", "review", "reactions"] as const;
type Level = (typeof LEVELS)[number];
type Tri = "" | "true" | "false"; // "" = the hub's own rule

interface Form {
  numbers: Record<string, Record<Level, string>>;
  secondLook: Record<Level, Tri>;
  switches: Record<string, Tri>;
  node: string;
}

const tri = (value: unknown): Tri => (value === true ? "true" : value === false ? "false" : "");

/** The form from what the hub has stored (a value that is a table fills its levels). */
export function formOf(view: ChatPolicyView): Form {
  const level = (l: Level, key: string): unknown => {
    const v = view.policy[key];
    if (v && typeof v === "object" && "effort" in v) {
      const table = (v as { effort: Record<string, unknown> }).effort;
      return table[l] ?? table.balanced;
    }
    return v;
  };
  const numbers: Form["numbers"] = {};
  for (const key of NUMBERS) {
    numbers[key] = Object.fromEntries(
      LEVELS.map((l) => [l, level(l, key) === undefined ? "" : String(level(l, key))]),
    ) as Record<Level, string>;
  }
  const secondLook = Object.fromEntries(LEVELS.map((l) => [l, tri(level(l, "second_look"))])) as Record<Level, Tri>;
  const switches: Record<string, Tri> = {};
  for (const key of SWITCHES) switches[key] = tri(level("balanced", key));
  const node = view.policy.node;
  return { numbers, secondLook, switches, node: typeof node === "string" ? node : "" };
}

/** The document the form stands for: one value when every level says the same, else a table. */
export function docOf(form: Form): Record<string, unknown> {
  const doc: Record<string, unknown> = {};
  const fold = <T,>(entries: [Level, T | undefined][]) => {
    const given = entries.filter(([, v]) => v !== undefined) as [Level, T][];
    if (given.length === 0) return undefined;
    const first = given[0]![1];
    if (given.length === LEVELS.length && given.every(([, v]) => v === first)) return first;
    return { effort: Object.fromEntries(given) };
  };
  for (const key of NUMBERS) {
    const value = fold(
      LEVELS.map((l): [Level, number | undefined] => {
        const text = (form.numbers[key]?.[l] ?? "").trim();
        return [l, text === "" ? undefined : Number(text)];
      }),
    );
    if (value !== undefined) doc[key] = value;
  }
  const second = fold(
    LEVELS.map((l): [Level, boolean | undefined] => {
      const t = form.secondLook[l];
      return [l, t === "" ? undefined : t === "true"];
    }),
  );
  if (second !== undefined) doc.second_look = second;
  for (const key of SWITCHES) {
    const t = form.switches[key];
    if (t) doc[key] = t === "true";
  }
  if (form.node.trim()) doc.node = form.node.trim();
  return doc;
}

/** How the chat answers, by effort (the chat policy): limits of a check, the second look, recall,
 *  notes, Nachlese, emoji answers, placement. An empty field is the hub's own rule. */
export function ChatPolicyPanel({ api }: { api: ApiClient }) {
  const { t, i18n } = useTranslation();
  const [form, setForm] = useState<Form | null>(null);
  const [state, setState] = useState<"idle" | "saved" | string>("idle");

  const load = useCallback(() => {
    api.getChatPolicy().then(
      (v) => setForm(formOf(v)),
      (err: unknown) => setState(describeError(toActionError(err), t, i18n)),
    );
  }, [api, t, i18n]);
  useEffect(load, [load]);

  async function save(doc: Record<string, unknown>) {
    try {
      setForm(formOf(await api.putChatPolicy(doc)));
      setState("saved");
    } catch (err) {
      setState(describeError(toActionError(err), t, i18n));
    }
  }

  if (!form) return state !== "idle" ? <p className="bad" role="alert">{state}</p> : null;
  const label = (l: Level) => t(`chatPolicy.level.${l}`);
  const option = (v: Tri) => t(v === "" ? "chatPolicy.hubRule" : v === "true" ? "chatPolicy.on" : "chatPolicy.off");
  const triSelect = (value: Tri, onChange: (v: Tri) => void, name: string) => (
    <select value={value} aria-label={name} onChange={(e) => onChange(e.target.value as Tri)}>
      {(["", "true", "false"] as Tri[]).map((v) => (
        <option key={v} value={v}>
          {option(v)}
        </option>
      ))}
    </select>
  );

  return (
    <section className="card" data-testid="chat-policy">
      <h2>{t("chatPolicy.title")}</h2>
      <p className="muted small">{t("chatPolicy.hint")}</p>
      <table className="plain">
        <thead>
          <tr>
            <th />
            {LEVELS.map((l) => (
              <th key={l}>{label(l)}</th>
            ))}
          </tr>
        </thead>
        <tbody>
          {NUMBERS.map((key) => (
            <tr key={key}>
              <th scope="row">{t(`chatPolicy.field.${key}`)}</th>
              {LEVELS.map((l) => (
                <td key={l}>
                  <input
                    inputMode="numeric"
                    size={5}
                    value={form.numbers[key]?.[l] ?? ""}
                    placeholder={t("chatPolicy.hubRule")}
                    aria-label={`${t(`chatPolicy.field.${key}`)} (${label(l)})`}
                    onChange={(e) =>
                      setForm({
                        ...form,
                        numbers: { ...form.numbers, [key]: { ...form.numbers[key]!, [l]: e.target.value } },
                      })
                    }
                  />
                </td>
              ))}
            </tr>
          ))}
          <tr>
            <th scope="row">{t("chatPolicy.field.second_look")}</th>
            {LEVELS.map((l) => (
              <td key={l}>
                {triSelect(
                  form.secondLook[l],
                  (v) => setForm({ ...form, secondLook: { ...form.secondLook, [l]: v } }),
                  `${t("chatPolicy.field.second_look")} (${label(l)})`,
                )}
              </td>
            ))}
          </tr>
        </tbody>
      </table>
      {SWITCHES.map((key) => (
        <label key={key} className="form-grid">
          <span>{t(`chatPolicy.field.${key}`)}</span>
          {triSelect(
            form.switches[key] ?? "",
            (v) => setForm({ ...form, switches: { ...form.switches, [key]: v } }),
            t(`chatPolicy.field.${key}`),
          )}
        </label>
      ))}
      <label className="form-grid">
        <span>{t("chatPolicy.field.node")}</span>
        <input
          value={form.node}
          placeholder={t("chatPolicy.hubRule")}
          onChange={(e) => setForm({ ...form, node: e.target.value })}
          aria-label={t("chatPolicy.field.node")}
        />
      </label>
      <div className="actions">
        <button type="button" className="primary" onClick={() => void save(docOf(form))}>
          {t("chatPolicy.save")}
        </button>
        <button type="button" onClick={() => void save({})}>
          {t("chatPolicy.reset")}
        </button>
        {state === "saved" && (
          <span className="muted small" role="status">
            {t("chatPolicy.saved")}
          </span>
        )}
      </div>
      {state !== "idle" && state !== "saved" && (
        <p className="bad" role="alert">
          {state}
        </p>
      )}
    </section>
  );
}
