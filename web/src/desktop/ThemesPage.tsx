import { useCallback, useEffect, useMemo, useState } from "react";
import { useTranslation } from "react-i18next";
import { UnauthorizedError, type ApiClient } from "../api/client";
import { describeError, toActionError } from "../errors";
import { requestOpen } from "../layout/intent";
import { previewTheme } from "./preview";
import {
  BAR_ITEMS,
  BAR_POSITIONS,
  BUILTIN_THEMES,
  CHROME_BUTTONS,
  LAYOUT_KINDS,
  MOTION_STYLES,
  isBuiltin,
  themeProblems,
  type BarItem,
  type ChromeButton,
  type ThemeManifest,
} from "./theme";
import { askConfirm } from "../components/askConfirm";

const NEW_THEME: ThemeManifest = {
  key: "my-theme",
  name: "My theme",
  base: "terminal",
  layout: { kind: "tile" },
  bar: { position: "bottom", items: ["start", "programs", "status", "me"] },
  chrome: { buttons: ["minimise", "maximise", "close"], titlebar: "top" },
  motion: { style: "none", ms: 0 },
  css: "",
};

/** Move an entry of a list by one place (the order of the taskbar parts and of the buttons). */
function moved<T>(list: T[], item: T, by: -1 | 1): T[] {
  const i = list.indexOf(item);
  const j = i + by;
  if (i < 0 || j < 0 || j >= list.length) return list;
  const next = [...list];
  [next[i], next[j]] = [next[j]!, next[i]!];
  return next;
}

/** The set of `all` that a list holds, the ones it holds first in their order, then the others:
 *  what a person edits when a theme says which parts it has and in which order. */
function arranged<T extends string>(all: readonly T[], held: T[]): T[] {
  return [...held, ...all.filter((x) => !held.includes(x))];
}

/** The themes of the desktop (docs/THEMES.md): the built-in ones to copy, the custom ones of this
 *  hub to change or delete, and an editor with a live preview: the desktop is drawn with the draft
 *  while it is typed, and only "Save" stores it. */
