import { useEffect, useState, type KeyboardEvent as ReactKeyboardEvent } from "react";
import { useTranslation } from "react-i18next";
import { setTileNew, tileNew } from "./dwindle";
import { accelerator, clientCommand, clientConfig, keyLabel, onClientResult, type ClientConfig, type ClientResult } from "./client";

/** Which key is recorded: the overlay's on the screen of the mouse, on every screen, Clipper's. */
type Slot = "overlay" | "all" | "clipper";

/** The desktop client's own settings (its keys and its backdrop), shown only inside the
 *  client: they live in the client's client.json on this machine, not on the hub. */
export function ClientSettings() {
  const { t } = useTranslation();
  const [config, setConfig] = useState<ClientConfig | null>(() => clientConfig());
  // which key is being recorded: the overlay's or Clipper's
  const [recording, setRecording] = useState<Slot | null>(null);
  const [answer, setAnswer] = useState<ClientResult | null>(null);
  const [tileOn, setTileOn] = useState(tileNew);

  useEffect(
    () =>
      onClientResult((r) => {
        setAnswer(r);
        setConfig(clientConfig());
      }),
    [],
  );

  if (!config) return null;

  const record = (slot: Slot) => (e: ReactKeyboardEvent<HTMLButtonElement>) => {
    if (recording !== slot) return;
    e.preventDefault();
    e.stopPropagation();
    if (e.key === "Escape") {
      setRecording(null);
      return;
    }
    const acc = accelerator(e.nativeEvent);
    if (!acc) return; // a modifier alone: wait for the key
    setRecording(null);
    setAnswer(null);
    clientCommand("set-key", { slot, key: acc });
  };

  const keyRow = (slot: Slot, label: string, value: string) => (
    <div className="client-key" data-setting={slot === "overlay" ? "client-keys" : undefined}>
      <span>{label}</span>
      <kbd>{keyLabel(value, t)}</kbd>
      <button
        type="button"
        onClick={() => {
          setAnswer(null);
          setRecording((r) => (r === slot ? null : slot));
        }}
        onKeyDown={record(slot)}
        onBlur={() => setRecording(null)}
        aria-pressed={recording === slot}
        // Esc while recording ends the recording, not the overlay (client.ts)
        data-esc-first={recording === slot ? "" : undefined}
      >
        {recording === slot ? t("client.settings.press") : t("client.settings.change")}
      </button>
    </div>
  );

  return (
    <section className="card" data-testid="client-settings">
      <h2>{t("client.settings.heading")}</h2>
      <p className="muted small">{t("client.settings.hint")}</p>
      {keyRow("overlay", t("client.settings.key"), config.key)}
      {keyRow("all", t("client.settings.allKey"), config.allKey ?? "ctrl+alt+KeyF")}
      {keyRow("clipper", t("client.settings.clipperKey"), config.clipperKey ?? "ctrl+alt+KeyC")}
      {answer?.what === "key" && (
        <p className={answer.ok ? "ok small" : "bad small"} role="status">
          {answer.ok ? t("client.settings.keySaved") : t("client.settings.keyTaken", { error: answer.error ?? "" })}
        </p>
      )}
      <label data-setting="client-look">
        {t("client.settings.effect")}
        <select value={config.effect} onChange={(e) => clientCommand("set-look", { effect: e.target.value })}>
          <option value="blur">{t("client.settings.effectBlur")}</option>
          <option value="none">{t("client.settings.effectNone")}</option>
        </select>
      </label>
      <p className="muted small">{t("client.settings.lookHint")}</p>
      {answer?.what === "look" && !answer.ok && (
        <p className="bad small" role="alert">
          {answer.error}
        </p>
      )}
      <label data-setting="client-tile">
        <input
          type="checkbox"
          checked={tileOn}
          onChange={(e) => {
            setTileNew(e.target.checked);
            setTileOn(e.target.checked);
          }}
        />
        {t("client.settings.tileNew")}
      </label>
      <p className="muted small">{t("client.settings.tileHint")}</p>
      <p className="muted small">{t("client.settings.meter")}</p>
    </section>
  );
}
