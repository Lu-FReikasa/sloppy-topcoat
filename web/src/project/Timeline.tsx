import { useCallback, useEffect, useState } from "react";
import { useTranslation } from "react-i18next";
import type { ApiClient } from "../api/client";
import type { ProjectEvent, ProjectNode } from "./types";

/** The kinds of the Akte in groups a person can pick from. */
export const KIND_GROUPS: Record<string, string[]> = {
  project: [
    "project.created", "project.updated", "project.from_template", "project.archived",
    "project.scope_changed", "project.trashed", "project.restored", "project.purged",
  ],
  node: [
    "node.created", "node.updated", "node.moved", "node.status_changed", "node.completed",
    "node.reopened", "node.claimed", "node.trashed", "node.restored", "node.purged",
  ],
  text: ["body.changed"],
  report: [
    "question.answered", "request.created", "request.decided", "request.queued", "report.added",
    "report.superseded",
  ],
  links: [
    "relation.added", "relation.ended", "label.added", "label.removed", "reminder.added",
    "reminder.removed", "reminder.fired",
  ],
  messages: [
    "message.sent", "message.received", "message.reviewed", "message.state_changed",
    "chain.started", "chain.flagged", "chain.stopped", "mandate.created", "mandate.used",
    "mandate.revoked",
  ],
  sets: ["changeset.proposed", "changeset.applied", "changeset.refused"],
}; // prettier-ignore

const PAGE = 50;

/** What a line says besides its kind: the short facts of its payload (never a long text). */
export function facts(
  payload: Record<string, unknown> | null,
): [string, string][] {
  if (!payload) return [];
  const out: [string, string][] = [];
  for (const [key, value] of Object.entries(payload)) {
    if (out.length >= 4) break;
    if (value === null || value === undefined || typeof value === "object")
      continue;
    const text = String(value);
    if (text.length > 80) continue;
    out.push([key, text]);
  }
  return out;
}

/** The Akte of a project: every change, oldest first, who made it and how. A line made by an
 *  agent or by a change set says so. */
export function Timeline({
  api,
  projectId,
  nodes,
  zone,
  tick,
  onError,
}: {
  api: ApiClient;
  projectId: string;
  nodes: ProjectNode[];
  zone: string;
  /** Changes when something was written: the Akte is read again. */
  tick: number;
  onError: (err: unknown) => void;
}) {
  const { t, i18n } = useTranslation();
  const [events, setEvents] = useState<ProjectEvent[] | null>(null);
  const [next, setNext] = useState<number | null>(null);
  const [group, setGroup] = useState("");
  const [node, setNode] = useState("");
  const [actor, setActor] = useState("");
  const [chain, setChain] = useState<{
    ok: boolean;
    problems: string[];
  } | null>(null);

  const load = useCallback(
    async (after: number) => {
      try {
        const page = await api.projectTimeline(projectId, {
          after,
          kinds: group ? KIND_GROUPS[group] : undefined,
          node: node || undefined,
          limit: PAGE,
        });
        setEvents((prev) =>
          after === 0 ? page.events : [...(prev ?? []), ...page.events],
        );
        setNext(page.next_after);
      } catch (err) {
        onError(err);
      }
    },
    [api, projectId, group, node, onError],
  );
  useEffect(() => {
    void Promise.resolve().then(() => load(0));
  }, [load, tick]);

  const lang = i18n.language.startsWith("de") ? "de" : "en";
  const stamp = new Intl.DateTimeFormat(i18n.language, {
    timeZone: zone,
    dateStyle: "short",
    timeStyle: "short",
  });
  const shown = (events ?? []).filter((e) => !actor || e.actor.kind === actor);
  const verify = async () => {
    try {
      setChain(await api.verifyProjectTimeline(projectId));
    } catch (err) {
      onError(err);
    }
  };
  return (
    <section className="project-timeline" aria-label={t("project.timelineTab")}>
      <div className="project-bar">
        <label>
          {t("project.timelineKind")}
          <select value={group} onChange={(e) => setGroup(e.target.value)}>
            <option value="">{t("project.timelineAllKinds")}</option>
            {Object.keys(KIND_GROUPS).map((g) => (
              <option key={g} value={g}>
                {t(`project.kindGroups.${g}`)}
              </option>
            ))}
          </select>
        </label>
        <label>
          {t("project.timelineNode")}
          <select value={node} onChange={(e) => setNode(e.target.value)}>
            <option value="">{t("project.timelineAllNodes")}</option>
            {nodes.map((n) => (
              <option key={n.id} value={n.id}>
                {n.number} {n.title}
              </option>
            ))}
          </select>
        </label>
        <label>
          {t("project.timelineActor")}
          <select value={actor} onChange={(e) => setActor(e.target.value)}>
            <option value="">{t("project.timelineAllActors")}</option>
            {(["person", "agent", "system"] as const).map((a) => (
              <option key={a} value={a}>
                {t(`project.actors.${a}`)}
              </option>
            ))}
          </select>
        </label>
        <button type="button" onClick={verify}>
          {t("project.verifyChain")}
        </button>
      </div>
      {chain && (
        <p role="status" className={chain.ok ? "project-ok" : "project-error"}>
          {chain.ok
            ? t("project.chainOk")
            : t("project.chainBroken", { problems: chain.problems.join("; ") })}
        </p>
      )}
      {events !== null && shown.length === 0 && (
        <p className="project-muted">{t("project.timelineEmpty")}</p>
      )}
      <ol className="project-events">
        {shown.map((e) => (
          <li key={e.id} className="project-event" data-kind={e.kind}>
            <time dateTime={e.at}>{stamp.format(new Date(e.at))}</time>
            <span className="project-event-label">
              {e.label[lang] ?? e.kind}
            </span>
            {e.node_ref && (
              <span className="project-event-node">
                {e.node_ref}
                {e.node_title ? ` ${e.node_title}` : ""}
              </span>
            )}
            <span className="project-muted">
              {t(`project.actors.${e.actor.kind}`, {
                defaultValue: e.actor.kind,
              })}
              {e.actor.app ? ` · ${e.actor.app}` : ""}
              {e.actor.change_set_id ? ` · ${t("project.viaChangeSet")}` : ""}
            </span>
            {facts(e.payload).map(([k, v]) => (
              <span key={k} className="project-fact">
                {k}: {v}
              </span>
            ))}
            {e.reason && <span className="project-muted">{e.reason}</span>}
          </li>
        ))}
      </ol>
      {next !== null && (
        <button type="button" onClick={() => void load(next)}>
          {t("project.timelineMore")}
        </button>
      )}
    </section>
  );
}
