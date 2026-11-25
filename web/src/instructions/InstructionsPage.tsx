import { useCallback, useEffect, useState } from "react";
import { useTranslation } from "react-i18next";
import { UnauthorizedError, type ApiClient } from "../api/client";
import { describeError, toActionError } from "../errors";
import { memoryHref } from "../route";
import type { CoreOverview } from "../types";
import { announceMemoryChange } from "../memory/useQuestions";
import { BudgetBars } from "./BudgetBars";
import { CoreEntries } from "./CoreEntries";
import { PersonaSection } from "./PersonaSection";
import { PreviewPanel } from "./PreviewPanel";
import { ProposalQueue } from "./ProposalQueue";
import { SystemRules } from "./SystemRules";
import { Wizard } from "./Wizard";
import { announceInstructionsChange, usePersonas } from "./personaUi";

/** "Instructions": what stands first in every prompt — the voice (persona), the user's own rules
 *  and styles, a profile of them, the household's facts and the fixed rules — with the token
 *  budget, the proposals waiting for a click, the history of each entry and a preview of the
 *  result. Personal to the signed-in user (an admin sees only their own, too, plus the household
 *  and the system rules, which they may change). */
export type InstructionPart = "proposals" | "persona" | "rules" | "household" | "recent" | "preview";

export function InstructionsPage({
  api,
  mode = "user",
  parts,
}: {
  api: ApiClient;
  /** "care" = the token budget and the fixed rules (admin area); the default is the rest. */
  mode?: "user" | "care";
  /** Only these blocks, without the page heading (a section of Settings). The profile is not one
   *  of them: it is the core on the memory page since phase 4. */
  parts?: readonly InstructionPart[];
}) {
  const { t, i18n } = useTranslation();
  const [core, setCore] = useState<CoreOverview | null>(null);
  const [context, setContext] = useState(16384);
  const [error, setError] = useState<string | null>(null);
  const [wizard, setWizard] = useState(false);
  const { library, reload: reloadPersonas } = usePersonas(api);

  const fail = useCallback(
    (err: unknown) => {
      if (!(err instanceof UnauthorizedError)) setError(describeError(toActionError(err), t, i18n));
    },
    [t, i18n],
  );

  const reload = useCallback(() => {
    api.core(context).then((c) => {
      setError(null);
      setCore(c);
    }, fail);
  }, [api, context, fail]);

  useEffect(reload, [reload]);

  const changed = useCallback(() => {
    reload();
    reloadPersonas();
    announceInstructionsChange();
    announceMemoryChange();
  }, [reload, reloadPersonas]);

  if (!core) {
    return error ? (
      <p className="error" role="alert">
        {error}
      </p>
    ) : (
      <p className="muted">{t("app.loading")}</p>
    );
  }

  if (mode === "care") {
    return (
      <section className="instructions">
        <h2>{t("care.instructions")}</h2>
        <p className="muted small">{t("care.instructionsIntro")}</p>
        {error && (
          <p className="error" role="alert">
            {error}
          </p>
        )}
        <BudgetBars budget={core.budget} context={context} onContext={setContext} />
        <SystemRules
          api={api}
          rules={core.system}
          admin={core.admin}
          onChanged={changed}
          onError={fail}
        />
      </section>
    );
  }

  const facts = core.profile;
  if (parts) {
    const has = (p: InstructionPart) => parts.includes(p);
    return (
      <section className="instructions">
        {error && (
          <p className="error" role="alert">
            {error}
          </p>
        )}
        {has("proposals") && core.proposals.length > 0 && (
          <section className="core-slice" data-setting="assistant-proposals" aria-label={t("instructions.proposals.heading")}>
            <h2>
              {t("instructions.proposals.heading")}
              <span className="nav-badge" data-testid="proposal-count">
                {core.proposals.length}
              </span>
            </h2>
            <p className="muted small">{t("instructions.proposals.intro")}</p>
            <ProposalQueue api={api} proposals={core.proposals} onChanged={changed} onError={fail} />
          </section>
        )}
        {has("persona") && library && (
          <div data-setting="assistant-persona">
            <PersonaSection
              api={api}
              library={library}
              admin={core.admin}
              onChanged={changed}
              onError={fail}
              onWizard={() => setWizard(true)}
            />
          </div>
        )}
        {has("rules") && (
          <div data-setting="assistant-rules">
            <CoreEntries
              api={api}
              title={t("instructions.rules.heading")}
              intro={t("instructions.rules.intro")}
              items={core.rules}
              mode="personal"
              editable
              canAdd
              onChanged={changed}
              onError={fail}
            />
          </div>
        )}
        {has("household") && (
          <div data-setting="household">
            <CoreEntries
              api={api}
              title={t("instructions.household.heading")}
              intro={core.admin ? t("instructions.household.adminNote") : t("instructions.household.readOnly")}
              items={core.household}
              mode="household"
              editable={core.admin}
              canAdd={core.admin}
              onChanged={changed}
              onError={fail}
            />
          </div>
        )}
        {has("recent") && core.recent.length > 0 && <RecentChats recent={core.recent} />}
        {has("preview") && <PreviewPanel text={core.preview} />}
        {wizard && <Wizard api={api} onClose={() => setWizard(false)} onDone={changed} />}
      </section>
    );
  }
  return (
    <section className="instructions">
      <h1>{t("instructions.heading")}</h1>
      <p>{t("instructions.intro")}</p>
      {error && (
        <p className="error" role="alert">
          {error}
        </p>
      )}

      <section className="core-slice" aria-label={t("instructions.proposals.heading")}>
        <h2>
          {t("instructions.proposals.heading")}
          {core.proposals.length > 0 && (
            <span className="nav-badge" data-testid="proposal-count">
              {core.proposals.length}
            </span>
          )}
        </h2>
        <p className="muted small">{t("instructions.proposals.intro")}</p>
        <ProposalQueue api={api} proposals={core.proposals} onChanged={changed} onError={fail} />
      </section>

      {library && (
        <PersonaSection
          api={api}
          library={library}
          admin={core.admin}
          onChanged={changed}
          onError={fail}
          onWizard={() => setWizard(true)}
        />
      )}

      <CoreEntries
        api={api}
        title={t("instructions.rules.heading")}
        intro={t("instructions.rules.intro")}
        items={core.rules}
        mode="personal"
        editable
        canAdd
        onChanged={changed}
        onError={fail}
      />

      <CoreEntries
        api={api}
        title={t("instructions.profile.heading")}
        intro={t("instructions.profile.intro")}
        items={facts}
        mode="personal"
        editable
        factsOnly
        onChanged={changed}
        onError={fail}
      />
      <p className="small">
        <a href={memoryHref}>{t("instructions.profile.link")}</a>
      </p>

      <CoreEntries
        api={api}
        title={t("instructions.household.heading")}
        intro={
          core.admin ? t("instructions.household.adminNote") : t("instructions.household.readOnly")
        }
        items={core.household}
        mode="household"
        editable={core.admin}
        canAdd={core.admin}
        onChanged={changed}
        onError={fail}
      />

      {core.recent.length > 0 && <RecentChats recent={core.recent} />}

      <PreviewPanel text={core.preview} />
      {wizard && <Wizard api={api} onClose={() => setWizard(false)} onDone={changed} />}
    </section>
  );
}

function RecentChats({ recent }: { recent: CoreOverview["recent"] }) {
  const { t } = useTranslation();
  return (
    <section className="core-slice" aria-label={t("instructions.recent.heading")}>
      <h2>{t("instructions.recent.heading")}</h2>
      <p className="muted small">{t("instructions.recent.intro")}</p>
      <ul className="core-items">
        {recent.map((c) => (
          <li key={c.id} className="core-item" data-testid="recent-chat">
            <strong>{c.title || c.day}</strong>
            <span className="muted small"> · {c.day}</span>
            <ul>
              {c.statements.map((s, i) => (
                <li key={i}>{s}</li>
              ))}
            </ul>
          </li>
        ))}
      </ul>
    </section>
  );
}
