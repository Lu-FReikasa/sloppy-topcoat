import { useEffect, useState } from "react";
import { useTranslation } from "react-i18next";
import type { ApiClient } from "../api/client";
import type { AgentContractView, UiAgentDefaults } from "../layout/types";
import { SensitiveReleases } from "../memory/SensitiveReleases";
import type { ChatToolsOverview } from "../types";

interface Props {
  api: ApiClient;
  /** The program (application) these settings are of. */
  workspace: string;
}

/** One part of the contract: a small heading and its list; nothing when it is empty. */
function ContractList({ title, items }: { title: string; items: string[] }) {
  if (items.length === 0) return null;
  return (
    <>
      <p className="fact-label">{title}</p>
      <ul>
        {items.map((x) => (
          <li key={x}>{x}</li>
        ))}
      </ul>
    </>
  );
}

/** The settings of one program that concern the person (docs/DESKTOP.md): the frame its assistant
 *  answers in (D74), what it may use (its tools are a ceiling, set by the admin, D69) and which
 *  sensitive branches of the person's core it may see.
 *  What it may use is listed whether or not a machine offers it right now; what no connected
 *  machine offers is marked. */
export function ProgramSettings({ api, workspace }: Props) {
  const { t, i18n } = useTranslation();
  // the contract comes in the language the GUI shows (the person's, else the browser's)
  const language = i18n.resolvedLanguage === "de" ? "de" : "en";
  const [ceiling, setCeiling] = useState<UiAgentDefaults["tool_scope"] | null>(
    null,
  );
  const [frame, setFrame] = useState<UiAgentDefaults["frame"]>(null);
  const [contract, setContract] = useState<AgentContractView | null>(null);
  const [reach, setReach] = useState<ChatToolsOverview | null>(null);
  const [failed, setFailed] = useState(false);

  useEffect(() => {
    let alive = true;
    api.uiWorkspaceAgent(workspace, language).then(
      (a) => {
        if (!alive) return;
        setCeiling(
          a.tool_scope ?? { nodes: null, groups: null, internet: null },
        );
        setFrame(a.frame ?? null);
        setContract(a.contract ?? null);
      },
      () => alive && setFailed(true),
    );
    api.chatTools(workspace).then(
      (o) => alive && setReach(o),
      () => undefined,
    );
    return () => {
      alive = false;
    };
  }, [api, workspace, language]);

  const reachable = new Set(reach?.groups.map((g) => g.key) ?? []);
  const away = (ok: boolean) =>
    reach && !ok ? (
      <span className="muted small"> ({t("program.unreachable")})</span>
    ) : null;

  return (
    <section className="program-settings">
      {contract && (
        <section className="card" data-testid="program-contract">
          <h2>{t("program.contract", { title: contract.title })}</h2>
          <p>{contract.purpose}</p>
          <ContractList
            title={t("program.contractTasks")}
            items={contract.tasks.map((x) =>
              t("program.contractTask", { text: x.text, example: x.example }),
            )}
          />
          <ContractList
            title={t("program.contractTools")}
            items={contract.tools}
          />
          <ContractList
            title={t("program.contractNever")}
            items={contract.never}
          />
          <ContractList
            title={t("program.contractClick")}
            items={contract.needs_click}
          />
          <ContractList
            title={t("program.contractAsks")}
            items={contract.talks_to.map((x) => `${x.to}: ${x.for}`)}
          />
        </section>
      )}
      {frame && (
        <section className="card" data-testid="program-frame">
          <h2>{t("program.frame")}</h2>
          <p className="muted small">{t("program.frameHint")}</p>
          {frame.tone && (
            <p>
              <span className="fact-label">{t("program.tone")}</span>{" "}
              {frame.tone}
            </p>
          )}
          {frame.rules.length > 0 && (
            <ul>
              {frame.rules.map((r) => (
                <li key={r}>{r}</li>
              ))}
            </ul>
          )}
        </section>
      )}
      <section className="card">
        <h2>{t("program.tools")}</h2>
        <p className="muted small">{t("program.toolsHint")}</p>
        {failed && (
          <p className="bad" role="alert">
            {t("program.toolsFailed")}
          </p>
        )}
        {ceiling && (
          <ul data-testid="program-tools">
            {ceiling.groups === null ? (
              <li>{t("program.allTools")}</li>
            ) : (
              ceiling.groups.map((g) => (
                <li key={g}>
                  {t(`scope.name.${g}`, { defaultValue: g })}
                  {away(reachable.has(g))}
                </li>
              ))
            )}
            {ceiling.internet !== false && (
              <li>
                {t("program.web")}
                {away(Boolean(reach?.internet.node))}
              </li>
            )}
            {ceiling.groups?.length === 0 && ceiling.internet === false && (
              <li className="muted">{t("program.noTools")}</li>
            )}
          </ul>
        )}
        <p className="muted small">{t("program.handoverHint")}</p>
      </section>
      <section className="card">
        <h2>{t("program.notes")}</h2>
        <SensitiveReleases api={api} program={workspace} />
      </section>
    </section>
  );
}
