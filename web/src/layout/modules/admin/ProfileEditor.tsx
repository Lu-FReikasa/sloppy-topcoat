import { useState, type FormEvent } from "react";
import { useTranslation } from "react-i18next";
import { TOOL_GROUPS, groupLabel } from "../../groups";
import type { UiGrant, UiProfileView } from "../../types";

interface Props {
  profile: UiProfileView | null;
  workspaces: { key: string; title: string }[];
  busy: boolean;
  onSave: (name: string, body: { description: string; default: boolean; grants: UiGrant[] }) => void;
  onCancel: () => void;
}

type Choice = "none" | "allow" | "deny";

/** The form of one profile: which workspaces its people are offered and which tool groups are taken
 *  away from them. Rights only narrow: what is not denied stays as the person's own grants say. */
export function ProfileEditor({ profile, workspaces, busy, onSave, onCancel }: Props) {
  const { t } = useTranslation();
  const have = (kind: UiGrant["kind"], key: string): Choice =>
    profile?.grants.find((g) => g.kind === kind && g.key === key)?.effect ?? "none";
  const [name, setName] = useState(profile?.name ?? "");
  const [description, setDescription] = useState(profile?.description ?? "");
  const [isDefault, setIsDefault] = useState(profile?.default ?? false);
  const [ws, setWs] = useState<Record<string, Choice>>(() => Object.fromEntries(workspaces.map((w) => [w.key, have("workspace", w.key)])));
  const [denied, setDenied] = useState<ReadonlySet<string>>(
    new Set(profile?.grants.filter((g) => g.kind === "tool" && g.effect === "deny").map((g) => g.key) ?? []),
  );

  const submit = (e: FormEvent) => {
    e.preventDefault();
    const grants: UiGrant[] = [
      ...workspaces.flatMap((w): UiGrant[] => (ws[w.key] && ws[w.key] !== "none" ? [{ kind: "workspace", key: w.key, effect: ws[w.key] as "allow" | "deny" }] : [])),
      ...TOOL_GROUPS.filter((g) => denied.has(g)).map((g): UiGrant => ({ kind: "tool", key: g, effect: "deny" })),
      ...(denied.has("web") ? [{ kind: "tool", key: "web", effect: "deny" } as UiGrant] : []),
    ];
    onSave(name, { description, default: isDefault, grants });
  };

  return (
    <form className="ws-edit card" onSubmit={submit} aria-label={t(profile ? "layout.admin.editProfile" : "layout.admin.newProfile")}>
      <h3>{t(profile ? "layout.admin.editProfile" : "layout.admin.newProfile")}</h3>
      <div className="ws-edit-row">
        <label>
          {t("layout.admin.profileName")}
          <input value={name} disabled={profile !== null} required maxLength={60} onChange={(e) => setName(e.target.value)} />
        </label>
        <label>
          {t("layout.admin.description")}
          <input value={description} onChange={(e) => setDescription(e.target.value)} />
        </label>
      </div>
      <div className="ws-edit-row">
        <label className="check">
          <input type="checkbox" checked={isDefault} onChange={(e) => setIsDefault(e.target.checked)} /> {t("layout.admin.defaultProfile")}
        </label>
      </div>
      <fieldset>
        <legend>{t("layout.admin.workspaceRights")}</legend>
        {workspaces.map((w) => (
          <label key={w.key} className="ws-edit-line">
            <span>{w.title}</span>
            <select value={ws[w.key] ?? "none"} onChange={(e) => setWs({ ...ws, [w.key]: e.target.value as Choice })}>
              <option value="none">{t("layout.admin.notOffered")}</option>
              <option value="allow">{t("layout.admin.allow")}</option>
              <option value="deny">{t("layout.admin.deny")}</option>
            </select>
          </label>
        ))}
      </fieldset>
      <fieldset>
        <legend>{t("layout.admin.toolRights")}</legend>
        <div className="ws-edit-grid">
          {[...TOOL_GROUPS, "web"].map((g) => (
            <label key={g} className="check">
              <input
                type="checkbox"
                checked={denied.has(g)}
                onChange={(e) => {
                  const next = new Set(denied);
                  if (e.target.checked) next.add(g);
                  else next.delete(g);
                  setDenied(next);
                }}
              />{" "}
              {t("layout.admin.takeAway", { group: g === "web" ? t("layout.admin.internet") : groupLabel(t, g) })}
            </label>
          ))}
        </div>
      </fieldset>
      <div className="ws-edit-row">
        <button type="submit" className="primary" disabled={busy || !name}>
          {t("layout.admin.save")}
        </button>
        <button type="button" onClick={onCancel}>
          {t("layout.admin.cancel")}
        </button>
      </div>
    </form>
  );
}
