import { useCallback, useEffect, useMemo, useState } from "react";
import { useTranslation } from "react-i18next";
import { UnauthorizedError, type ApiClient } from "../api/client";
import { describeError, toActionError } from "../errors";
import type {
  IdentityApp,
  IdentityGroup,
  IdentityPerson,
  IdentitySource,
  IdentityView,
} from "../types";

interface Props {
  api: ApiClient;
  isAdmin: boolean;
}

/** The identity view (E3-12): who is in which group and which group opens which app — read from
 *  Keycloak through a tool node, shown with its age and with what it cannot see. Nothing here
 *  changes anything: access grants that write to the backends are a later step. */
export function IdentityPage({ api, isAdmin }: Props) {
  const { t, i18n } = useTranslation();
  const [view, setView] = useState<IdentityView | null>(null);
  const [error, setError] = useState<string | null>(null);
  // the number of the read asked for and of the one that came back: busy = they differ
  const [asked, setAsked] = useState({ n: 1, refresh: false });
  const [done, setDone] = useState(0);
  const busy = done !== asked.n;

  const fail = useCallback(
    (err: unknown) => {
      if (!(err instanceof UnauthorizedError)) setError(describeError(toActionError(err), t, i18n));
    },
    [t, i18n],
  );

  useEffect(() => {
    let live = true;
    api.identity(asked.refresh).then(
      (v) => {
        if (!live) return;
        setError(null);
        setView(v);
        setDone(asked.n);
      },
      (err) => {
        if (!live) return;
        fail(err);
        setDone(asked.n);
      },
    );
    return () => {
      live = false;
    };
  }, [api, asked, fail]);

  const load = (refresh: boolean) => setAsked((a) => ({ n: a.n + 1, refresh }));

  return (
    <section className="identity">
      <h1>{t("identity.heading")}</h1>
      <p className="muted">{t("identity.intro")}</p>
      {error && (
        <p className="error" role="alert">
          {error}
        </p>
      )}
      {view && view.sources.length === 0 && <p className="muted">{t("identity.none")}</p>}
      {view?.sources.map((s) => (
        <Source
          key={s.node.id}
          source={s}
          busy={busy}
          onRefresh={() => load(true)}
          appGroups={view.app_groups}
          isAdmin={isAdmin}
          api={api}
          onSaved={() => load(false)}
          fail={fail}
        />
      ))}
    </section>
  );
}

function ageText(seconds: number, t: (k: string, o?: Record<string, unknown>) => string): string {
  if (seconds < 90) return t("inventory.ageSeconds", { count: seconds });
  if (seconds < 5400) return t("inventory.ageMinutes", { count: Math.round(seconds / 60) });
  return t("inventory.ageHours", { count: Math.round(seconds / 3600) });
}

function Source({
  source: s,
  busy,
  onRefresh,
  appGroups,
  isAdmin,
  api,
  onSaved,
  fail,
}: {
  source: IdentitySource;
  busy: boolean;
  onRefresh: () => void;
  appGroups: Record<string, string[]>;
  isAdmin: boolean;
  api: ApiClient;
  onSaved: () => void;
  fail: (e: unknown) => void;
}) {
  const { t } = useTranslation();
  const [filter, setFilter] = useState("");
  const [open, setOpen] = useState<string | null>(null);
  const needle = filter.trim().toLowerCase();
  const people = useMemo(
    () =>
      (s.people ?? []).filter(
        (p) => !needle || `${p.username} ${p.name} ${p.groups.join(" ")}`.toLowerCase().includes(needle),
      ),
    [s.people, needle],
  );
  const apps = useMemo(
    () => (s.apps ?? []).filter((a) => !a.builtin || a.people > 0),
    [s.apps],
  );

  return (
    <article className="card" data-testid="identity-source">
      <div className="card-head">
        <h2>{s.node.name}</h2>
        {s.realm && <span className="badge">{t("identity.realm", { realm: s.realm })}</span>}
        <span className="spacer" />
        <button type="button" disabled={busy} onClick={onRefresh}>
          {t("identity.refresh")}
        </button>
      </div>
      {s.error && (
        <p className="error" role="alert" data-testid="identity-error">
          {t("identity.sourceFailed", { message: s.error.message })}
        </p>
      )}
      {s.age_seconds !== undefined && (
        <p className="muted small">{t("identity.lastLook", { age: ageText(s.age_seconds, t) })}</p>
      )}
      {(s.notes ?? []).map((n) => (
        <p className="notice small" key={n} data-testid="identity-note">
          {n}
        </p>
      ))}

      {s.people && (
        <>
          <h3>
            {t("identity.people")} <span className="muted small">({people.length})</span>
          </h3>
          <input
            type="search"
            aria-label={t("identity.filter")}
            placeholder={t("identity.filter")}
            value={filter}
            onChange={(e) => setFilter(e.target.value)}
          />
          <div className="table-wrap">
            <table className="models" data-testid="identity-people">
              <tbody>
                {people.map((p) => (
                  <PersonRow
                    key={p.id}
                    p={p}
                    open={open === p.id}
                    onToggle={() => setOpen(open === p.id ? null : p.id)}
                  />
                ))}
              </tbody>
            </table>
          </div>
        </>
      )}

      {s.groups && (
        <>
          <h3>
            {t("identity.groups")} <span className="muted small">({s.groups.length})</span>
          </h3>
          <div className="table-wrap">
            <table className="models" data-testid="identity-groups">
              <tbody>
                {s.groups.map((g) => (
                  <GroupRow key={g.id || g.path} g={g} />
                ))}
              </tbody>
            </table>
          </div>
        </>
      )}

      {s.apps && (
        <>
          <h3>
            {t("identity.apps")} <span className="muted small">({apps.length})</span>
          </h3>
          <div className="table-wrap">
            <table className="models" data-testid="identity-apps">
              <tbody>
                {apps.map((a) => (
                  <AppRow
                    key={a.client_id}
                    a={a}
                    configured={appGroups[a.client_id] ?? []}
                    appGroups={appGroups}
                    isAdmin={isAdmin}
                    api={api}
                    onSaved={onSaved}
                    fail={fail}
                  />
                ))}
              </tbody>
            </table>
          </div>
        </>
      )}
    </article>
  );
}

