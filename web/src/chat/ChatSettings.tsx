import { ChatListSwitch } from "./ChatListSwitch";
import { ClientSettings } from "../desktop/ClientSettings";
import {
  useCallback,
  useEffect,
  useState,
  type FormEvent,
  type ReactNode,
} from "react";
import { useTranslation } from "react-i18next";
import { UnauthorizedError, type ApiClient } from "../api/client";
import { describeError, toActionError } from "../errors";
import { memoryHref } from "../route";
import type { ChatSettings as Settings, Quality } from "../types";

interface Props {
  api: ApiClient;
  /** One part only, for a section of Settings: the chat list (General), the answers (Assistant),
   *  the memory switch (Memory). Without it the whole page as before. */
  part?: "chatlist" | "assistant" | "memory";
}

type Personal = Settings["user"];

/** The whole personal record the hub stores; a section changes only its own fields, the others
 *  stay as they were *saved* (an unsaved edit in another section is never persisted by accident). */
function personalBody(saved: Personal, changes: Partial<Personal>): Personal {
  // persona_id is read-only here (the personas page changes it): the hub refuses it in this body
  const { persona_id: _persona, ...rest } = { ...saved, ...changes };
  return rest;
}

interface SectionProps {
  title: string;
  scope: "personal" | "hub";
  hint?: ReactNode;
  dirty: boolean;
  valid?: boolean;
  justSaved: boolean;
  error: string | null;
  onSave: () => void;
  children: ReactNode;
  testId: string;
  /** The `data-setting` anchor of the block (Settings jumps there). */
  anchor?: string;
}

/** One block of the settings page: its own heading and scope, its own Save button directly
 *  below the fields, and its own "unsaved changes" / "saved" state. */
function SettingsSection(p: SectionProps) {
  const { t } = useTranslation();
  const submit = (e: FormEvent) => {
    e.preventDefault();
    if (p.dirty && p.valid !== false) p.onSave();
  };
  return (
    <form
      className="form settings-section card"
      onSubmit={submit}
      data-testid={p.testId}
      data-setting={p.anchor}
    >
      <header className="settings-section-head">
        <h2>{p.title}</h2>
        <span className={`scope-badge scope-${p.scope}`}>
          {p.scope === "personal"
            ? t("settings.scopePersonal")
            : t("settings.scopeHub")}
        </span>
      </header>
      {p.hint && <p className="muted small">{p.hint}</p>}
      {p.error && (
        <p className="error" role="alert">
          {p.error}
        </p>
      )}
      {p.children}
      <div className="settings-save">
        <button
          type="submit"
          className="primary"
          disabled={!p.dirty || p.valid === false}
        >
          {t("settings.save")}
        </button>
        {p.dirty ? (
          <span className="unsaved small" role="status">
            {t("settings.unsaved")}
          </span>
        ) : p.justSaved ? (
          <span className="saved small" role="status">
            {t("settings.saved")}
          </span>
        ) : null}
      </div>
    </form>
  );
}

/** The person's own settings (Settings › Me, docs/DESKTOP.md): the chat (model, form of address,
 *  system prompt, temperature, quality, internet), the memory switch and the external model, each
 *  section with its own Save. The hub's models are the admin's, in Administration › System. */
export function ChatSettings({ api, part }: Props) {
  const { t, i18n } = useTranslation();
  const [settings, setSettings] = useState<Settings | null>(null);
  const [error, setError] = useState<string | null>(null);

  const fail = useCallback(
    (err: unknown) => {
      if (!(err instanceof UnauthorizedError))
        setError(describeError(toActionError(err), t, i18n));
    },
    [t, i18n],
  );

  useEffect(() => {
    api.chatSettings().then(setSettings, fail);
  }, [api, fail]);

  if (part === "chatlist") return <ChatListCard />;
  if (!settings) {
    return error ? (
      <p className="error" role="alert">
        {error}
      </p>
    ) : (
      <p className="muted">{t("app.loading")}</p>
    );
  }

  if (part === "assistant") return <ChatSection api={api} settings={settings} onSaved={setSettings} />;
  if (part === "memory") return <MemorySection api={api} settings={settings} onSaved={setSettings} />;
  return (
    <section>
      <h1>{t("settings.heading")}</h1>
      <ClientSettings />
      <ChatListCard />
      <ChatSection api={api} settings={settings} onSaved={setSettings} />
      <MemorySection api={api} settings={settings} onSaved={setSettings} />
    </section>
  );
}

function ChatListCard() {
  const { t } = useTranslation();
  return (
    <section className="card" data-setting="chatlist">
      <h2>{t("chatlist.heading")}</h2>
      <p className="muted small">{t("chatlist.hint")}</p>
      <ChatListSwitch />
    </section>
  );
}

interface SectionState {
  api: ApiClient;
  settings: Settings;
  onSaved: (s: Settings) => void;
}

function useSave(onSaved: (s: Settings) => void) {
  const { t, i18n } = useTranslation();
  const [error, setError] = useState<string | null>(null);
  const [justSaved, setJustSaved] = useState(false);
  const run = async (fn: () => Promise<Settings>) => {
    setError(null);
    try {
      onSaved(await fn());
      setJustSaved(true);
    } catch (err) {
      if (!(err instanceof UnauthorizedError))
        setError(describeError(toActionError(err), t, i18n));
    }
  };
  return { error, justSaved, run, edited: () => setJustSaved(false) };
}

