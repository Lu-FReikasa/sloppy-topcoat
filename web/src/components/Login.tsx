import { useState, type FormEvent } from "react";
import { useTranslation } from "react-i18next";
import { LanguageSwitch } from "./LanguageSwitch";

interface Props {
  onLogin: (token: string) => void;
  invalid?: boolean;
}

export function Login({ onLogin, invalid = false }: Props) {
  const { t } = useTranslation();
  const [token, setToken] = useState("");

  function submit(e: FormEvent) {
    e.preventDefault();
    const trimmed = token.trim();
    if (trimmed) onLogin(trimmed);
  }

  return (
    <main className="login">
      <form className="card" onSubmit={submit}>
        <h1>{t("login.heading")}</h1>
        {invalid && (
          <p className="error" role="alert">
            {t("login.invalid")}
          </p>
        )}
        <label htmlFor="dev-token">{t("login.tokenLabel")}</label>
        <input
          id="dev-token"
          type="password"
          autoComplete="off"
          value={token}
          onChange={(e) => setToken(e.target.value)}
        />
        <p className="muted small">{t("login.hint")}</p>
        <button type="submit" disabled={!token.trim()}>
          {t("login.submit")}
        </button>
        <LanguageSwitch />
      </form>
    </main>
  );
}
