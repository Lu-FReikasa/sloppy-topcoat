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
import { ImageAttachButton, ImageDraftStrip, MessageImages } from "../chat/ChatImages";
import { imageFiles, pastedImages, useImageDraft } from "../chat/images";
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
  // D125: images of the next question; the hub keeps them in the tab, in memory only
  const pictures = useImageDraft();
  const loadImage = useCallback(
    (imageId: string) => api.incognitoImage(current ?? "", imageId),
    [api, current],
  );

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
    const images = pictures.images;
    if ((!content && images.length === 0) || !current || busy) return;
    setDraft("");
    pictures.clear();
    setBusy(true);
    setError(null);
    const shown = images.map((i) => ({ id: i.key, width: 0, height: 0, src: i.preview }));
    setMessages((m) => [
      ...m,
      { role: "user", content, ...(shown.length ? { images: shown } : {}) },
    ]);
    try {
      const r = await api.sendIncognito(current, content, images.map((i) => i.data));
      setMessages((m) => [...m, r.message]);
      reload();
    } catch {
      setError(t("incognito.answerFailed"));
      setMessages((m) => m.slice(0, -1));
      setDraft(content);
      pictures.setImages(images);
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
                    <>
                      {m.images && m.images.length > 0 && (
                        <MessageImages images={m.images} load={loadImage} />
                      )}
                      {m.content}
                    </>
                  )}
                  {m.vision && !m.vision.sees && (
                    <p className="talk-calm small" role="status">
                      {t("images.blind")}
                    </p>
                  )}
                </div>
              ))}
              {busy && (
                <p className="muted talk-thinking">{t("talk.thinking")}</p>
              )}
              <div ref={bottom} />
            </div>
          )}
          <ImageDraftStrip
            images={pictures.images}
            problem={pictures.problem}
            onRemove={pictures.remove}
          />
          <form
            className="talk-composer"
            onSubmit={(e) => void send(e)}
            onDragOver={(e) => {
              if (Array.from(e.dataTransfer.types).includes("Files")) e.preventDefault();
            }}
            onDrop={(e) => {
              const files = imageFiles(e.dataTransfer);
              if (files.length === 0) return;
              e.preventDefault();
              void pictures.add(files);
            }}
          >
            <ImageAttachButton onFiles={(files) => void pictures.add(files)} />
            <textarea
              ref={box}
              rows={MIN_LINES}
              value={draft}
              aria-label={t("incognito.placeholder")}
              placeholder={t("incognito.placeholder")}
              onChange={(e) => setDraft(e.target.value)}
              onPaste={(e) => {
                const files = pastedImages(e.clipboardData);
                if (files.length === 0) return;
                e.preventDefault();
                void pictures.add(files);
              }}
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
              disabled={busy || (!draft.trim() && pictures.images.length === 0)}
            >
              {t("talk.send")}
            </button>
          </form>
        </div>
      )}
    </section>
  );
}
