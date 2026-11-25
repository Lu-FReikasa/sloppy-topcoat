import { useState } from "react";
import { useTranslation } from "react-i18next";
import type { ApiClient } from "../api/client";
import { formatDateTime } from "../format";
import type { CoreItem, MemoryVersion } from "../types";

type Mode = "personal" | "household";

interface Props {
  api: ApiClient;
  title: string;
  intro?: string;
  items: CoreItem[];
  mode: Mode;
  /** May the signed-in user change these (their own, or an admin the household). */
  editable: boolean;
  /** Entries a style or a rule can be added to (the personal instructions). */
  canAdd?: boolean;
  /** Facts offer a pin switch instead of an edit form (they are changed on the memory page). */
  factsOnly?: boolean;
  onChanged: () => void;
  onError: (err: unknown) => void;
}

/** The entries of one slice of the core: text, example, in-core flag, edit, pin, history with undo. */
export function CoreEntries({
  api,
  title,
  intro,
  items,
  mode,
  editable,
  canAdd,
  factsOnly,
  onChanged,
  onError,
}: Props) {
  const { t } = useTranslation();
  const [kind, setKind] = useState<"rule" | "style">("rule");
  const [text, setText] = useState("");
  const [example, setExample] = useState("");

  async function add() {
    try {
      if (mode === "household") await api.addHouseholdItem({ text: text.trim() });
      else
        await api.addCoreItem({
          kind,
          text: text.trim(),
          ...(kind === "style" && example.trim() ? { example: example.trim() } : {}),
        });
      setText("");
      setExample("");
      onChanged();
    } catch (err) {
      onError(err);
    }
  }

  return (
    <section className="core-slice" aria-label={title}>
      <h2>{title}</h2>
      {intro && <p className="muted small">{intro}</p>}
      {items.length === 0 && <p className="muted small">{t("instructions.empty")}</p>}
      <ul className="core-items">
        {items.map((i) => (
          <Entry
            key={i.id}
            api={api}
            item={i}
            mode={mode}
            editable={editable}
            factsOnly={factsOnly}
            onChanged={onChanged}
            onError={onError}
          />
        ))}
      </ul>
      {editable && canAdd && (
        <form
          className="core-add stack"
          onSubmit={(e) => {
            e.preventDefault();
            if (text.trim()) void add();
          }}
        >
          {mode === "personal" && (
            <label className="picker">
              <span className="fact-label">{t("instructions.kindLabel")}</span>
              <select value={kind} onChange={(e) => setKind(e.target.value as "rule" | "style")}>
                <option value="rule">{t("instructions.kind.rule")}</option>
                <option value="style">{t("instructions.kind.style")}</option>
              </select>
            </label>
          )}
          <label>
            <span className="fact-label">{t("instructions.text")}</span>
            <textarea
              value={text}
              rows={2}
              maxLength={1000}
              onChange={(e) => setText(e.target.value)}
              aria-label={t("instructions.addText", { slice: title })}
            />
          </label>
          {mode === "personal" && kind === "style" && (
            <label>
              <span className="fact-label">{t("instructions.example")}</span>
              <textarea
                value={example}
                rows={2}
                maxLength={480}
                placeholder={t("instructions.examplePlaceholder")}
                onChange={(e) => setExample(e.target.value)}
                aria-label={t("instructions.example")}
              />
            </label>
          )}
          <div className="row-actions">
            <button type="submit" className="primary" disabled={!text.trim()}>
              {t("instructions.add")}
            </button>
          </div>
        </form>
      )}
    </section>
  );
}

