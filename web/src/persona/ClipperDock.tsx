import { announceConversations } from "../chat/openChats";
import { useEffect, useLayoutEffect, useReducer, useRef, useState, type CSSProperties, type FormEvent } from "react";
import { useTranslation } from "react-i18next";
import type { ApiClient, ChatEvent } from "../api/client";
import { asOrder, minSteps, progressOf, stepsAfter, StepBar, summarize } from "../chat/ActivityTimeline";
import { Markdown } from "../chat/Markdown";
import { PreparedChanges } from "../chat/PreparedChanges";
import { requestOpen } from "../layout/intent";
import type { PropValue } from "../layout/types";
import { Clipper, ticketNumber } from "./Clipper";
import { TASK_EVENT, type TaskSignal } from "./heraldNews";
import { applicationFor, dockMood, dockReduce, EMPTY_DOCK, shortText } from "./dock";
import "./clipper.css";

interface Props {
  api: ApiClient;
  /** The window Clipper is attached to: its program, title and the view it shows. */
  program: string;
  programTitle: string;
  module: string;
  props: Record<string, PropValue>;
  viewTitle: string;
  /** Where the talk stands: the window's upper right corner (perch) or the column beside it. */
  style: CSSProperties;
  /** The column is open beside the window (the window gives it room, Desktop.tsx). */
  expanded: boolean;
  /** The window stands at the top of the screen: no room above its title bar, he sits below it. */
  atTop?: boolean;
  /** The conversation to go on with (a window pinned, restored or reloaded keeps its talk). */
  conversationId?: string | null;
  onConversation?: (id: string) => void;
  onExpand: (open: boolean) => void;
  onClose: () => void;
}

/** Clipper at a window (AltGr+C; the maintainer's design, 2026-11-13). He leans over the window's
 *  title bar and talks in pixel speech bubbles: only his newest line and the field to answer.
 *  A click on him folds the talk out into a column beside the window; he then hangs at the top of
 *  both and holds them together, until he is clicked again. The talk belongs to the window's
 *  application and starts with what the window shows (the hub's `context`); the whole conversation
 *  is in Chat. */
