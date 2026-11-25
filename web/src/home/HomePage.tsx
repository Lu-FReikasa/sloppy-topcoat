import { useTranslation } from "react-i18next";
import { programHue, programTitle, viewTitle } from "../desktop/programs";
import { requestOpen } from "../layout/intent";
import { moduleMeta } from "../layout/modules/meta";
import type { UiModuleInfo, Workspace } from "../layout/types";

interface Props {
  workspaces: readonly Workspace[];
  modules: readonly UiModuleInfo[];
  /** The program this overview belongs to (Start): it is not listed in itself. */
  self: string;
}

/** Start › Anwendungen (the maintainer, 2026-11-13): every application at a glance, what it is
 *  for and its views, each one click away. The taskbar's start button opens it. */
export function HomePage({ workspaces, modules, self }: Props) {
  const { t } = useTranslation();
  const allowed = new Map(modules.map((m) => [m.id, m.allowed]));
  const views = (w: Workspace) =>
    w.modules.filter((id) => {
      const meta = moduleMeta(id);
      return meta !== undefined && !meta.needsProps && (allowed.get(id) ?? false);
    });
  const open = (w: Workspace, module?: string) => {
    const first = module ?? views(w)[0];
    if (first) requestOpen({ module: first, props: {}, program: w.key });
  };
  const apps = workspaces.filter((w) => w.key !== self);
  return (
    <section className="home">
      <h1>{t("home.heading")}</h1>
      <p className="muted">{t("home.intro")}</p>
      <ul className="home-grid">
        {apps.map((w) => {
          const list = views(w);
          return (
            <li key={w.key} className="home-card" style={{ ["--pg-hue" as string]: String(programHue(w.key)) }}>
              <button type="button" className="home-open" onClick={() => open(w)}>
                <span className="home-icon" aria-hidden="true">
                  {w.icon}
                </span>
                <span className="home-title">{programTitle(w, t)}</span>
              </button>
              <p className="muted small">{t(`home.purpose.${w.key}`, { defaultValue: "" })}</p>
              {list.length > 1 && (
                <p className="home-views small">
                  {list.map((m) => (
                    <button key={m} type="button" className="linklike" onClick={() => open(w, m)}>
                      {viewTitle(m, t)}
                    </button>
                  ))}
                </p>
              )}
            </li>
          );
        })}
      </ul>
      <p className="muted small">{t("home.paletteHint")}</p>
    </section>
  );
}