function PersonRow({ p, open, onToggle }: { p: IdentityPerson; open: boolean; onToggle: () => void }) {
  const { t } = useTranslation();
  return (
    <>
      <tr className={p.enabled ? undefined : "muted"}>
        <td>
          <button type="button" className="link" onClick={onToggle} aria-expanded={open}>
            {p.name ? `${p.name} (${p.username})` : p.username}
          </button>
        </td>
        <td>{p.enabled ? t("identity.enabled") : t("identity.disabled")}</td>
        <td className="muted small">{t(`identity.source.${p.source}`)}</td>
        <td className="small">{p.groups.join(", ") || t("identity.noGroups")}</td>
        <td className="small">{p.apps.map((a) => a.name).join(", ") || t("identity.noApps")}</td>
      </tr>
      {open && (
        <tr data-testid="identity-person-detail">
          <td colSpan={5}>
            {p.apps.length === 0 && <p className="muted">{t("identity.noAppsWhy")}</p>}
            <ul>
              {p.apps.map((a) => (
                <li key={a.app}>
                  <strong>{a.name}</strong>
                  {a.roles.length > 0 && <> — {t("identity.roles", { roles: a.roles.join(", ") })}</>}
                  <ul>
                    {a.via.map((v) => (
                      <li key={`${v.group}-${v.member_of}-${v.kind}`} className="small">
                        {v.kind === "configured"
                          ? t("identity.viaConfigured", { member: v.member_of, group: v.group })
                          : v.member_of === v.group
                            ? t("identity.viaDirect", { group: v.group })
                            : t("identity.viaParent", { member: v.member_of, group: v.group })}
                      </li>
                    ))}
                  </ul>
                </li>
              ))}
            </ul>
          </td>
        </tr>
      )}
    </>
  );
}

function GroupRow({ g }: { g: IdentityGroup }) {
  const { t } = useTranslation();
  const apps = [...new Set(g.opens.map((o) => o.app))];
  return (
    <tr>
      <td>{g.path}</td>
      <td className="small">
        {g.members_ok ? t("identity.memberCount", { count: g.members }) : t("identity.membersUnknown")}
      </td>
      <td className="small">{apps.join(", ") || t("identity.opensNothing")}</td>
    </tr>
  );
}

function AppRow({
  a,
  configured,
  appGroups,
  isAdmin,
  api,
  onSaved,
  fail,
}: {
  a: IdentityApp;
  configured: string[];
  appGroups: Record<string, string[]>;
  isAdmin: boolean;
  api: ApiClient;
  onSaved: () => void;
  fail: (e: unknown) => void;
}) {
  const { t } = useTranslation();
  const [editing, setEditing] = useState(false);
  const [text, setText] = useState(configured.join(", "));
  const [saving, setSaving] = useState(false);

  async function save() {
    setSaving(true);
    try {
      const next = { ...appGroups };
      const paths = text
        .split(",")
        .map((x) => x.trim())
        .filter((x) => x.startsWith("/"));
      if (paths.length > 0) next[a.client_id] = paths;
      else delete next[a.client_id];
      await api.setIdentityAppGroups(next);
      setEditing(false);
      onSaved();
    } catch (err) {
      fail(err);
    } finally {
      setSaving(false);
    }
  }

  return (
    <tr className={a.enabled ? undefined : "muted"}>
      <td>
        {a.name}
        {a.name !== a.client_id && <span className="muted small"> ({a.client_id})</span>}
      </td>
      <td className="small">
        {t("identity.peopleCount", { count: a.people })}
        {a.people_enabled !== a.people && ` ${t("identity.ofWhichDisabled", { count: a.people - a.people_enabled })}`}
      </td>
      <td className="small">{a.groups.join(", ") || t("identity.noGroupsOpen")}</td>
      <td className="small">
        {editing ? (
          <span className="row-actions">
            <input
              type="text"
              aria-label={t("identity.readsGroups")}
              placeholder="/family, /admins"
              value={text}
              onChange={(e) => setText(e.target.value)}
            />
            <button type="button" className="small primary" disabled={saving} onClick={() => void save()}>
              {t("identity.save")}
            </button>
          </span>
        ) : (
          <>
            {configured.length > 0 && (
              <span className="muted">{t("identity.configuredGroups", { groups: configured.join(", ") })} </span>
            )}
            {isAdmin && (
              <button type="button" className="small" onClick={() => setEditing(true)}>
                {t("identity.readsGroups")}
              </button>
            )}
          </>
        )}
      </td>
    </tr>
  );
}