export function ClipperDock({
  api,
  program,
  programTitle,
  module,
  props,
  viewTitle,
  style,
  expanded,
  atTop = false,
  conversationId,
  onConversation,
  onExpand,
  onClose,
}: Props) {
  const { t } = useTranslation();
  const [s, dispatch] = useReducer(dockReduce, EMPTY_DOCK);
  const [text, setText] = useState("");
  // the bubble at the perch can be put away; Clipper stays
  const [quiet, setQuiet] = useState(false);
  const stop = useRef<AbortController | null>(null);
  const input = useRef<HTMLTextAreaElement>(null);
  const miniInput = useRef<HTMLTextAreaElement>(null);
  const list = useRef<HTMLOListElement>(null);

  useEffect(() => (expanded ? input.current : miniInput.current)?.focus(), [expanded]);
  // the fields grow with what is typed, up to their max-height (clipper.css); one line was ~25 signs
  useLayoutEffect(() => {
    for (const el of [input.current, miniInput.current]) if (el) grow(el);
  }, [text, expanded]);

  // a talk that existed before: its bubbles, and the answer that may still be running (the hub
  // keeps answering whatever the page does, chat_runs.py)
  useEffect(() => {
    if (!conversationId) return;
    const ctl = new AbortController();
    stop.current = ctl;
    void (async () => {
      try {
        const conv = await api.getConversation(conversationId);
        const running = conv.messages.find((m) => m.role === "assistant" && m.status === "running");
        dispatch({ type: "loaded", id: conversationId, messages: conv.messages, running: Boolean(running) });
        if (running) {
          for await (const event of api.attachAnswer(running.id, 0, ctl.signal)) dispatch({ type: "event", event });
        }
      } catch (err) {
        if (!ctl.signal.aborted) dispatch({ type: "failed", text: err instanceof Error ? err.message : String(err) });
      } finally {
        dispatch({ type: "ended" });
        if (stop.current === ctl) stop.current = null;
      }
    })();
    return () => ctl.abort();
    // only the talk it was opened with; a new one starts from `send`
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // the check's place in the queue (queue_watch.py on the hub): his ticket while it waits
  const [ahead, setAhead] = useState<number | null>(null);
  const taskId = s.taskId;
  useEffect(() => {
    if (!taskId) return;
    const onTask = (e: Event) => {
      const { event, data } = (e as CustomEvent<TaskSignal>).detail;
      if (event !== "agent_progress" || data.task_id !== taskId) return;
      setAhead(data.phase === "queued" && typeof data.ahead === "number" ? data.ahead : null);
    };
    window.addEventListener(TASK_EVENT, onTask);
    return () => window.removeEventListener(TASK_EVENT, onTask);
  }, [taskId]);
  const queued = s.running && taskId && ahead !== null ? { number: ticketNumber(taskId), ahead } : null;
  // closing the talk stops only this reader; the answer goes on in the hub
  useEffect(() => () => stop.current?.abort(), []);
  useEffect(() => {
    const el = list.current;
    if (el) el.scrollTop = el.scrollHeight; // the newest bubble in view
  }, [s.bubbles.length, s.answer, expanded]);

  const send = async (e?: FormEvent) => {
    e?.preventDefault();
    const content = text.trim();
    if (!content || s.running) return;
    setText("");
    setQuiet(false);
    dispatch({ type: "sent", text: content });
    const ctl = new AbortController();
    stop.current = ctl;
    try {
      let id = s.conversationId;
      if (!id) {
        const conv = await api.createConversation({
          title: content.slice(0, 60),
          application: applicationFor(program),
          context: { module, props: { ...plain(props), ...placeOf(program) }, title: `${programTitle} · ${viewTitle}` },
        });
        id = conv.id;
        dispatch({ type: "conversation", id });
        onConversation?.(id);
        announceConversations(); // Start lists it among the open chats at once
      }
      for await (const event of api.sendMessage(id, content, ctl.signal)) dispatch({ type: "event", event: event as ChatEvent });
    } catch (err) {
      if (!ctl.signal.aborted) dispatch({ type: "failed", text: err instanceof Error ? err.message : String(err) });
    } finally {
      dispatch({ type: "ended" });
      if (stop.current === ctl) stop.current = null;
    }
  };

  // the progress of a check: the step against the run's limit and what checks usually take
  const checkAt = s.activity.map((i) => i.kind).lastIndexOf("check");
  const order = checkAt >= 0 ? asOrder(s.activity[checkAt]?.data.order) : undefined;
  const sum = summarize(s.activity);
  const progress =
    s.running && order
      ? progressOf(stepsAfter(s.activity, checkAt), minSteps(order), true, sum.current?.kind === "tool", order)
      : null;

  // prepared changes still waiting (the card decides them; this only says that there are some)
  const [pending, setPending] = useState(0);
  const changesFor = s.changesFor;
  useEffect(() => {
    if (!changesFor || s.running) return;
    let alive = true;
    api.projectChanges(changesFor).then(
      (all) => alive && setPending(all.filter((c) => c.status === "pending").length),
      () => undefined,
    );
    return () => {
      alive = false;
    };
  }, [api, changesFor, s.running, expanded]);
  // a research started from the offer runs in the hub and writes into this conversation: he
  // reads it again now and then until it is over
  const [followResearch, setFollowResearch] = useState(false);
  const following = followResearch && s.running;
  const convId = s.conversationId;
  useEffect(() => {
    if (!following || !convId) return;
    const timer = window.setInterval(() => {
      api.getConversation(convId).then(
        (conv) =>
          dispatch({
            type: "loaded",
            id: convId,
            messages: conv.messages,
            running: conv.messages.some((m) => m.role === "assistant" && m.status === "running"),
          }),
        () => undefined,
      );
    }, 4000);
    return () => window.clearInterval(timer);
  }, [api, following, convId]);
  const startOffer = () => {
    const offer = s.offer;
    if (!offer || !convId) return;
    dispatch({ type: "offerTaken" });
    setFollowResearch(true);
    api.startResearchInChat(convId, offer.question, offer.messageId).then(
      (m) => dispatch({ type: "event", event: { type: "bubble", message: m } }),
      (err) => dispatch({ type: "failed", text: err instanceof Error ? err.message : String(err) }),
    );
  };

  const waiting = !s.running && (s.offer || s.handoverId || (changesFor && pending > 0)) && (
    <span className="clipper-waiting">
      {s.offer && (
        <button type="button" className="primary small" onClick={startOffer}>
          {t("researchOffer.start")}
        </button>
      )}
      {changesFor && pending > 0 && (
        <button type="button" className="primary small" onClick={() => onExpand(true)}>
          {t("clipper.dock.changesWaiting", { count: pending })}
        </button>
      )}
      {s.handoverId && (
        <button
          type="button"
          className="primary small"
          onClick={() => requestOpen({ module: "page.handovers", props: { id: s.handoverId as string } })}
        >
          {t("clipper.dock.handoverWaiting")}
        </button>
      )}
    </span>
  );

  const newest = s.running && s.answer ? s.answer : [...s.bubbles].reverse().find((b) => b.who === "clipper")?.text;
  const short = newest ? shortText(newest) : null;

  const working = s.running && (
    <p className="clipper-progress small">
      <span className="muted">
        {sum.current ? t("chain.now", { what: sum.current.title || t(`chain.kind.${sum.current.kind}`) }) : t("chain.working")}
      </span>
      {progress && <StepBar step={progress.step} of={progress.of} live typical={progress.typical} max={progress.max} />}
    </p>
  );

  const ask = (
    <form className="clipper-ask" onSubmit={(e) => void send(e)}>
      <textarea
        ref={input}
        rows={expanded ? 2 : 1}
        value={text}
        placeholder={t("clipper.dock.placeholder")}
        aria-label={t("clipper.dock.placeholder")}
        data-esc-first=""
        onChange={(e) => setText(e.target.value)}
        onKeyDown={(e) => {
          if (e.key === "Enter" && !e.shiftKey) {
            e.preventDefault();
            void send();
          }
          if (e.key === "Escape") {
            if (expanded) onExpand(false);
            else setQuiet(true);
          }
        }}
      />
      {s.running ? (
        <button type="button" onClick={() => stop.current?.abort()}>
          {t("clipper.dock.stop")}
        </button>
      ) : (
        <button type="submit" className="primary" disabled={!text.trim()}>
          {t("clipper.dock.send")}
        </button>
      )}
    </form>
  );

  return (
    <aside
      className={`clipper-talk ${expanded ? "is-column" : "is-perch"}${atTop ? " at-top" : ""}`}
      style={style}
      aria-label={t("clipper.dock.label", { program: programTitle })}
      data-testid="clipper-dock"
      // the window's drag moves him in the same frame (Desktop.tsx, grab)
      data-dock-for={program}
      data-talk={expanded ? "column" : "perch"}
    >
      <button
        type="button"
        className="clipper-talk-figure"
        aria-expanded={expanded}
        title={expanded ? t("clipper.dock.fold") : t("clipper.dock.unfold")}
        aria-label={expanded ? t("clipper.dock.fold") : t("clipper.dock.unfold")}
        onClick={() => {
          setQuiet(false);
          onExpand(!expanded);
        }}
      >
        <Clipper
          mood={dockMood(s)}
          seed={s.bubbles.length}
          app={program}
          queue={queued}
          burn={progress ? progress.step / Math.max(progress.max ?? progress.of, 1) : null}
        />
      </button>

      {!expanded && !quiet && (
        // the perch (the maintainer, 2026-11-13): a comic bubble flying free beside him, a small
        // field and Send; nothing else until he is folded out
        <div className="clipper-perch">
          {waiting}
          {(short || s.running) && (
            <button
              type="button"
              className={`clipper-comic${s.running && !s.answer ? " is-busy" : ""}`}
              data-testid="clipper-bubble"
              title={t("clipper.dock.unfold")}
              onClick={() => onExpand(true)}
            >
              {short ? (
                <Markdown text={short.text} />
              ) : (
                <span>
                  {sum.current ? t("chain.now", { what: sum.current.title || t(`chain.kind.${sum.current.kind}`) }) : t("chain.working")}
                  {progress && ` · ${progress.step}/${progress.max ?? progress.of}`}
                </span>
              )}
            </button>
          )}
          <form className="clipper-mini-ask" onSubmit={(e) => void send(e)}>
            <textarea
              ref={miniInput}
              rows={1}
              value={text}
              placeholder={t("clipper.dock.placeholder")}
              aria-label={t("clipper.dock.placeholder")}
              data-esc-first=""
              onChange={(e) => setText(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === "Enter" && !e.shiftKey) {
                  e.preventDefault();
                  void send();
                }
                if (e.key === "Escape") setQuiet(true);
              }}
            />
            {s.running ? (
              <button type="button" onClick={() => stop.current?.abort()}>
                {t("clipper.dock.stop")}
              </button>
            ) : (
              <button type="submit" disabled={!text.trim()}>
                {t("clipper.dock.send")}
              </button>
            )}
          </form>
        </div>
      )}

      {expanded && (
        <div className="clipper-column">
          <header className="clipper-column-head">
            <span className="muted small">{t("clipper.dock.about", { place: viewTitle })}</span>
            {s.conversationId && (
              <button
                type="button"
                className="linklike small"
                onClick={() => requestOpen({ module: "page.chat", props: { id: s.conversationId as string } })}
              >
                {t("clipper.dock.openChat")}
              </button>
            )}
            <button type="button" className="linklike small" onClick={() => onExpand(false)}>
              {t("clipper.dock.fold")}
            </button>
            <button type="button" className="linklike small" onClick={onClose} aria-label={t("clipper.dock.close")}>
              ×
            </button>
          </header>
          <ol className="clipper-bubbles" ref={list}>
            {s.bubbles.map((b) => (
              <li key={b.id} className={`clipper-pixel-line is-${b.who}${b.failed ? " is-failed" : ""}`}>
                {b.who === "clipper" ? <Markdown text={b.text} /> : b.text}
              </li>
            ))}
            {s.running && s.answer && (
              <li className="clipper-pixel-line is-clipper is-live">
                <Markdown text={s.answer} />
              </li>
            )}
          </ol>
          {s.handoverId && !s.running && (
            <p className="clipper-waiting">
              <button
                type="button"
                className="primary small"
                onClick={() => requestOpen({ module: "page.handovers", props: { id: s.handoverId as string } })}
              >
                {t("clipper.dock.handoverWaiting")}
              </button>
            </p>
          )}
          {changesFor && !s.running && <PreparedChanges api={api} taskId={changesFor} />}
          {s.offer && !s.running && (
            <p className="clipper-waiting">
              <button type="button" className="primary small" onClick={startOffer}>
                {t("researchOffer.start")}
              </button>
            </p>
          )}
          {working}
          {ask}
        </div>
      )}
    </aside>
  );
}

/** What the window's page shows now, when it says so (`data-place-*` on an element inside it): the
 *  project page names the project and task in view, which its opening props may not. */
export function placeOf(program: string): Record<string, string> {
  const win = Array.from(document.querySelectorAll<HTMLElement>("[data-program]")).find((el) => el.dataset.program === program);
  const el = win?.querySelector<HTMLElement>("[data-place-project], [data-place-node]");
  const out: Record<string, string> = {};
  if (el?.dataset.placeProject) out.project = el.dataset.placeProject;
  if (el?.dataset.placeNode) out.node = el.dataset.placeNode;
  return out;
}

/** A field as high as its text, up to its CSS max-height (then it scrolls). */
function grow(el: HTMLTextAreaElement): void {
  el.style.height = "auto";
  el.style.height = `${el.scrollHeight + 4}px`;
}

/** The props as the hub takes them: strings, numbers and booleans. */
function plain(props: Record<string, PropValue>): Record<string, string | number | boolean> {
  const out: Record<string, string | number | boolean> = {};
  for (const [k, v] of Object.entries(props)) {
    if (typeof v === "string" || typeof v === "number" || typeof v === "boolean") out[k] = v;
  }
  return out;
}
