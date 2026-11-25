import { useEffect, useState } from "react";
import { useTranslation } from "react-i18next";
import type { ApiClient } from "../api/client";
import type { MemoryTagTree } from "../types";
import { tagLabel } from "./TagTree";

/** Which sensitive branches of the core this program may see (D74): a note about the person with a
 *  tag below `health` is hidden from every program until the person releases the branch for one. */
export function SensitiveReleases({
  api,
  program,
}: {
  api: ApiClient;
  program: string;
}) {
  const { t } = useTranslation();
  const [tree, setTree] = useState<MemoryTagTree | null>(null);
  const [failed, setFailed] = useState(false);

  useEffect(() => {
    api.memoryTags().then(setTree, () => setFailed(true));
  }, [api]);

  async function toggle(id: string, released: string[], on: boolean) {
    const next = on
      ? [...released, program]
      : released.filter((p) => p !== program);
    try {
      setTree(await api.updateMemoryTag(id, { released: next }));
    } catch {
      setFailed(true);
    }
  }

  if (failed)
    return (
      <p className="bad" role="alert">
        {t("program.releasesFailed")}
      </p>
    );
  if (!tree) return null;
  // the heads of the sensitive branches: what is sensitive below them is released with them
  const heads = tree.tags.filter((x) => x.sensitive === true);
  return (
    <>
      <p className="muted small">{t("program.releasesHint")}</p>
      {heads.length === 0 ? (
        <p className="muted">{t("program.noSensitive")}</p>
      ) : (
        <ul data-testid="program-releases">
          {heads.map((tag) => {
            const released = tag.effective.released;
            return (
              <li key={tag.id}>
                <label className="check">
                  <input
                    type="checkbox"
                    checked={released.includes(program)}
                    onChange={(e) =>
                      void toggle(tag.id, released, e.target.checked)
                    }
                  />{" "}
                  {t("program.release", { tag: tagLabel(tag, t) })}
                </label>
              </li>
            );
          })}
        </ul>
      )}
    </>
  );
}
