import { useState } from "react";
import { useTranslation } from "react-i18next";
import type { ApiClient } from "../api/client";
import { instructionsHref } from "../route";
import { announceInstructionsChange, usePersonas } from "./personaUi";
import { Wizard } from "./Wizard";

/** The first-start wizard for a new user, a quiet banner for everybody who has no persona (an
 *  existing user is never forced into anything: "later" and "skip" are there, and the banner can
 *  be closed for this session). Mounted once, for the whole app; ``quiet`` keeps the banner away
 *  (on the chat page every pixel of height belongs to the conversation). */
export function PersonaPrompt({ api, quiet = false }: { api: ApiClient; quiet?: boolean }) {
  const { t } = useTranslation();
  const { library, reload } = usePersonas(api);
  const [open, setOpen] = useState(false);
  const [dismissed, setDismissed] = useState(false);
  const [wizardSeen, setWizardSeen] = useState(false);
  if (!library) return null;
  const wizard = library.wizard;
  const showWizard = open || (wizard.show && !wizardSeen);

  async function away(state: "later" | "skipped") {
    try {
      await api.setWizardState(state);
    } catch {
      /* the banner stays; try again later */
    }
    announceInstructionsChange();
    reload();
  }

  return (
    <>
      {wizard.banner && !quiet && !dismissed && !showWizard && (
        <p className="notice persona-banner" role="status" data-testid="persona-banner">
          <span>{t("persona.banner.text")} </span>
          <button type="button" className="primary" onClick={() => setOpen(true)}>
            {t("persona.banner.start")}
          </button>{" "}
          <a href={instructionsHref}>{t("persona.banner.link")}</a>{" "}
          <button type="button" onClick={() => setDismissed(true)}>
            {t("persona.wizard.later")}
          </button>{" "}
          <button type="button" onClick={() => void away("skipped")}>
            {t("persona.wizard.skip")}
          </button>
        </p>
      )}
      {showWizard && (
        <Wizard
          api={api}
          onClose={() => {
            setOpen(false);
            setWizardSeen(true);
          }}
          onDone={reload}
        />
      )}
    </>
  );
}
