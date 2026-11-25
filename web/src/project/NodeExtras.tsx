import { useCallback, useEffect, useState, type FormEvent } from "react";
import { useTranslation } from "react-i18next";
import type { ApiClient } from "../api/client";
import { fromInputValue, joinOffset, splitOffset, toInputValue } from "./time";
import {
  RELATION_KINDS,
  type ProjectFound,
  type ProjectLabel,
  type ProjectNode,
  type ProjectRelation,
  type ProjectRelationKind,
  type ProjectReminder,
} from "./types";

type Fail = (err: unknown) => void;

/** The labels of a node: chips to take off, and a box to put one on (made when it is new). */
export function LabelsEditor({
  api,
  node,
  onChanged,
  onError,
}: {
  api: ApiClient;
  node: ProjectNode;
  onChanged: () => void;
  onError: Fail;
}) {
  const { t } = useTranslation();
  const [known, setKnown] = useState<ProjectLabel[]>([]);
  const [text, setText] = useState("");
  useEffect(() => {
    let live = true;
    api.projectLabels().then(
      (r) => live && setKnown(r.labels),
      () => undefined,
    );
    return () => {
      live = false;
    };
  }, [api, node.labels]);

  const add = async (e: FormEvent) => {
    e.preventDefault();
    const title = text.trim();
    if (!title) return;
    try {
      let label = known.find(
        (l) => l.title.toLowerCase() === title.toLowerCase(),
      );
      if (!label) label = await api.createProjectLabel({ title });
      await api.setProjectNodeLabels(node.id, { add: [label.id] });
      setText("");
      onChanged();
    } catch (err) {
      onError(err);
    }
  };
  const take = async (label: ProjectLabel) => {
    try {
      await api.setProjectNodeLabels(node.id, { remove: [label.id] });
      onChanged();
    } catch (err) {
      onError(err);
    }
  };
  const listId = `labels-${node.id}`;
  return (
    <fieldset className="project-extra">
      <legend>{t("project.labels")}</legend>
      <span className="project-chips">
        {(node.labels ?? []).map((l) => (
          <span key={l.id} className="project-chip">
            {l.title}
            <button
              type="button"
              aria-label={t("project.removeLabel", { label: l.title })}
              onClick={() => take(l)}
            >
              ×
            </button>
          </span>
        ))}
      </span>
      <form className="project-bar" onSubmit={add}>
        <input
          list={listId}
          aria-label={t("project.addLabel")}
          placeholder={t("project.labelPlaceholder")}
          value={text}
          onChange={(e) => setText(e.target.value)}
          maxLength={40}
        />
        <datalist id={listId}>
          {known.map((l) => (
            <option key={l.id} value={l.title} />
          ))}
        </datalist>
        <button type="submit" disabled={!text.trim()}>
          {t("project.addLabel")}
        </button>
      </form>
    </fieldset>
  );
}

