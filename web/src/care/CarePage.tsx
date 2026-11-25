import { useTranslation } from "react-i18next";
import type { ApiClient } from "../api/client";
import { InstructionsPage } from "../instructions/InstructionsPage";
import { KnowledgePage } from "../knowledge/KnowledgePage";
import { MemoryPage } from "../memory/MemoryPage";
import type { Me } from "../types";

/** The technical side of memory and instructions (Administration › Maintenance): the care and
 *  review runs, the token budget and the fixed rules. The knowledge sources are a view of their own
 *  since phase 4 (`SourcesPage`). Each part is the page of the user area in its "care" form, so
 *  there is one implementation. */
export function CarePage({ api }: { api: ApiClient }) {
  const { t } = useTranslation();
  return (
    <section className="care">
      <h1>{t("care.heading")}</h1>
      <p>{t("care.intro")}</p>
      <MemoryPage api={api} mode="care" />
      <InstructionsPage api={api} mode="care" />
    </section>
  );
}

/** Administration › Knowledge: the sources the assistants search (folders on tool nodes, uploaded
 *  notes) and their exclusion rules. Research › Knowledge shows them, read only. */
export function SourcesPage({ api, me }: { api: ApiClient; me: Me | null }) {
  return <KnowledgePage api={api} me={me} mode="care" />;
}
