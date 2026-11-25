import { useState } from "react";
import { useTranslation } from "react-i18next";
import type { ApiClient } from "../api/client";
import { bytesToGb, formatDecimal } from "../format";
import { modelsHref } from "../route";
import type { NodeSummary } from "../types";
import { buildCatalogue } from "./ModelCatalogue";
import { ModelNodes } from "./ModelNodes";
import { TransferList } from "./TransferList";

interface Props {
  api: ApiClient;
  /** The model name from the route. */
  name: string;
  nodes: NodeSummary[];
  loaded: boolean;
  error: string | null;
  /** Called after a successful action (the live stream also updates the list). */
  onChanged?: () => void;
}

/** One model across all nodes: where it is stored and loaded, and what can be done with it. */
export function ModelDetail({ api, name, nodes, loaded, error, onChanged }: Props) {
  const { t, i18n } = useTranslation();
  const locale = i18n.language;
  const [refreshKey, setRefreshKey] = useState(0);

  const entry = buildCatalogue(nodes).find((e) => e.name === name);

  const back = (
    <p>
      <a href={modelsHref}>{t("model.back")}</a>
    </p>
  );

  if (error) {
    return (
      <section>
        {back}
        <p className="error" role="alert">
          {t("overview.loadError", { message: error })}
        </p>
      </section>
    );
  }
  if (!loaded) {
    return (
      <section>
        {back}
        <p className="muted">{t("app.loading")}</p>
      </section>
    );
  }
  if (!entry) {
    return (
      <section>
        {back}
        <h1>{name}</h1>
        <p className="empty" data-testid="model-not-found">
          {t("model.notFound", { model: name })} {t("model.notFoundHint")}
        </p>
      </section>
    );
  }

  return (
    <section>
      {back}
      <h1>{name}</h1>
      {entry.sizeBytes !== undefined && (
        <p className="muted" data-testid="model-size">
          {t("model.size", {
            value: t("detail.sizeGb", { value: formatDecimal(bytesToGb(entry.sizeBytes), locale) }),
          })}
        </p>
      )}
      <ModelNodes
        api={api}
        name={name}
        nodes={nodes}
        onChanged={onChanged}
        onTransferCreated={() => setRefreshKey((k) => k + 1)}
      />
      <TransferList api={api} model={name} events={[]} refreshKey={refreshKey} />
    </section>
  );
}
