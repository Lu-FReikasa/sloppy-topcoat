import { useState, type ReactNode } from "react";
import { useTranslation } from "react-i18next";
import { moduleMeta } from "./meta";

export interface TabSpec {
  key: string;
  /** The module whose page this tab is (its title is the tab's label, and its right decides
   *  whether the tab is offered). */
  module: string;
  /** A label of its own, for a tab that shows part of a module's page. */
  labelKey?: string;
  render: () => ReactNode;
}

/** One view of a program made of a few pages that belong together (docs/DESKTOP.md): the machines
 *  and what runs on them, the models and who does what, the rights and the API keys. The tabs that
 *  are offered are the ones the person may use; `initial` is the tab an address or link asks for. */
export function Tabbed({ tabs, initial }: { tabs: TabSpec[]; initial?: string }) {
  const { t } = useTranslation();
  const [current, setCurrent] = useState(
    tabs.some((x) => x.key === initial) ? (initial as string) : (tabs[0]?.key ?? ""),
  );
  const shown = tabs.find((x) => x.key === current) ?? tabs[0];
  if (!shown) return null;
  const label = (module: string) => {
    const meta = moduleMeta(module);
    return meta ? t(meta.titleKey) : module;
  };
  return (
    <section className="tabbed">
      {tabs.length > 1 && (
        <div className="inbox-tabs" role="tablist" aria-label={label(tabs[0]?.module ?? "")}>
          {tabs.map((x) => (
            <button
              key={x.key}
              type="button"
              role="tab"
              aria-selected={x.key === shown.key}
              className={x.key === shown.key ? "primary" : undefined}
              onClick={() => setCurrent(x.key)}
            >
              {x.labelKey ? t(x.labelKey) : label(x.module)}
            </button>
          ))}
        </div>
      )}
      {shown.render()}
    </section>
  );
}
