import { useId, useState, type FormEvent } from "react";
import { useTranslation } from "react-i18next";
import type { ApiClient } from "../api/client";
import type { MemoryItem, MemoryTag } from "../types";
import { PROGRAMS, PROGRAM_KINDS } from "./programs";

interface Props {
  api: ApiClient;
  item: MemoryItem;
  /** The person's tag tree: the tags offered while typing. */
  tags: MemoryTag[];
  act: <T>(fn: () => Promise<T>, done?: string) => Promise<void>;
}

/** Where a note stands (ARCHITECTURE D74): in the core (about the person, read by every program)
 *  or in one program's memory, and what it is about (its tags in the tag tree). A new tag is typed
 *  as `parent/name`. Changing it changes where the note is seen, not what it says. */
export function Placement({ api, item, tags, act }: Props) {
  const { t } = useTranslation();
  const [tag, setTag] = useState("");
  const list = useId();
  const current = item.tags ?? [];

  function add(e: FormEvent) {
    e.preventDefault();
    const next = tag.trim();
    if (!next) return;
    setTag("");
    void act(() => api.placeMemory(item.id, { tags: [...current, next] }));
  }

  return (
    <div className="memory-placement" data-testid="memory-placement">
      <label>
        <span className="fact-label">{t("memory.placement.label")}</span>{" "}
        <select
          value={item.application ?? ""}
          onChange={(e) =>
            void act(() =>
              api.placeMemory(item.id, { application: e.target.value || null }),
            )
          }
        >
          <option value="" disabled={PROGRAM_KINDS.includes(item.kind ?? "")}>
            {t("memory.placement.core")}
          </option>
          {PROGRAMS.map((key) => (
            <option key={key} value={key}>
              {t(`inbox.apps.${key}`)}
            </option>
          ))}
        </select>
      </label>
      <span className="memory-tags">
        {current.map((x) => (
          <span key={x} className="chip" data-testid="memory-tag">
            {x}{" "}
            <button
              type="button"
              className="linklike"
              aria-label={t("memory.placement.removeTag", { tag: x })}
              onClick={() =>
                void act(() =>
                  api.placeMemory(item.id, {
                    tags: current.filter((y) => y !== x),
                  }),
                )
              }
            >
              ×
            </button>
          </span>
        ))}
        <form onSubmit={add} className="memory-tag-form">
          <input
            type="text"
            list={list}
            aria-label={t("memory.placement.addTag")}
            placeholder={t("memory.placement.addTag")}
            value={tag}
            maxLength={61}
            onChange={(e) => setTag(e.target.value)}
          />
          <datalist id={list}>
            {tags.map((x) => (
              <option key={x.id} value={x.name} />
            ))}
          </datalist>
        </form>
      </span>
    </div>
  );
}