/** The relations of a node, also to other projects, and a small search to add one. */
export function RelationsEditor({
  api,
  node,
  onChanged,
  onError,
}: {
  api: ApiClient;
  node: ProjectNode;
  onChanged: () => void;
  onError: Fail;
}) {
  const { t } = useTranslation();
  const [relations, setRelations] = useState<ProjectRelation[] | null>(null);
  const [kind, setKind] = useState<ProjectRelationKind>("blocked_by");
  const [query, setQuery] = useState("");
  const [found, setFound] = useState<ProjectFound[]>([]);
  const [other, setOther] = useState("");

  const load = useCallback(async () => {
    try {
      setRelations((await api.projectRelations(node.id)).relations);
    } catch (err) {
      onError(err);
    }
  }, [api, node.id, onError]);
  useEffect(() => {
    void Promise.resolve().then(load);
  }, [load]);

  const search = async (e: FormEvent) => {
    e.preventDefault();
    const word = query.replace(/["\\*]/g, " ").trim();
    if (word.length < 2) return;
    try {
      const r = await api.searchProjectNodes({
        filter: `q like "*${word}*" && kind != report`,
        limit: 20,
      });
      const rows = r.nodes.filter((n) => n.id !== node.id);
      setFound(rows);
      setOther(rows[0]?.id ?? "");
    } catch (err) {
      onError(err);
    }
  };
  const add = async () => {
    if (!other) return;
    try {
      setRelations(
        (await api.addProjectRelation(node.id, { other_id: other, kind }))
          .relations,
      );
      setFound([]);
      setQuery("");
      setOther("");
      onChanged();
    } catch (err) {
      onError(err);
    }
  };
  const end = async (relation: ProjectRelation) => {
    try {
      await api.endProjectRelation(relation.id);
      await load();
      onChanged();
    } catch (err) {
      onError(err);
    }
  };
  return (
    <fieldset className="project-extra">
      <legend>{t("project.relations")}</legend>
      {relations !== null && relations.length === 0 && (
        <p className="project-muted">{t("project.noRelations")}</p>
      )}
      <ul className="project-list">
        {(relations ?? []).map((r) => (
          <li key={r.id} className="project-row">
            <span>{t(`project.relationKinds.${r.kind}.${r.direction}`)}</span>
            <span className="project-name">
              {r.other.ref} {r.other.title}
            </span>
            <button
              type="button"
              onClick={() => end(r)}
              aria-label={`${t("project.endRelation")}: ${r.other.ref}`}
            >
              {t("project.endRelation")}
            </button>
          </li>
        ))}
      </ul>
      <form className="project-bar" onSubmit={search}>
        <select
          aria-label={t("project.relationKind")}
          value={kind}
          onChange={(e) => setKind(e.target.value as ProjectRelationKind)}
        >
          {RELATION_KINDS.map((k) => (
            <option key={k} value={k}>
              {t(`project.relationKinds.${k}.out`)}
            </option>
          ))}
        </select>
        <input
          aria-label={t("project.relationSearch")}
          placeholder={t("project.relationSearch")}
          value={query}
          onChange={(e) => setQuery(e.target.value)}
        />
        <button type="submit" disabled={query.trim().length < 2}>
          {t("project.filterApply")}
        </button>
      </form>
      {found.length > 0 && (
        <div className="project-bar">
          <select
            aria-label={t("project.addRelation")}
            value={other}
            onChange={(e) => setOther(e.target.value)}
          >
            {found.map((n) => (
              <option key={n.id} value={n.id}>
                {n.ref} {n.title}
              </option>
            ))}
          </select>
          <button type="button" onClick={add}>
            {t("project.addRelation")}
          </button>
        </div>
      )}
    </fieldset>
  );
}

/** The reminders of a task: at a fixed time, or before or after one of its dates. */
export function RemindersEditor({
  api,
  node,
  zone,
  onError,
}: {
  api: ApiClient;
  node: ProjectNode;
  zone: string;
  onError: Fail;
}) {
  const { t, i18n } = useTranslation();
  const [reminders, setReminders] = useState<ProjectReminder[] | null>(null);
  const [mode, setMode] = useState<"at" | "relative">("relative");
  const [at, setAt] = useState("");
  const [date, setDate] = useState<"due" | "start" | "end">("due");
  const [amount, setAmount] = useState(-60);
  const [unit, setUnit] = useState<"minutes" | "hours" | "days">("minutes");

  const load = useCallback(async () => {
    try {
      setReminders((await api.projectReminders(node.id)).reminders);
    } catch (err) {
      onError(err);
    }
  }, [api, node.id, onError]);
  useEffect(() => {
    void Promise.resolve().then(load);
  }, [load]);

  const hasDate =
    date === "due"
      ? node.due_at
      : date === "start"
        ? node.start_at
        : node.end_at;
  const submit = async (e: FormEvent) => {
    e.preventDefault();
    try {
      if (mode === "at") {
        const iso = fromInputValue(at, zone);
        if (!iso) return;
        await api.addProjectReminder(node.id, { at: iso });
      } else {
        if (!hasDate) return;
        await api.addProjectReminder(node.id, {
          relative_to: date,
          offset_seconds: joinOffset(amount, unit),
        });
      }
      setAt("");
      await load();
    } catch (err) {
      onError(err);
    }
  };
  const remove = async (r: ProjectReminder) => {
    try {
      await api.removeProjectReminder(r.id);
      await load();
    } catch (err) {
      onError(err);
    }
  };
  const describe = (r: ProjectReminder): string => {
    if (r.at) return toInputValue(r.at, zone).replace("T", " ");
    const { amount: n, unit: u } = splitOffset(r.offset_seconds ?? 0);
    return `${n} ${t(`project.remindUnits.${u}`)} · ${t(`project.remindDates.${r.relative_to ?? "due"}`)}`;
  };
  if (node.kind !== "task") return null;
  return (
    <fieldset className="project-extra">
      <legend>{t("project.reminders")}</legend>
      {reminders !== null && reminders.length === 0 && (
        <p className="project-muted">{t("project.noReminders")}</p>
      )}
      <ul className="project-list">
        {(reminders ?? []).map((r) => (
          <li key={r.id} className="project-row">
            <span className="project-name">{describe(r)}</span>
            {r.fired_at && (
              <span className="project-muted">
                {t("project.reminderSent", {
                  when: new Intl.DateTimeFormat(i18n.language, {
                    timeZone: zone,
                    dateStyle: "short",
                    timeStyle: "short",
                  }).format(new Date(r.fired_at)),
                })}
              </span>
            )}
            <button
              type="button"
              onClick={() => remove(r)}
              aria-label={`${t("project.removeReminder")}: ${describe(r)}`}
            >
              {t("project.removeReminder")}
            </button>
          </li>
        ))}
      </ul>
      <form className="project-bar" onSubmit={submit}>
        <select
          aria-label={t("project.addReminder")}
          value={mode}
          onChange={(e) => setMode(e.target.value as typeof mode)}
        >
          <option value="relative">{t("project.remindRelative")}</option>
          <option value="at">{t("project.remindAt")}</option>
        </select>
        {mode === "at" ? (
          <input
            type="datetime-local"
            aria-label={t("project.remindAt")}
            value={at}
            onChange={(e) => setAt(e.target.value)}
          />
        ) : (
          <>
            <select
              aria-label={t("project.remindDate")}
              value={date}
              onChange={(e) => setDate(e.target.value as typeof date)}
            >
              {(["due", "start", "end"] as const).map((d) => (
                <option key={d} value={d}>
                  {t(`project.remindDates.${d}`)}
                </option>
              ))}
            </select>
            <input
              type="number"
              aria-label={t("project.remindAmount")}
              value={amount}
              onChange={(e) => setAmount(Number(e.target.value))}
            />
            <select
              aria-label={t("project.remindAmount")}
              value={unit}
              onChange={(e) => setUnit(e.target.value as typeof unit)}
            >
              {(["minutes", "hours", "days"] as const).map((u) => (
                <option key={u} value={u}>
                  {t(`project.remindUnits.${u}`)}
                </option>
              ))}
            </select>
          </>
        )}
        <button type="submit" disabled={mode === "relative" ? !hasDate : !at}>
          {t("project.addReminder")}
        </button>
        {mode === "relative" && !hasDate && (
          <span className="project-muted">
            {t("project.reminderNeedsDate", {
              date: t(`project.remindDates.${date}`),
            })}
          </span>
        )}
      </form>
    </fieldset>
  );
}
