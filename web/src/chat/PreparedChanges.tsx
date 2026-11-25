import { useCallback, useEffect, useState } from "react";
import { useTranslation } from "react-i18next";
import type { ApiClient } from "../api/client";
import { ChangeSetCard } from "../project/ChangeSets";
import type { ProjectChange, ProjectChangeSet } from "../project/types";
import { BatchCard } from "./BatchCard";

/** What the Projekte chat prepared in one check (ARCHITECTURE D15). The chat reads its own
 *  application freely but changes nothing: every change waits here for a click. The state is read
 *  from the hub, so a reload shows what was decided.
 *
 *  New entries are shown the way a person reads a plan (live 2026-11-17: three cards of "Art task,
 *  Titel …, Darunter 44c2095b-…" said nothing about what would change): grouped under the entry
 *  they go under, each with its title and its text, and a warning when it has none. */

const CAPABILITY_KEY: Record<string, string> = {
  "project.update": "projectUpdate",
  "project.node.create": "nodeCreate",
  "project.node.update": "nodeUpdate",
  "project.node.move": "nodeMove",
  "project.report.add": "reportAdd",
  "project.request.send": "requestAdd",
  "project.order.draft": "orderDraft",
  // the batch card of the planner (project_tools.py, BATCH_TOOL)
  "project.batch.propose": "projectBatch",
  // the single-step tools of 04g (project_write_tools.py)
  "project.create": "projectCreate",
  "project.duplicate": "projectDuplicate",
  "project.move": "projectMove",
  "project.node.complete": "nodeComplete",
  "project.node.claim": "nodeClaim",
  "project.node.reschedule": "nodeReschedule",
  "project.relation.add": "relationAdd",
  "project.relation.end": "relationEnd",
  "project.label.add": "labelAdd",
  "project.label.detach": "labelDetach",
  "project.reminder.add": "reminderAdd",
  "project.reminder.cancel": "reminderCancel",
  "project.view.save": "viewSave",
};

const CREATE = "project.node.create";
// what a person does not need to read: ids, and the kind when it is the usual one
const HIDDEN = new Set(["project_id", "node_id", "parent_id", "revision", "title", "body"]);
const BODY_PREVIEW = 600;

function shown(value: unknown): string {
  if (Array.isArray(value)) return value.join(", ");
  if (value !== null && typeof value === "object") return JSON.stringify(value);
  return String(value);
}

function str(value: unknown): string {
  return typeof value === "string" ? value : "";
}

/** The other fields of a change, without ids and without what the card already says. */
function extraFields(c: ProjectChange): [string, unknown][] {
  return Object.entries(c.preview.fields).filter(
    ([name, value]) =>
      !HIDDEN.has(name) && !(name === "kind" && value === "task") && value !== "" && value != null,
  );
}

/** The text a new entry gets, short with a "more" to open it. */
function Body({ text }: { text: string }) {
  const { t } = useTranslation();
  const [open, setOpen] = useState(false);
  const long = text.length > BODY_PREVIEW;
  return (
    <div className="project-settext" data-testid="change-body">
      {open || !long ? text : `${text.slice(0, BODY_PREVIEW)} …`}
      {long && !open && (
        <button type="button" className="project-linkbutton" onClick={() => setOpen(true)}>
          {t("changes.more")}
        </button>
      )}
    </div>
  );
}

type Group = { key: string; parent?: { ref: string; title: string }; items: ProjectChange[] };

/** New entries by the entry they go under, in the order they were prepared. */
function groupsOf(creates: ProjectChange[]): Group[] {
  const groups: Group[] = [];
  for (const c of creates) {
    const key = str(c.preview.fields.parent_id) || `top:${c.preview.target ?? ""}`;
    let g = groups.find((x) => x.key === key);
    if (!g) {
      g = { key, parent: c.preview.parent, items: [] };
      groups.push(g);
    }
    g.items.push(c);
  }
  return groups;
}

