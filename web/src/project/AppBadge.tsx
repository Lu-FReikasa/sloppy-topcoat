import { useTranslation } from "react-i18next";

/** The application a task works in, and whether it may propose changes (project_runs.py).
 *  Colour per application; an outline means the task only reads, a filled badge means it may
 *  propose changes. The administration says so in words: its tasks run with admin rights. */
export function AppBadge({
  application,
  mode,
}: {
  application?: string | null;
  mode?: string | null;
}) {
  const { t } = useTranslation();
  if (!application) return null;
  const writes = mode === "schreiben";
  const modeKey = writes ? "schreiben" : "lesen";
  return (
    <span
      className={`app-badge app-${application}${writes ? " is-write" : ""}`}
      data-testid="app-badge"
      data-app={application}
      data-mode={modeKey}
      title={t(`project.mode.${modeKey}`)}
    >
      {t(`project.app.${application}`, { defaultValue: application })}
      {" · "}
      {t(`project.mode.${modeKey}`)}
      {application === "admin" && ` · ${t("project.app.adminRights")}`}
    </span>
  );
}
