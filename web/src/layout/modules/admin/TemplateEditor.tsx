import { useState, type FormEvent } from "react";
import { useTranslation } from "react-i18next";
import type { ApiClient } from "../../../api/client";
import { ALL_GROUPS, groupLabel } from "../../groups";
import type { UiTemplateView } from "../../types";

type Body = Parameters<ApiClient["putUiTemplate"]>[1];

interface Props {
  /** The workspace being edited; none = a new one. */
  template: UiTemplateView | null;
  /** Every module that may be put in a workspace. */
  modules: { id: string; title: string }[];
  busy: boolean;
  onSave: (key: string, body: Body) => void;
  onCancel: () => void;
}

type Tri = "default" | "on" | "off";

const toTri = (v: boolean | null | undefined): Tri => (v === true ? "on" : v === false ? "off" : "default");
const fromTri = (v: Tri): boolean | null => (v === "on" ? true : v === "off" ? false : null);

/** The form of one workspace: what it is called, which windows it holds, who may open it, and what the
 *  assistant starts with in it. */
export function TemplateEditor({ template, modules, busy, onSave, onCancel }: Props) {
  const { t } = useTranslation();
  const scope = template?.agent.tool_scope;
  const [key, setKey] = useState(template?.key ?? "");
  const [title, setTitle] = useState(template?.title ?? "");
  const [icon, setIcon] = useState(template?.icon ?? "");
  const [position, setPosition] = useState(template?.position ?? 50);
  const [chosen, setChosen] = useState<ReadonlySet<string>>(new Set(template?.modules ?? []));
  const [adminOnly, setAdminOnly] = useState(template?.requires_role === "admin");
  const [allGroups, setAllGroups] = useState(scope?.groups == null);
  const [groups, setGroups] = useState<ReadonlySet<string>>(new Set(scope?.groups ?? []));
  const [internet, setInternet] = useState<Tri>(toTri(scope?.internet));
  const [memory, setMemory] = useState<"default" | "off">(scope?.memory === false ? "off" : "default");
  const [role, setRole] = useState<"none" | "fast" | "deep">(template?.agent.model_role ?? "none");

  const toggle = (set: ReadonlySet<string>, id: string, on: boolean): ReadonlySet<string> => {
    const next = new Set(set);
    if (on) next.add(id);
    else next.delete(id);
    return next;
  };

  const submit = (e: FormEvent) => {
    e.preventDefault();
    onSave(key, {
      title,
      icon,
      position,
      modules: modules.map((m) => m.id).filter((id) => chosen.has(id)),
      requires_role: adminOnly ? "admin" : null,
      agent: {
        tool_scope: {
          nodes: null,
          groups: allGroups ? null : ALL_GROUPS.filter((g) => groups.has(g)),
          internet: fromTri(internet),
          ...(memory === "off" ? { memory: false } : { memory: null }),
        },
        persona: template?.agent.persona ?? null,
        model_role: role === "none" ? null : role,
      },
    });
  };

  return (
    <form className="ws-edit card" onSubmit={submit} aria-label={t(template ? "layout.admin.editWorkspace" : "layout.admin.newWorkspace")}>
      <h3>{t(template ? "layout.admin.editWorkspace" : "layout.admin.newWorkspace")}</h3>
      <div className="ws-edit-row">
        <label>
          {t("layout.admin.key")}
          <input
            value={key}
            disabled={template !== null}
            pattern="[a-z0-9][a-z0-9\-]{0,39}"
            required
            onChange={(e) => setKey(e.target.value)}
          />
        </label>
        <label>
          {t("layout.admin.title")}
          <input value={title} required maxLength={80} onChange={(e) => setTitle(e.target.value)} />
        </label>
        <label>
          {t("layout.admin.icon")}
          <input value={icon} maxLength={8} size={4} onChange={(e) => setIcon(e.target.value)} />
        </label>
        <label>
          {t("layout.admin.position")}
          <input type="number" min={0} max={1000} value={position} onChange={(e) => setPosition(Number(e.target.value))} />
        </label>
      </div>
      <div className="ws-edit-row">
        <label className="check">
          <input type="checkbox" checked={adminOnly} onChange={(e) => setAdminOnly(e.target.checked)} /> {t("layout.admin.adminOnly")}
        </label>
      </div>
      <fieldset>
        <legend>{t("layout.admin.modules")}</legend>
        <div className="ws-edit-grid">
          {modules.map((m) => (
            <label key={m.id} className="check">
              <input type="checkbox" checked={chosen.has(m.id)} onChange={(e) => setChosen(toggle(chosen, m.id, e.target.checked))} /> {m.title}
            </label>
          ))}
        </div>
      </fieldset>
      <fieldset>
        <legend>{t("layout.admin.agent")}</legend>
        <label className="check">
          <input type="checkbox" checked={allGroups} onChange={(e) => setAllGroups(e.target.checked)} /> {t("layout.admin.allGroups")}
        </label>
        {!allGroups && (
          <div className="ws-edit-grid">
            {ALL_GROUPS.map((g) => (
              <label key={g} className="check">
                <input type="checkbox" checked={groups.has(g)} onChange={(e) => setGroups(toggle(groups, g, e.target.checked))} /> {groupLabel(t, g)}
              </label>
            ))}
          </div>
        )}
        <div className="ws-edit-row">
          <label>
            {t("layout.admin.internet")}
            <select value={internet} onChange={(e) => setInternet(e.target.value as Tri)}>
              <option value="default">{t("layout.admin.useDefault")}</option>
              <option value="on">{t("layout.admin.on")}</option>
              <option value="off">{t("layout.admin.off")}</option>
            </select>
          </label>
          <label>
            {t("layout.admin.memory")}
            <select value={memory} onChange={(e) => setMemory(e.target.value as "default" | "off")}>
              <option value="default">{t("layout.admin.useDefault")}</option>
              <option value="off">{t("layout.admin.incognito")}</option>
            </select>
          </label>
          <label>
            {t("layout.admin.modelRole")}
            <select value={role} onChange={(e) => setRole(e.target.value as "none" | "fast" | "deep")}>
              <option value="none">{t("layout.admin.useDefault")}</option>
              <option value="fast">{t("layout.admin.fast")}</option>
              <option value="deep">{t("layout.admin.deep")}</option>
            </select>
          </label>
        </div>
      </fieldset>
      <div className="ws-edit-row">
        <button type="submit" className="primary" disabled={busy || !key || !title}>
          {t("layout.admin.save")}
        </button>
        <button type="button" onClick={onCancel}>
          {t("layout.admin.cancel")}
        </button>
      </div>
    </form>
  );
}