export function ThemesPage({ api }: { api: ApiClient }) {
  const { t, i18n } = useTranslation();
  const [custom, setCustom] = useState<ThemeManifest[]>([]);
  const [draft, setDraft] = useState<ThemeManifest | null>(null);
  const [isNew, setIsNew] = useState(false);
  const [live, setLive] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const fail = useCallback(
    (err: unknown) => {
      if (!(err instanceof UnauthorizedError)) setError(describeError(toActionError(err), t, i18n));
    },
    [t, i18n],
  );
  const load = useCallback(() => {
    api.uiThemes().then(setCustom, fail);
  }, [api, fail]);
  useEffect(load, [load]);

  const problems = useMemo(() => (draft ? themeProblems(draft) : []), [draft]);
  // the desktop shows the draft while it is valid; leaving the page ends the preview
  useEffect(() => {
    previewTheme(live && draft && problems.length === 0 ? draft : null);
  }, [draft, live, problems]);
  useEffect(() => () => previewTheme(null), []);

  function edit(theme: ThemeManifest, fresh: boolean) {
    setDraft({ ...theme, css: theme.css ?? "" });
    setIsNew(fresh);
    setNotice(null);
    setError(null);
  }
  const change = (patch: Partial<ThemeManifest>) => setDraft((d) => (d ? { ...d, ...patch } : d));

  async function save() {
    if (!draft) return;
    setBusy(true);
    setError(null);
    try {
      const doc: ThemeManifest = { ...draft };
      if (!doc.css) delete doc.css;
      await api.putTheme(doc.key, doc);
      setNotice(t("themes.saved", { name: doc.name }));
      setIsNew(false);
      load();
    } catch (err) {
      fail(err);
    } finally {
      setBusy(false);
    }
  }

  async function remove(key: string) {
    if (!await askConfirm(t("themes.confirmDelete", { key }))) return;
    setBusy(true);
    try {
      await api.deleteTheme(key);
      if (draft?.key === key) setDraft(null);
      load();
    } catch (err) {
      fail(err);
    } finally {
      setBusy(false);
    }
  }

  const copyOf = (theme: ThemeManifest): ThemeManifest => ({
    ...theme,
    key: `${theme.key}-copy`.slice(0, 40),
    name: t("themes.copyName", { name: theme.name }).slice(0, 60),
    base: theme.base ?? (isBuiltin(theme.key) ? (theme.key as ThemeManifest["base"]) : undefined),
  });

  return (
    <section className="themes" aria-label={t("themes.heading")}>
      <h1>{t("themes.heading")}</h1>
      <p className="muted">{t("themes.intro")}</p>
      {error && (
        <p className="error" role="alert">
          {error}
        </p>
      )}
      {notice && <p role="status">{notice}</p>}

      <h2>{t("themes.list")}</h2>
      <ul className="theme-list" data-testid="theme-list">
        {BUILTIN_THEMES.map((th) => (
          <li key={th.key}>
            <strong>{t(`desktop.looks.${th.key}`)}</strong> <span className="muted small">{t("themes.builtin")}</span>{" "}
            <button type="button" onClick={() => edit(copyOf(th), true)}>
              {t("themes.copy")}
            </button>
          </li>
        ))}
        {custom.map((th) => (
          <li key={th.key}>
            <strong>{th.name}</strong> <span className="muted small">{th.key}</span>{" "}
            <button type="button" onClick={() => edit(th, false)}>
              {t("themes.edit")}
            </button>{" "}
            <button
              type="button"
              onClick={() => requestOpen({ module: "page.theme-workshop", props: { key: th.key } })}
            >
              {t("themeWorkshop.open")}
            </button>{" "}
            <button type="button" onClick={() => edit(copyOf(th), true)}>
              {t("themes.copy")}
            </button>{" "}
            <button type="button" disabled={busy} onClick={() => void remove(th.key)}>
              {t("themes.delete")}
            </button>
          </li>
        ))}
      </ul>
      <p>
        <button type="button" onClick={() => edit(NEW_THEME, true)}>
          {t("themes.new")}
        </button>{" "}
        <button type="button" onClick={() => requestOpen({ module: "page.theme-workshop", props: {} })}>
          {t("themeWorkshop.newTheme")}
        </button>
      </p>

      {draft && (
        <form
          className="card theme-editor"
          data-testid="theme-editor"
          onSubmit={(e) => {
            e.preventDefault();
            void save();
          }}
        >
          <h2>{isNew ? t("themes.newHeading") : t("themes.editHeading", { name: draft.name })}</h2>
          <label>
            <span>{t("themes.key")}</span>
            <input
              value={draft.key}
              disabled={!isNew}
              onChange={(e) => change({ key: e.target.value })}
              aria-label={t("themes.key")}
            />
          </label>
          <label>
            <span>{t("themes.name")}</span>
            <input
              value={draft.name}
              onChange={(e) => change({ name: e.target.value })}
              aria-label={t("themes.name")}
            />
          </label>
          <label>
            <span>{t("themes.base")}</span>
            <select
              value={draft.base ?? ""}
              onChange={(e) => change({ base: (e.target.value || undefined) as ThemeManifest["base"] })}
              aria-label={t("themes.base")}
            >
              <option value="">{t("themes.baseNone")}</option>
              {BUILTIN_THEMES.map((b) => (
                <option key={b.key} value={b.key}>
                  {t(`desktop.looks.${b.key}`)}
                </option>
              ))}
            </select>
          </label>
          <label>
            <span>{t("themes.layout")}</span>
            <select
              value={draft.layout.kind}
              onChange={(e) => change({ layout: { kind: e.target.value as ThemeManifest["layout"]["kind"] } })}
              aria-label={t("themes.layout")}
            >
              {LAYOUT_KINDS.map((k) => (
                <option key={k} value={k}>
                  {t(`themes.layouts.${k}`)}
                </option>
              ))}
            </select>
          </label>
          <label>
            <span>{t("themes.barPosition")}</span>
            <select
              value={draft.bar.position}
              onChange={(e) =>
                change({ bar: { ...draft.bar, position: e.target.value as ThemeManifest["bar"]["position"] } })
              }
              aria-label={t("themes.barPosition")}
            >
              {BAR_POSITIONS.map((p) => (
                <option key={p} value={p}>
                  {t(`themes.positions.${p}`)}
                </option>
              ))}
            </select>
          </label>
          <fieldset>
            <legend>{t("themes.barItems")}</legend>
            {arranged<BarItem>(BAR_ITEMS, draft.bar.items).map((item) => {
              const on = draft.bar.items.includes(item);
              return (
                <div key={item} className="theme-part">
                  <label>
                    <input
                      type="checkbox"
                      checked={on}
                      onChange={() =>
                        change({
                          bar: {
                            ...draft.bar,
                            items: on ? draft.bar.items.filter((x) => x !== item) : [...draft.bar.items, item],
                          },
                        })
                      }
                    />{" "}
                    {t(`themes.items.${item}`)}
                  </label>
                  {on && (
                    <>
                      <button
                        type="button"
                        aria-label={t("themes.up", { part: t(`themes.items.${item}`) })}
                        onClick={() => change({ bar: { ...draft.bar, items: moved(draft.bar.items, item, -1) } })}
                      >
                        ↑
                      </button>
                      <button
                        type="button"
                        aria-label={t("themes.down", { part: t(`themes.items.${item}`) })}
                        onClick={() => change({ bar: { ...draft.bar, items: moved(draft.bar.items, item, 1) } })}
                      >
                        ↓
                      </button>
                    </>
                  )}
                </div>
              );
            })}
          </fieldset>
          <label>
            <span>{t("themes.titlebar")}</span>
            <select
              value={draft.chrome.titlebar}
              onChange={(e) => change({ chrome: { ...draft.chrome, titlebar: e.target.value as "top" | "hidden" } })}
              aria-label={t("themes.titlebar")}
            >
              <option value="top">{t("themes.titlebarTop")}</option>
              <option value="hidden">{t("themes.titlebarHidden")}</option>
            </select>
          </label>
          <fieldset>
            <legend>{t("themes.buttons")}</legend>
            {arranged<ChromeButton>(CHROME_BUTTONS, draft.chrome.buttons).map((b) => {
              const on = draft.chrome.buttons.includes(b);
              return (
                <div key={b} className="theme-part">
                  <label>
                    <input
                      type="checkbox"
                      checked={on}
                      onChange={() =>
                        change({
                          chrome: {
                            ...draft.chrome,
                            buttons: on ? draft.chrome.buttons.filter((x) => x !== b) : [...draft.chrome.buttons, b],
                          },
                        })
                      }
                    />{" "}
                    {t(`themes.buttonNames.${b}`)}
                  </label>
                  {on && (
                    <>
                      <button
                        type="button"
                        aria-label={t("themes.up", { part: t(`themes.buttonNames.${b}`) })}
                        onClick={() =>
                          change({ chrome: { ...draft.chrome, buttons: moved(draft.chrome.buttons, b, -1) } })
                        }
                      >
                        ↑
                      </button>
                      <button
                        type="button"
                        aria-label={t("themes.down", { part: t(`themes.buttonNames.${b}`) })}
                        onClick={() =>
                          change({ chrome: { ...draft.chrome, buttons: moved(draft.chrome.buttons, b, 1) } })
                        }
                      >
                        ↓
                      </button>
                    </>
                  )}
                </div>
              );
            })}
          </fieldset>
          <label>
            <span>{t("themes.motion")}</span>
            <select
              value={draft.motion.style}
              onChange={(e) =>
                change({ motion: { ...draft.motion, style: e.target.value as ThemeManifest["motion"]["style"] } })
              }
              aria-label={t("themes.motion")}
            >
              {MOTION_STYLES.map((m) => (
                <option key={m} value={m}>
                  {t(`themes.motions.${m}`)}
                </option>
              ))}
            </select>
          </label>
          {(["close", "overlay"] as const).map((part) => {
            const label = t(part === "close" ? "themes.motionClose" : "themes.motionOverlay");
            return (
              <label key={part}>
                <span>{label}</span>
                <select
                  value={draft.motion[part] ?? "none"}
                  onChange={(e) =>
                    change({ motion: { ...draft.motion, [part]: e.target.value as ThemeManifest["motion"]["style"] } })
                  }
                  aria-label={label}
                >
                  {MOTION_STYLES.map((m) => (
                    <option key={m} value={m}>
                      {t(`themes.motions.${m}`)}
                    </option>
                  ))}
                </select>
              </label>
            );
          })}
          <label>
            <span>{t("themes.motionMs")}</span>
            <input
              type="number"
              min={0}
              max={2000}
              step={20}
              value={draft.motion.ms}
              onChange={(e) => change({ motion: { ...draft.motion, ms: Number(e.target.value) } })}
              aria-label={t("themes.motionMs")}
            />
          </label>
          <label>
            <span>{t("themes.css")}</span>
            <textarea
              rows={8}
              spellCheck={false}
              value={draft.css ?? ""}
              onChange={(e) => change({ css: e.target.value })}
              aria-label={t("themes.css")}
            />
          </label>
          <p className="muted small">{t("themes.cssHint")}</p>
          {problems.length > 0 && (
            <ul className="error" role="alert" data-testid="theme-problems">
              {problems.map((p) => (
                <li key={p}>{p}</li>
              ))}
            </ul>
          )}
          <label className="check">
            <input type="checkbox" checked={live} onChange={(e) => setLive(e.target.checked)} /> {t("themes.live")}
          </label>
          <div className="row-actions">
            <button type="submit" className="primary" disabled={busy || problems.length > 0}>
              {t("themes.save")}
            </button>
            <button type="button" onClick={() => setDraft(null)}>
              {t("themes.close")}
            </button>
          </div>
        </form>
      )}
    </section>
  );
}
