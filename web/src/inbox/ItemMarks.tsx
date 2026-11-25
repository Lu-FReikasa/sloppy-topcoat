import { useCallback, useEffect, useState } from "react";
import { useTranslation } from "react-i18next";
import type { ApiClient } from "../api/client";
import { formatDateTime } from "../format";
import { MARK_KINDS, type ItemMark, type MarkKind } from "../types";

interface Props {
  api: ApiClient;
  itemId: string;
  /** Bumped when the inbox changed (a re-check may have left a mark). */
  tick?: number;
  /** Show the marks only, without the form to add one (the board). */
  compact?: boolean;
}

/** Comments, Post-Its, stickers and highlights on an item (docs/PLATFORM.md, 3.7). A mark sits on
 *  the item; it never changes its text. The re-check leaves its own ("still correct", "outdated"),
 *  the person adds theirs and may resolve or remove any. */
export function ItemMarks({ api, itemId, tick = 0, compact = false }: Props) {
  const { t, i18n } = useTranslation();
  const [marks, setMarks] = useState<ItemMark[]>([]);
  const [kind, setKind] = useState<MarkKind>("postit");
  const [text, setText] = useState("");
  const [failed, setFailed] = useState(false);

  const load = useCallback(() => {
    api.itemMarks(itemId).then(
      (r) => {
        setMarks(r.marks);
        setFailed(false);
      },
      () => setFailed(true),
    );
  }, [api, itemId]);
  useEffect(load, [load, tick]);

  async function act(action: Promise<unknown>) {
    try {
      await action;
      load();
    } catch {
      setFailed(true);
    }
  }

  async function add() {
    const body = text.trim();
    if (!body && kind !== "highlight") return;
    setText("");
    await act(api.addItemMark(itemId, kind, body));
  }

  return (
    <div className="item-marks" data-testid="item-marks">
      {failed && (
        <p className="bad small" role="alert">
          {t("marks.failed")}
        </p>
      )}
      <ul className="marks-list">
        {marks.map((m) => (
          <li
            key={m.id}
            className={`mark mark-${m.kind}${m.resolved_at ? " mark-done" : ""}`}
            data-testid="mark"
          >
            <span className="mark-kind">{t(`marks.kind.${m.kind}`)}</span>{" "}
            <span className="mark-text">{m.text}</span>
            <span className="muted small">
              {" "}
              {m.author === "person"
                ? ""
                : `· ${t(`marks.author.${m.author}`, { defaultValue: m.author })} `}
              {m.created_at
                ? `· ${formatDateTime(m.created_at, i18n.language) ?? ""}`
                : ""}
            </span>
            {!compact && (
              <span className="actions">
                {m.kind === "postit" && (
                  <button
                    type="button"
                    onClick={() =>
                      void act(api.resolveItemMark(m.id, !m.resolved_at))
                    }
                  >
                    {m.resolved_at ? t("marks.reopen") : t("marks.done")}
                  </button>
                )}
                <button
                  type="button"
                  onClick={() => void act(api.deleteItemMark(m.id))}
                >
                  {t("marks.remove")}
                </button>
              </span>
            )}
          </li>
        ))}
      </ul>
      {!compact && (
        <div className="marks-add">
          <select
            value={kind}
            aria-label={t("marks.kindLabel")}
            onChange={(e) => setKind(e.target.value as MarkKind)}
          >
            {MARK_KINDS.map((k) => (
              <option key={k} value={k}>
                {t(`marks.kind.${k}`)}
              </option>
            ))}
          </select>
          <input
            type="text"
            value={text}
            maxLength={500}
            aria-label={t("marks.text")}
            placeholder={t("marks.text")}
            onChange={(e) => setText(e.target.value)}
            onKeyDown={(e) => e.key === "Enter" && void add()}
          />
          <button type="button" onClick={() => void add()}>
            {t("marks.add")}
          </button>
        </div>
      )}
    </div>
  );
}