function Entry({
  api,
  item: i,
  mode,
  editable,
  factsOnly,
  onChanged,
  onError,
}: {
  api: ApiClient;
  item: CoreItem;
  mode: Mode;
  editable: boolean;
  factsOnly?: boolean;
  onChanged: () => void;
  onError: (err: unknown) => void;
}) {
  const { t, i18n } = useTranslation();
  const [editing, setEditing] = useState(false);
  const [text, setText] = useState(i.text);
  const [example, setExample] = useState(i.example ?? "");
  const [versions, setVersions] = useState<MemoryVersion[] | null>(null);
  const [removed, setRemoved] = useState(false);
  const isStyle = i.kind === "style";
  const isInstruction = i.kind === "style" || i.kind === "rule";

  async function run(fn: () => Promise<unknown>) {
    try {
      await fn();
      onChanged();
    } catch (err) {
      onError(err);
    }
  }

  async function save() {
    await run(async () => {
      if (mode === "household") await api.editHouseholdItem(i.id, { text: text.trim() });
      else
        await api.editCoreItem(i.id, {
          text: text.trim(),
          ...(isStyle ? { example: example.trim() || null } : {}),
        });
      setEditing(false);
    });
  }

  async function history() {
    if (versions) {
      setVersions(null);
      return;
    }
    try {
      setVersions((await api.memoryItem(i.id)).versions);
    } catch (err) {
      onError(err);
    }
  }

  if (removed) {
    return (
      <li className="core-item removed" data-testid="core-item-removed">
        <span className="muted">{t("instructions.removed")}</span>{" "}
        <button
          type="button"
          onClick={() =>
            void run(async () => {
              await api.restoreMemory(i.id);
              setRemoved(false);
            })
          }
        >
          {t("instructions.undo")}
        </button>
      </li>
    );
  }

  return (
    <li className="core-item" data-testid="core-item">
      {editing ? (
        <div className="stack">
          <textarea
            value={text}
            rows={3}
            maxLength={1000}
            aria-label={t("instructions.text")}
            onChange={(e) => setText(e.target.value)}
          />
          {isStyle && (
            <textarea
              value={example}
              rows={2}
              maxLength={480}
              aria-label={t("instructions.example")}
              placeholder={t("instructions.examplePlaceholder")}
              onChange={(e) => setExample(e.target.value)}
            />
          )}
          <div className="row-actions">
            <button
              type="button"
              className="primary"
              disabled={!text.trim()}
              onClick={() => void save()}
            >
              {t("instructions.save")}
            </button>
            <button type="button" onClick={() => setEditing(false)}>
              {t("instructions.cancel")}
            </button>
          </div>
        </div>
      ) : (
        <>
          <p className="core-text">
            {isInstruction && <span className="tag">{t(`instructions.kind.${i.kind}`)}</span>}{" "}
            {i.text}
          </p>
          {isStyle && i.example && (
            <p className="small" data-testid="item-example">
              {t("instructions.example")}: <em>“{i.example}”</em>
            </p>
          )}
          <p className="muted small core-flags">
            {i.core && <span data-testid="flag-pinned">{t("instructions.pinned")}</span>}
            {i.weak && <span data-testid="flag-weak"> · {t("instructions.weak")}</span>}
            {i.source_type === "agent" && (
              <span data-testid="flag-learned"> · {t("instructions.learnedByAgent")}</span>
            )}
            {i.in_core === false && (
              <span data-testid="flag-cut"> · {t("instructions.notInCore")}</span>
            )}
            {i.in_core === true && !isInstruction && (
              <span data-testid="flag-in-core"> · {t("instructions.inCore")}</span>
            )}
            {i.version > 1 && ` · ${t("memory.version", { value: i.version })}`}
          </p>
          {editable && (
            <div className="row-actions">
              {!factsOnly && (
                <button type="button" onClick={() => setEditing(true)}>
                  {t("instructions.edit")}
                </button>
              )}
              {mode === "personal" && (
                <label className="check pin-check">
                  <input
                    type="checkbox"
                    checked={i.core === true}
                    onChange={(e) =>
                      void run(() => api.editCoreItem(i.id, { core: e.target.checked }))
                    }
                  />
                  {t("instructions.pin")}
                </label>
              )}
              <button type="button" onClick={() => void history()}>
                {versions ? t("instructions.hideHistory") : t("instructions.history")}
              </button>
              {!factsOnly && (
                <button
                  type="button"
                  className="danger"
                  onClick={() =>
                    void run(async () => {
                      if (mode === "household") await api.deleteHouseholdItem(i.id);
                      else await api.deleteCoreItem(i.id);
                      setRemoved(true);
                    })
                  }
                >
                  {t("instructions.remove")}
                </button>
              )}
            </div>
          )}
          {!editable && (
            <div className="row-actions">
              <button type="button" onClick={() => void history()}>
                {versions ? t("instructions.hideHistory") : t("instructions.history")}
              </button>
            </div>
          )}
        </>
      )}
      {versions && (
        <ol className="history small" data-testid="item-history">
          {versions.map((v) => (
            <li key={v.version}>
              <span className="muted">
                {t("memory.version", { value: v.version })} ·{" "}
                {formatDateTime(v.created_at, i18n.language)} · {v.actor}
              </span>
              <br />
              {v.text}
              <br />
              <span className="muted">{v.reason}</span>
              {editable && mode === "personal" && v.version !== i.version && (
                <>
                  {" "}
                  <button
                    type="button"
                    onClick={() => void run(() => api.revertMemory(i.id, v.version))}
                  >
                    {t("instructions.revert", { version: v.version })}
                  </button>
                </>
              )}
            </li>
          ))}
        </ol>
      )}
    </li>
  );
}
