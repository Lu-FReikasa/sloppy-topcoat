import { useCallback, useEffect, useState } from "react";
import { useTranslation } from "react-i18next";
import type { ApiClient } from "../api/client";
import { APPLICATIONS } from "../inbox/AppTag";
import type { MemorySortSuggestion } from "../types";

interface Props {
  api: ApiClient;
  /** Bumped when notes changed: the suggestions are looked at again. */
  tick: number;
  onDone: () => void;
}

/** Notes learned before applications existed that look as if they belong to Development or
 *  Administration: a suggestion from their words, which the person accepts (or changes) in one go.
 *  Nothing is moved until they say so. */
export function SortBanner({ api, tick, onDone }: Props) {
  const { t } = useTranslation();
  const [open, setOpen] = useState(false);
  const [hidden, setHidden] = useState(false);
  const [list, setList] = useState<MemorySortSuggestion[]>([]);
  const [choice, setChoice] = useState<Record<string, string | null>>({});
  const [failed, setFailed] = useState(false);

  const load = useCallback(() => {
    api.memorySort().then(
      (r) => {
        setList(r.suggestions);
        setChoice(
          Object.fromEntries(r.suggestions.map((s) => [s.id, s.suggested])),
        );
      },
      () => undefined,
    );
  }, [api]);
  useEffect(load, [load, tick]);

  if (hidden || list.length === 0) return null;
  const accepted = list.filter((s) => choice[s.id]);

  async function apply() {
    try {
      await api.applyMemorySort(
        accepted.map((s) => ({ id: s.id, application: choice[s.id] ?? null })),
      );
      setFailed(false);
      onDone();
      load();
    } catch {
      setFailed(true);
    }
  }

  return (
    <section className="notice memory-sort" data-testid="memory-sort">
      <p>
        {t("memory.sort.intro", { count: list.length })}{" "}
        <button
          type="button"
          className="linklike"
          onClick={() => setOpen((o) => !o)}
        >
          {open ? t("memory.sort.hide") : t("memory.sort.show")}
        </button>
      </p>
      {open && (
        <ul className="memory-sort-list">
          {list.map((s) => (
            <li key={s.id}>
              <select
                aria-label={t("memory.placement.label")}
                value={choice[s.id] ?? ""}
                onChange={(e) =>
                  setChoice({ ...choice, [s.id]: e.target.value || null })
                }
              >
                <option value="">{t("memory.sort.keep")}</option>
                {APPLICATIONS.map((key) => (
                  <option key={key} value={key}>
                    {t(`inbox.apps.${key}`)}
                  </option>
                ))}
              </select>{" "}
              {s.text}
            </li>
          ))}
        </ul>
      )}
      {failed && (
        <p className="bad" role="alert">
          {t("memory.sort.failed")}
        </p>
      )}
      <div className="actions">
        <button
          type="button"
          className="primary"
          disabled={accepted.length === 0}
          onClick={() => void apply()}
        >
          {t("memory.sort.apply", { count: accepted.length })}
        </button>
        <button type="button" onClick={() => setHidden(true)}>
          {t("memory.sort.later")}
        </button>
      </div>
    </section>
  );
}
