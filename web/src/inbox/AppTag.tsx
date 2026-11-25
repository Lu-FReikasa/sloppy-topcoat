import { useTranslation } from "react-i18next";

/** The application an item comes from, as a tag in the colour of that application (the same in the
 *  inbox, on the board and in the filed corner). An application nobody named keeps its key. */
export function AppTag({ application }: { application: string }) {
  const { t } = useTranslation();
  return (
    <span className={`app-tag app-tag-${application}`} data-testid="app-tag">
      {t(`inbox.apps.${application}`, { defaultValue: application })}
    </span>
  );
}

export { APPLICATIONS } from "../chat/applications";