function ChatSection({ api, settings, onSaved }: SectionState) {
  const { t } = useTranslation();
  const saved = settings.user;
  const [address, setAddress] = useState<"du" | "sie">(saved.address);
  const [quality, setQuality] = useState<Quality>(saved.quality ?? "balanced");
  const [internet, setInternet] = useState(saved.internet ?? true);
  const [prompt, setPrompt] = useState(saved.system_prompt ?? "");
  const [temperature, setTemperature] = useState(
    saved.temperature === null ? "" : String(saved.temperature),
  );
  const { error, justSaved, run, edited } = useSave(onSaved);

  const temperatureValue =
    temperature.trim() === "" ? null : Number(temperature.replace(",", "."));
  const valid =
    temperatureValue === null ||
    (Number.isFinite(temperatureValue) &&
      temperatureValue >= 0 &&
      temperatureValue <= 2);
  const dirty =
    address !== saved.address ||
    quality !== (saved.quality ?? "balanced") ||
    internet !== (saved.internet ?? true) ||
    (prompt.trim() || null) !== saved.system_prompt ||
    temperatureValue !== saved.temperature;
  const change =
    <T,>(set: (v: T) => void) =>
    (v: T) => {
      edited();
      set(v);
    };

  return (
    <SettingsSection
      testId="settings-chat"
      title={t("settings.chatSection")}
      scope="personal"
      hint={t("settings.chatHint")}
      dirty={dirty}
      valid={valid}
      justSaved={justSaved}
      error={error}
      onSave={() =>
        void run(() =>
          api.putChatSettings(
            personalBody(saved, {
              address,
              quality,
              internet,
              system_prompt: prompt.trim() || null,
              temperature: temperatureValue,
            }),
          ),
        )
      }
    >
      <label data-setting="assistant-address">
        <span>{t("settings.address")}</span>
        <select
          value={address}
          onChange={(e) => change(setAddress)(e.target.value as "du" | "sie")}
        >
          <option value="du">{t("settings.addressDu")}</option>
          <option value="sie">{t("settings.addressSie")}</option>
        </select>
      </label>
      <p className="muted small">{t("settings.addressHint")}</p>
      <label data-setting="assistant-quality">
        <span>{t("quality.defaultLabel")}</span>
        <select
          value={quality}
          onChange={(e) => change(setQuality)(e.target.value as Quality)}
        >
          {(["fast", "balanced", "thorough"] as const).map((q) => (
            <option key={q} value={q}>
              {t(`quality.${q}`)}
            </option>
          ))}
        </select>
      </label>
      <p className="muted small">{t("quality.defaultHint")}</p>
      <label className="check" data-setting="assistant-internet">
        <input
          type="checkbox"
          checked={internet}
          onChange={(e) => change(setInternet)(e.target.checked)}
        />
        <span>{t("internet.defaultLabel")}</span>
      </label>
      <p className="muted small">{t("internet.defaultHint")}</p>
      {/* rarely needed: closed unless something is set there (decided 2026-11-12) */}
      <details className="settings-advanced" data-setting="assistant-advanced" open={Boolean(saved.system_prompt) || typeof saved.temperature === "number" || !valid}>
        <summary>{t("settings.advanced")}</summary>
        <p className="muted small">{t("settings.advancedHint")}</p>
        <label>
          <span>{t("settings.systemPrompt")}</span>
          <textarea
            rows={6}
            maxLength={4000}
            value={prompt}
            onChange={(e) => change(setPrompt)(e.target.value)}
          />
        </label>
        <p className="muted small">{t("settings.systemPromptHint")}</p>
        <label>
          <span>{t("settings.temperature")}</span>
          <input
            inputMode="decimal"
            value={temperature}
            placeholder={t("settings.temperatureDefault")}
            aria-invalid={!valid}
            onChange={(e) => change(setTemperature)(e.target.value)}
          />
        </label>
        {!valid && (
          <p className="error small" role="alert">
            {t("settings.temperatureInvalid")}
          </p>
        )}
      </details>
    </SettingsSection>
  );
}

function MemorySection({ api, settings, onSaved }: SectionState) {
  const { t } = useTranslation();
  const saved = settings.user;
  const [enabled, setEnabled] = useState(saved.memory_enabled);
  const { error, justSaved, run, edited } = useSave(onSaved);
  return (
    <SettingsSection
      testId="settings-memory"
      anchor="memory-switch"
      title={t("settings.memorySection")}
      scope="personal"
      hint={
        <>
          {t("settings.memoryHint")}{" "}
          <a href={memoryHref}>{t("settings.memoryLink")}</a>
        </>
      }
      dirty={enabled !== saved.memory_enabled}
      justSaved={justSaved}
      error={error}
      onSave={() =>
        void run(() =>
          api.putChatSettings(personalBody(saved, { memory_enabled: enabled })),
        )
      }
    >
      <label className="check">
        <input
          type="checkbox"
          checked={enabled}
          onChange={(e) => {
            edited();
            setEnabled(e.target.checked);
          }}
        />
        <span>{t("settings.memory")}</span>
      </label>
    </SettingsSection>
  );
}
