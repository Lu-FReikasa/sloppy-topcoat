import { useCallback, useEffect, useMemo, useState } from "react";
import { useTranslation } from "react-i18next";
import type { ApiClient } from "../../api/client";
import { type UiBootstrap, type UiProfileView, type UiTemplateView, type UiUserView } from "../types";
import { ProfileEditor } from "./admin/ProfileEditor";
import { TemplateEditor } from "./admin/TemplateEditor";
import { programTitle } from "../../i18n/shipped";
import { moduleMeta } from "./meta";

interface Props {
  api: ApiClient;
  boot: UiBootstrap | null;
  /** One half only (Administration since phase 4): the applications with the profiles that grant
   *  them, or the people and their profile. Without it both. */
  part?: "apps" | "people";
}

function describe(err: unknown): string {
  return err instanceof Error ? err.message : String(err);
}

/** Workspaces, profiles and who has which, for the admin. A change reaches open pages at once. */
export function WorkspacesAdmin({ api, boot, part }: Props) {
  const { t } = useTranslation();
  const [templates, setTemplates] = useState<UiTemplateView[]>([]);
  const [profiles, setProfiles] = useState<UiProfileView[]>([]);
  const [users, setUsers] = useState<UiUserView[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [note, setNote] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [editing, setEditing] = useState<UiTemplateView | "new" | null>(null);
  const [editingProfile, setEditingProfile] = useState<UiProfileView | "new" | null>(null);
  const [copyFrom, setCopyFrom] = useState<string | null>(null);
  const [copyKey, setCopyKey] = useState("");
  const [confirmDelete, setConfirmDelete] = useState<string | null>(null);

  const load = useCallback(() => {
    Promise.all([api.uiTemplates(), api.uiProfiles(), api.uiUsers()]).then(
      ([tpl, prof, usr]) => {
        setTemplates(tpl.templates);
        setProfiles(prof.profiles);
        setUsers(usr.users);
        setError(null);
      },
      (err: unknown) => setError(describe(err)),
    );
  }, [api]);

  useEffect(load, [load]);

  /** Do a change, say so, and read everything again. Says whether it worked, so a form stays open
   *  when the hub refused. */
  const run = useCallback(
    async (what: () => Promise<unknown>, done: string): Promise<boolean> => {
      setBusy(true);
      setNote(null);
      try {
        await what();
        setError(null);
        setNote(done);
        load();
        return true;
      } catch (err) {
        setError(describe(err));
        return false;
      } finally {
        setBusy(false);
      }
    },
    [load],
  );

  const title = (w: UiTemplateView) => programTitle(w, t);
  const moduleChoices = useMemo(
    () =>
      (boot?.modules ?? [])
        .map((m) => ({ id: m.id, meta: moduleMeta(m.id) }))
        .filter((m) => m.meta && !m.meta.needsProps)
        .map((m) => ({ id: m.id, title: t(m.meta?.titleKey ?? m.id) })),
    [boot, t],
  );
  const workspaceChoices = templates.map((w) => ({ key: w.key, title: title(w) }));

  return (
    <section className="ws-admin">
      {error && (
        <p className="bad" role="alert">
          {error}
        </p>
      )}
      {note && (
        <p className="muted" role="status">
          {note}
        </p>
      )}

      {part !== "people" && (
      <>
      <h2>{t("layout.admin.workspaces")}</h2>
      <table className="ws-admin-table">
        <thead>
          <tr>
            <th>{t("layout.admin.title")}</th>
            <th>{t("layout.admin.modules")}</th>
            <th>{t("layout.admin.rev")}</th>
            <th />
          </tr>
        </thead>
        <tbody>
          {templates.map((w) => (
            <tr key={w.key}>
              <td>
                {w.icon} {title(w)}
                {w.requires_role === "admin" && <span className="badge"> {t("layout.admin.adminOnly")}</span>}
                {w.builtin && <span className="badge"> {t("layout.admin.builtin")}</span>}
              </td>
              <td>{w.modules.length}</td>
              <td>{w.rev}</td>
              <td className="ws-admin-actions">
                <button type="button" disabled={w.builtin || busy} onClick={() => setEditing(w)}>
                  {t("layout.admin.edit")}
                </button>
                <button type="button" disabled={busy} onClick={() => { setCopyFrom(w.key); setCopyKey(`${w.key}-2`); }}>
                  {t("layout.admin.duplicate")}
                </button>
                <button type="button" disabled={w.builtin || busy} onClick={() => setConfirmDelete(w.key)}>
                  {t("layout.admin.delete")}
                </button>
              </td>
            </tr>
          ))}
        </tbody>
      </table>
      {copyFrom && (
        <form
          className="ws-edit-row"
          onSubmit={(e) => {
            e.preventDefault();
            void run(() => api.duplicateUiTemplate(copyFrom, copyKey), t("layout.admin.done")).then((ok) => ok && setCopyFrom(null));
          }}
        >
          <label>
            {t("layout.admin.copyAs", { key: copyFrom })}
            <input value={copyKey} required pattern="[a-z0-9][a-z0-9\-]{0,39}" onChange={(e) => setCopyKey(e.target.value)} />
          </label>
          <button type="submit" className="primary" disabled={busy}>
            {t("layout.admin.duplicate")}
          </button>
          <button type="button" onClick={() => setCopyFrom(null)}>
            {t("layout.admin.cancel")}
          </button>
        </form>
      )}
      {confirmDelete && (
        <p role="alertdialog" aria-label={t("layout.admin.confirmDelete", { key: confirmDelete })}>
          {t("layout.admin.confirmDelete", { key: confirmDelete })}{" "}
          <button
            type="button"
            className="danger"
            onClick={() => void run(() => api.deleteUiTemplate(confirmDelete), t("layout.admin.done")).then((ok) => ok && setConfirmDelete(null))}
          >
            {t("layout.admin.delete")}
          </button>{" "}
          <button type="button" onClick={() => setConfirmDelete(null)}>
            {t("layout.admin.cancel")}
          </button>
        </p>
      )}
      {editing ? (
        <TemplateEditor
          template={editing === "new" ? null : editing}
          modules={moduleChoices}
          busy={busy}
          onSave={(key, body) => void run(() => api.putUiTemplate(key, body), t("layout.admin.saved")).then((ok) => ok && setEditing(null))}
          onCancel={() => setEditing(null)}
        />
      ) : (
        <p>
          <button type="button" onClick={() => setEditing("new")}>
            {t("layout.admin.newWorkspace")}
          </button>
        </p>
      )}

      <h2>{t("layout.admin.profiles")}</h2>
      <ul className="ws-admin-profiles">
        {profiles.map((p) => (
          <li key={p.name}>
            <strong>{p.name}</strong>
            {p.default && <span className="badge"> {t("layout.admin.default")}</span>}
            <span className="muted"> {t("layout.admin.grants", { count: p.grants.length })}</span>{" "}
            <button type="button" disabled={busy} onClick={() => setEditingProfile(p)}>
              {t("layout.admin.edit")}
            </button>{" "}
            <button
              type="button"
              disabled={busy || p.builtin || p.default}
              onClick={() => void run(() => api.deleteUiProfile(p.name), t("layout.admin.done"))}
            >
              {t("layout.admin.delete")}
            </button>
          </li>
        ))}
      </ul>
      {editingProfile ? (
        <ProfileEditor
          profile={editingProfile === "new" ? null : editingProfile}
          workspaces={workspaceChoices}
          busy={busy}
          onSave={(name, body) => void run(() => api.putUiProfile(name, body), t("layout.admin.saved")).then((ok) => ok && setEditingProfile(null))}
          onCancel={() => setEditingProfile(null)}
        />
      ) : (
        <p>
          <button type="button" onClick={() => setEditingProfile("new")}>
            {t("layout.admin.newProfile")}
          </button>
        </p>
      )}

      </>
      )}

      {part !== "apps" && (
      <>
      <h2>{t("layout.admin.people")}</h2>
      <table className="ws-admin-table">
        <tbody>
          {users.map((u) => (
            <tr key={u.id}>
              <td>
                {u.name} {u.role === "admin" && <span className="badge">{t("layout.admin.adminRole")}</span>}
              </td>
              <td>
                <select
                  aria-label={t("layout.admin.profileOf", { name: u.name })}
                  value={u.profile ?? ""}
                  disabled={busy}
                  onChange={(e) => void run(() => api.putUiUserProfile(u.id, e.target.value || null), t("layout.admin.saved"))}
                >
                  <option value="">{t("layout.admin.noProfile")}</option>
                  {profiles.map((p) => (
                    <option key={p.name} value={p.name}>
                      {p.name}
                    </option>
                  ))}
                </select>
              </td>
            </tr>
          ))}
        </tbody>
      </table>
      </>
      )}
    </section>
  );
}
