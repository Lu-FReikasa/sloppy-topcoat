import { useEffect, useState } from "react";
import { useTranslation } from "react-i18next";
import { UnauthorizedError, type ApiClient } from "../api/client";
import { describeError, toActionError } from "../errors";
import type { AgentSettings } from "../types";

interface Props {
  api: ApiClient;
}

/** How the agent talks to each model (admin): native tool calls, the JSON protocol, or auto. */
export function ToolModes({ api }: Props) {
  const { t, i18n } = useTranslation();
  const [settings, setSettings] = useState<AgentSettings | null>(null);
  const [modes, setModes] = useState<Record<string, string>>({});
  const [model, setModel] = useState("");
  const [mode, setMode] = useState("native");
  const [error, setError] = useState<string | null>(null);
  const [saved, setSaved] = useState(false);

  useEffect(() => {
    api.agentSettings().then(
      (s) => {
        setSettings(s);
        setModes(s.tool_modes);
      },
      (err: unknown) => {
        if (!(err instanceof UnauthorizedError))
          setError(describeError(toActionError(err), t, i18n));
      },
    );
  }, [api, t, i18n]);

  if (!settings) {
    return error ? (
      <p className="error" role="alert">
        {error}
      </p>
    ) : null;
  }

  const choices = settings.modes;
  const dirty = JSON.stringify(modes) !== JSON.stringify(settings.tool_modes);

  async function save() {
    setError(null);
    try {
      const s = await api.saveAgentToolModes(modes);
      setSettings(s);
      setModes(s.tool_modes);
      setSaved(true);
    } catch (err) {
      if (!(err instanceof UnauthorizedError))
        setError(describeError(toActionError(err), t, i18n));
    }
  }

  return (
    <section className="tool-modes" data-testid="tool-modes">
      <h2>{t("agent.toolModes.heading")}</h2>
      <p className="muted small">
        {t("agent.toolModes.intro", {
          default: t(`agent.toolModes.mode.${settings.default_tool_mode}`),
        })}
      </p>
      {Object.keys(modes).length > 0 && (
        <table className="models">
          <tbody>
            {Object.entries(modes)
              .sort(([a], [b]) => a.localeCompare(b))
              .map(([name, value]) => (
                <tr key={name}>
                  <td className="model-name">{name}</td>
                  <td>
                    <select
                      aria-label={name}
                      value={value}
                      onChange={(e) => {
                        setSaved(false);
                        setModes({ ...modes, [name]: e.target.value });
                      }}
                    >
                      {choices.map((c) => (
                        <option key={c} value={c}>
                          {t(`agent.toolModes.mode.${c}`)}
                        </option>
                      ))}
                    </select>
                  </td>
                  <td>
                    <button
                      type="button"
                      onClick={() => {
                        setSaved(false);
                        const { [name]: _gone, ...rest } = modes;
                        void _gone;
                        setModes(rest);
                      }}
                    >
                      {t("agent.toolModes.remove")}
                    </button>
                  </td>
                </tr>
              ))}
          </tbody>
        </table>
      )}
      <div className="dialog-actions">
        <input
          aria-label={t("agent.toolModes.model")}
          placeholder={t("agent.toolModes.model")}
          value={model}
          onChange={(e) => setModel(e.target.value)}
        />
        <select
          aria-label={t("agent.toolModes.modeLabel")}
          value={mode}
          onChange={(e) => setMode(e.target.value)}
        >
          {choices.map((c) => (
            <option key={c} value={c}>
              {t(`agent.toolModes.mode.${c}`)}
            </option>
          ))}
        </select>
        <button
          type="button"
          disabled={model.trim() === ""}
          onClick={() => {
            setSaved(false);
            setModes({ ...modes, [model.trim()]: mode });
            setModel("");
          }}
        >
          {t("agent.toolModes.add")}
        </button>
        <button
          type="button"
          className="primary"
          disabled={!dirty}
          onClick={() => void save()}
        >
          {t("agent.toolModes.save")}
        </button>
        {saved && (
          <span className="muted small">{t("agent.toolModes.saved")}</span>
        )}
      </div>
      {error && (
        <p className="error" role="alert">
          {error}
        </p>
      )}
    </section>
  );
}
