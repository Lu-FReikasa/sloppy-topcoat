import {
  useCallback,
  useEffect,
  useRef,
  useState,
  type FormEvent,
} from "react";
import { useTranslation } from "react-i18next";
import type { ApiClient } from "../api/client";
import { Markdown } from "../chat/Markdown";
import { requestOpen } from "../layout/intent";
import type { IncognitoMessage, IncognitoTab } from "../types";
import { reducedMotion } from "../motion";
import { Burning } from "./Burning";
import { MIN_LINES, useGrowingTextarea } from "../talk/growTextarea";

interface Props {
  api: ApiClient;
}

/** The incognito folder (docs/PLATFORM.md, section 3.9): conversations that are never written.
 *  They live in the memory of the hub only, know nothing of the person but what is said in the
 *  tab, may use the web (switchable per tab) and stay until they are burnt or the hub restarts. */
export function IncognitoPage({ api }: Props) {
  const { t } = useTranslation();
  const [tabs, setTabs] = useState<IncognitoTab[]>([]);
  const [current, setCurrent] = useState<string | null>(null);
  const [messages, setMessages] = useState<IncognitoMessage[]>([]);
  const [draft, setDraft] = useState("");
  const [busy, setBusy] = useState(false);
  const [burning, setBurning] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const bottom = useRef<HTMLDivElement>(null);
  const box = useRef<HTMLTextAreaElement>(null);
  useGrowingTextarea(box, draft);

  function choose(id: string | null) {
    setMessages([]);
    setCurrent(id);
  }

  const tab = tabs.find((x) => x.id === current) ?? null;

  const reload = useCallback(() => {
    api.incognitoTabs().then(
      (r) => {
        setTabs(r.tabs);
        setError(null);
      },
      () => setError(t("incognito.loadFailed")),
    );
  }, [api, t]);
  useEffect(reload, [reload]);

  useEffect(() => {
    if (!current) return;
    let alive = true;
    api.incognitoTab(current).then(
      (x) => alive && setMessages(x.messages ?? []),
      () => alive && setMessages([]),
    );
    return () => {
      alive = false;
    };
  }, [api, current]);

  useEffect(
    () => bottom.current?.scrollIntoView?.({ block: "end" }),
    [messages, busy],
  );

  async function create() {
    try {
      const x = await api.createIncognitoTab(true);
      reload();
      choose(x.id);
    } catch {
      setError(t("incognito.createFailed"));
    }
  }

  async function send(e: FormEvent) {
    e.preventDefault();
    const content = draft.trim();
    if (!content || !current || busy) return;
    setDraft("");
    setBusy(true);
    setError(null);
    setMessages((m) => [...m, { role: "user", content }]);
    try {
      const r = await api.sendIncognito(current, content);
      setMessages((m) => [...m, r.message]);
      reload();
    } catch {
      setError(t("incognito.answerFailed"));
      setMessages((m) => m.slice(0, -1));
      setDraft(content);
    } finally {
      setBusy(false);
    }
  }

  async function burn(id: string) {
    const finish = async () => {
      try {
        await api.deleteIncognitoTab(id);
      } catch {
        /* it may be gone already (a restart): the list is read again below */
      }
      setBurning(null);
      if (current === id) choose(null);
      reload();
    };
    if (reducedMotion()) return finish();
    setBurning(id);
    window.setTimeout(() => void finish(), 1400);
  }

  async function toggleInternet() {
    if (!tab) return;
    try {
      await api.updateIncognitoTab(tab.id, !tab.internet);
      reload();
    } catch {
      setError(t("incognito.updateFailed"));
    }
  }

  return (
    <section className="incognito" aria-label={t("incognito.title")}>
      <p className="muted small" data-testid="incognito-notice">
        {t("incognito.notice")}
      </p>
      <div className="incognito-bar">
        <button type="button" className="primary" onClick={() => void create()}>
          {t("incognito.new")}
        </button>
        {tabs.map((x) => (
          <button
            key={x.id}
            type="button"
            role="tab"
            aria-selected={x.id === current}
            className={x.id === current ? "primary" : undefined}
            onClick={() => choose(x.id)}
          >
            {x.title || t("incognito.untitled")}
          </button>
        ))}
      </div>
      {error && (
        <p className="bad" role="alert">
          {error}
        </p>
      )}
      {!tab && tabs.length === 0 && (
        <p className="muted">{t("incognito.empty")}</p>
      )}
      {!tab && tabs.length > 0 && (
        <p className="muted">{t("incognito.pick")}</p>
      )}
      {tab && (
        <div className="incognito-tab" data-testid="incognito-tab">
          <div className="incognito-tools">
            <label className="check">
              <input
                type="checkbox"
                checked={tab.internet}
                onChange={() => void toggleInternet()}
              />{" "}
              {t("incognito.internet")}
            </label>
            <span className="spacer" />
            <button
              type="button"
              onClick={() =>
                requestOpen({
                  module: "page.handovers",
                  props: { incognito: tab.id },
                })
              }
              disabled={messages.length === 0}
            >
              {t("incognito.handover")}
            </button>
            <button
              type="button"
              className="danger"
              onClick={() => void burn(tab.id)}
              disabled={burning !== null}
            >
              {t("incognito.burn")}
            </button>
          </div>
          {burning === tab.id ? (
            <Burning text={messages.map((m) => m.content).join("\n")} />
          ) : (
            <div className="incognito-messages" role="log" aria-live="polite">
              {messages.map((m, i) => (
                <div
                  key={i}
                  className={`talk-msg ${m.role === "user" ? "talk-user" : "talk-assistant"}`}
                >
                  {m.role === "assistant" ? (
                    <Markdown text={m.content} />
                  ) : (
                    m.content
                  )}
                </div>
              ))}
              {busy && (
                <p className="muted talk-thinking">{t("talk.thinking")}</p>
              )}
              <div ref={bottom} />
            </div>
          )}
          <form className="talk-composer" onSubmit={(e) => void send(e)}>
            <textarea
              ref={box}
              rows={MIN_LINES}
              value={draft}
              aria-label={t("incognito.placeholder")}
              placeholder={t("incognito.placeholder")}
              onChange={(e) => setDraft(e.target.value)}
              onKeyDown={(e) => {
                if (
                  e.key === "Enter" &&
                  !e.shiftKey &&
                  !e.nativeEvent.isComposing
                ) {
                  e.preventDefault();
                  void send(e as unknown as FormEvent);
                }
              }}
            />
            <button
              type="submit"
              className="primary"
              disabled={busy || !draft.trim()}
            >
              {t("talk.send")}
            </button>
          </form>
        </div>
      )}
    </section>
  );
}