export function PreparedChanges({
  api,
  taskId,
}: {
  api: ApiClient;
  taskId: string;
}) {
  const { t } = useTranslation();
  const [changes, setChanges] = useState<ProjectChange[] | null>(null);
  // the change sets of the same check: in the chat too, with what each operation writes (live
  // 2026-11-14: they showed only on the project's page, so the chat said "waits for your click"
  // and showed nothing to click)
  const [sets, setSets] = useState<ProjectChangeSet[]>([]);
  const [busy, setBusy] = useState<string | null>(null);
  const [error, setError] = useState("");

  const load = useCallback(() => {
    api
      .projectChanges(taskId)
      .then(setChanges)
      .catch(() => setError(t("changes.loadFailed")));
    api
      .projectChangeSets({ taskId })
      .then((r) => setSets(r.change_sets.filter((x) => x.status !== "refused")))
      .catch(() => undefined);
  }, [api, taskId, t]);

  useEffect(load, [load]);

  async function decideOne(change: ProjectChange, apply: boolean) {
    const done = apply
      ? await api.applyProjectChange(change.id)
      : await api.refuseProjectChange(change.id);
    setChanges((list) => (list ?? []).map((c) => (c.id === done.id ? done : c)));
  }

  /** One click for one change, or for every waiting change of a group, one after the other: each
   *  stays its own confirmed action (D15), the button only spares the clicks. */
  async function decide(list: ProjectChange[], apply: boolean, key: string) {
    setBusy(key);
    setError("");
    try {
      for (const c of list) {
        if (c.status === "pending") await decideOne(c, apply);
      }
    } catch {
      setError(t("changes.decideFailed"));
      load();
    } finally {
      setBusy(null);
    }
  }

  if ((!changes || changes.length === 0) && sets.length === 0) return null;
  const all = changes ?? [];
  const creates = all.filter((c) => c.capability === CREATE);
  const others = all.filter((c) => c.capability !== CREATE);

  const status = (c: ProjectChange) => (
    <span className="badge" data-testid="change-status">
      {t(`changes.status.${c.status}`)}
    </span>
  );
  const buttons = (list: ProjectChange[], key: string, many: boolean) => (
    <div className="row">
      <button type="button" disabled={busy !== null} onClick={() => void decide(list, true, key)}>
        {many ? t("changes.applyAll", { count: list.length }) : t("changes.apply")}
      </button>
      <button type="button" disabled={busy !== null} onClick={() => void decide(list, false, key)}>
        {many ? t("changes.refuseAll") : t("changes.refuse")}
      </button>
    </div>
  );
  const failure = (c: ProjectChange) =>
    c.status === "failed" && c.result?.message != null ? (
      <p className="notice-line">{String(c.result.message)}</p>
    ) : null;
  const extras = (c: ProjectChange) => {
    const rest = extraFields(c);
    return rest.length === 0 ? null : (
      <dl>
        {rest.map(([name, value]) => (
          <div key={name}>
            <dt>{t(`changes.field.${name}`, { defaultValue: name })}</dt>
            <dd>{shown(value)}</dd>
          </div>
        ))}
      </dl>
    );
  };

  return (
    <section
      className="prepared-changes"
      data-testid="prepared-changes"
      aria-label={t("changes.title")}
    >
      <h4>{t("changes.title")}</h4>
      {sets.map((set) => (
        <ChangeSetCard
          key={`${set.id}:${set.status}`}
          api={api}
          set={set}
          onError={() => setError(t("changes.decideFailed"))}
          onDecided={(done) =>
            setSets((list) => list.map((x) => (x.id === done.id ? done : x)))
          }
        />
      ))}
      {all.some((c) => c.status === "pending") && <p className="muted">{t("changes.hint")}</p>}
      {groupsOf(creates).map((g) => {
        const waiting = g.items.filter((c) => c.status === "pending");
        return (
          <div key={g.key} className="change-group" data-testid="change-group">
            <strong>
              {g.parent
                ? t("changes.newUnder", { ref: g.parent.ref, title: g.parent.title })
                : t("changes.newIn", { project: g.items[0]?.preview.target ?? "" })}
            </strong>
            <ul>
              {g.items.map((c) => {
                const body = str(c.preview.fields.body);
                return (
                  <li key={c.id} data-status={c.status}>
                    <div>
                      <strong data-testid="change-title">{str(c.preview.fields.title)}</strong>{" "}
                      {status(c)}
                    </div>
                    {body ? (
                      <Body text={body} />
                    ) : (
                      <p className="project-warn" data-testid="change-nobody">
                        {t("changes.noBody")}
                      </p>
                    )}
                    {extras(c)}
                    {failure(c)}
                    {c.status === "pending" && buttons([c], c.id, false)}
                  </li>
                );
              })}
            </ul>
            {waiting.length > 1 && buttons(waiting, g.key, true)}
          </div>
        );
      })}
      <ul>
        {others.map((c) => {
          const key = CAPABILITY_KEY[c.capability];
          if (c.capability === "project.batch.propose") {
            return (
              <li key={c.id} data-status={c.status}>
                <BatchCard
                  change={c}
                  api={api}
                  busy={busy === c.id}
                  onDecide={(apply) => void decide([c], apply, c.id)}
                />
              </li>
            );
          }
          return (
            <li key={c.id} data-status={c.status}>
              <div>
                <strong>{key ? t(`changes.cap.${key}`) : c.capability}</strong>
                {c.preview.target && (
                  <span> · {t("changes.inProject", { project: c.preview.target })}</span>
                )}{" "}
                {status(c)}
              </div>
              {str(c.preview.fields.title) && <p>{str(c.preview.fields.title)}</p>}
              {str(c.preview.fields.body) && <Body text={str(c.preview.fields.body)} />}
              {extras(c)}
              {failure(c)}
              {c.status === "pending" && buttons([c], c.id, false)}
            </li>
          );
        })}
      </ul>
      {error && <p className="notice-line">{error}</p>}
    </section>
  );
}
