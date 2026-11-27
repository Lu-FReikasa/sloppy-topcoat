import { useCallback, useEffect, useLayoutEffect, useRef, useState, type KeyboardEvent } from "react";
import { overlayOf } from "../desktop/state";
import { useTranslation } from "react-i18next";
import type { ApiClient } from "../api/client";
import { ThemeCardContext } from "../chat/ThemeCard";
import { ActivityTimeline } from "../chat/ActivityTimeline";
import { CheckControls } from "../chat/CheckControls";
import { ExportButtons } from "../chat/ExportButtons";
import { HandoffCard } from "../chat/HandoffCard";
import { InternetSwitch, internetOn } from "../chat/InternetSwitch";
import { ScopeHint } from "../chat/ScopeHint";
import { ChatOrigin } from "../chat/ChatOrigin";
import { arrange } from "../chat/layout";
import { Markdown } from "../chat/Markdown";
import { RememberedNotes } from "../chat/MemoryNotes";
import { DoneSteps, ProgressLine } from "../chat/MessageView";
import { PreparedChanges } from "../chat/PreparedChanges";
import { ResearchOffer } from "../chat/ResearchOffer";
import { ReportCardView } from "../chat/ReportCard";
import { ReviewNote, useReviewLights, useReviews } from "../chat/ReviewNote";
import { useChat } from "../chat/useChat";
import { ImageAttachButton, ImageDraftStrip, MessageImages } from "../chat/ChatImages";
import { imageFiles, pastedImages, useImageDraft, type DraftImage } from "../chat/images";
import { useStickToBottom } from "../chat/useStickToBottom";
import { dayKey, dayLabel } from "../chat/time";
import { COMPOSE_EVENT, takeCompose } from "../desktop/compose";
import { requestShow, showFromInput } from "../files/bus";
import type {
  ActivityItem,
  ChatMessage,
  ChatSettings,
  ChatToolsOverview,
  ConversationSegment,
  ConversationSegments,
  ConversationThread,
  Quality,
  ThreadSection,
  ToolScope,
} from "../types";
import { EvidenceMark } from "./EvidenceMark";
import { IdleArt } from "./IdleArt";
import { Clipper, moodOf, useClipperActive } from "../persona/Clipper";
import { MessageLine } from "./MessageLine";
import { MIN_LINES, useGrowingTextarea } from "./growTextarea";
import { PushToTalkButton, VoiceProblemLine } from "../voice/PushToTalkButton";
import { usePushToTalk } from "../voice/usePushToTalk";

/** What the user sees of the history: what was said, an emoji answer, the card of a request that
 *  was handed to the queue, and the result cards of checks (folded), not the run behind it. */
/** An activity note on the images of an answer (D125): `vision_routed` or `vision_blind`. */
export function isVisionNote(item: ActivityItem): boolean {
  const event = item.data?.event;
  return item.kind === "note" && typeof event === "string" && event.startsWith("vision_");
}

export function visibleMessages(messages: ChatMessage[]): ChatMessage[] {
  return messages.filter((m) => {
    if (m.kind === "report") return !!m.report;
    if (m.kind === "handoff") return true;
    if (m.kind === "reaction") return m.content.trim() !== "";
    return (
      (m.kind === undefined || m.kind === "say") &&
      (m.content.trim() !== "" || m.error)
    );
  });
}

interface Props {
  api: ApiClient;
  /** What a conversation started here may use (a workspace's defaults, narrowed by the profile). */
  newChatScope?: ToolScope | null;
  /** The workspace (application) this window belongs to: it shows that application's conversation. */
  application?: string;
  /** A conversation to open instead of the newest one (a link from the shelf or the inbox). */
  conversationId?: string;
}

/** A conversation from before applications belongs to Chat. */
const DEFAULT_APPLICATION = "chat";

/** Where a new section starts in the visible history: the index of the first visible message
 *  that belongs to it, the summary of the section before, and how long the rest was. A section
 *  the user asked for ("new section") has no pause worth naming (under a minute). */
export interface SectionBreak {
  index: number;
  summary: string | null;
  pauseMinutes: number;
}

/** The line between a chat of the continuous conversation and the next one. */
function breakBetween(
  index: number,
  before: ThreadSection,
  nextStart: string,
  thread: ConversationThread | null,
): SectionBreak {
  const idle = thread?.idle_seconds ?? 0;
  const gap = (Date.parse(nextStart) - Date.parse(before.ended_at)) / 1000;
  return {
    index,
    summary: before.summary,
    pauseMinutes: idle > 0 && gap >= idle ? Math.round(gap / 60) : 0,
  };
}

export function sectionBreaks(
  messages: ChatMessage[],
  segments: ConversationSegment[],
  idleSeconds: number,
): SectionBreak[] {
  const out: SectionBreak[] = [];
  segments.forEach((seg, i) => {
    const before = segments[i - 1];
    if (!before) return;
    const index = messages.findIndex((m) => m.id >= seg.first_message_id);
    if (index <= 0) return;
    const gap =
      (Date.parse(seg.started_at) - Date.parse(before.ended_at)) / 1000;
    out.push({
      index,
      summary: before.summary,
      pauseMinutes: gap >= idleSeconds ? Math.round(gap / 60) : 0,
    });
  });
  return out;
}

/** One window, one history: the conversation the user last spoke in (a new one only when there
 *  is none yet). No list, no model or node pickers, no timings; failures are one calm sentence. */
export function TalkPage({
  api,
  newChatScope,
  application = DEFAULT_APPLICATION,
  conversationId,
}: Props) {
  const { t, i18n } = useTranslation();
  const chat = useChat(api, newChatScope, application);
  const clipper = useClipperActive(api); // the persona "clipper" stands in the corner
  // in the overlay Clipper stands in the corner of the screen already, not twice
  const inOverlay = overlayOf(window.location.search);
  const {
    state,
    open,
    send,
    stop,
    attach,
    create,
    adopt,
    regenerate,
    continueAnswer,
    update,
    undoNote,
  } = chat;
  const [draft, setDraft] = useState("");
  // D125: images for the next message (pasted, dropped or chosen)
  const pictures = useImageDraft();
  const loadImage = useCallback((imageId: string) => api.chatImage(imageId), [api]);
  const [sections, setSections] = useState<ConversationSegments | null>(null);
  const [thread, setThread] = useState<ConversationThread | null>(null);
  // "new section" was asked: the next message goes into a new chat that continues this one
  const [wantBreak, setWantBreak] = useState(false);
  // the window shows the open chat only; the chats before it are one click away
  const [showEarlier, setShowEarlier] = useState(false);
  const input = useRef<HTMLTextAreaElement>(null);
  const room = useRef<HTMLElement>(null);
  useGrowingTextarea(input, draft, room);
  const creating = useRef(false);
  // a text another window handed over ("Look at this in Development") goes into the draft
  useEffect(() => {
    function take() {
      const text = takeCompose(application);
      if (text === null) return;
      setDraft((d) => (d.trim() === "" ? text : `${d}\n${text}`));
      input.current?.focus();
    }
    take();
    window.addEventListener(COMPOSE_EVENT, take);
    return () => window.removeEventListener(COMPOSE_EVENT, take);
  }, [application]);
  // the development assistant points at code (`repo.show`): once per item of the live chain
  const shown = useRef(new Set<number>());
  // the few choices of a conversation that stay: how much trouble the answer takes, the internet
  const [settings, setSettings] = useState<ChatSettings | null>(null);
  const [overview, setOverview] = useState<ChatToolsOverview | undefined>();
  useEffect(() => {
    api.chatSettings().then(setSettings, () => undefined);
  }, [api]);

  // the conversation the person last spoke in, in this application
  const mine = state.conversations.filter(
    (c) => (c.application ?? DEFAULT_APPLICATION) === application,
  );
  const id = conversationId ?? mine[0]?.id;
  useEffect(() => {
    if (!state.listLoaded) return;
    if (id) {
      if (!state.details[id]) open(id);
    } else if (!creating.current) {
      creating.current = true;
      void create();
    }
  }, [state.listLoaded, id, state.details, open, create]);

  const detail = id ? state.details[id] : undefined;
  // a conversation of another application opened here (from the archive, a window's talk) keeps
  // that application's rights: the tools shown are its own (2026-11-13)
  const ownApp = detail?.application ?? application;
  useEffect(() => {
    api.chatTools(ownApp).then(setOverview, () => undefined);
  }, [api, ownApp]);
  const stream = id ? state.streams[id] : undefined;
  const running = stream?.running === true;
  const messageCount = detail?.messages.length ?? 0;
  // the second look's findings under the answers, what the memory did with an answer, and the
  // offer to check an answer against the servers
  // a look the person asked for (reviewAnswer) asks the reviews again a few times until it is in
  const [reviewTick, setReviewTick] = useState(0);
  const reviews = useReviews(api, id ?? "", messageCount + reviewTick * 100000);
  const lights = useReviewLights(api, id ?? "", messageCount + reviewTick * 100000);
  const [asked, setAsked] = useState<Set<number>>(new Set());
  const reviewAnswer = (messageId: number) => {
    if (!api) return;
    setAsked((prev) => new Set(prev).add(messageId));
    void api.reviewMessage(messageId).then(
      () => {
        for (const ms of [3000, 8000, 20000, 45000])
          window.setTimeout(() => setReviewTick((n) => n + 1), ms);
      },
      () => setAsked((prev) => {
        const next = new Set(prev);
        next.delete(messageId);
        return next;
      }),
    );
  };
  const notes = id ? state.notes[id] : undefined;
  const offerAt = id ? state.offers[id] : undefined;

  // what the hub still does for this chat in the background (the running summary, the second
  // look): writing never waits for it, the page only says so while it is not done
  const [work, setWork] = useState<"idle" | "waiting" | "running">("idle");
  useEffect(() => {
    if (!id) return;
    let live = true;
    let timer: number | undefined;
    const look = () =>
      api.conversationBackground(id).then(
        (r) => {
          if (!live) return;
          setWork(r.state);
          if (r.state !== "idle") timer = window.setTimeout(look, 5000);
        },
        () => {
          if (live) setWork("idle");
        },
      );
    void look();
    return () => {
      live = false;
      window.clearTimeout(timer);
    };
  }, [api, id, messageCount, running]);

  useEffect(() => {
    const last = detail?.messages.find(
      (m) => m.role === "assistant" && m.status === "running",
    );
    if (detail && last && !state.streams[detail.id]) attach(detail.id, last.id);
  }, [detail, state.streams, attach]);

  const messages = detail ? visibleMessages(detail.messages) : [];
  useEffect(() => {
    if (!id) return;
    api.conversationSegments(id).then(setSections, () => undefined);
  }, [api, id, messageCount, running]);
  useEffect(() => {
    if (!id) return;
    api.conversationThread(id).then(setThread, () => undefined);
  }, [api, id, messageCount, running]);
  // the chats this one continues, shown above it as one history
  const earlierSections = thread?.enabled
    ? thread.sections.filter((s) => !s.current)
    : [];
  const earlierKey = earlierSections.map((s) => s.id).join(",");
  useEffect(() => {
    if (!showEarlier) return;
    for (const s of earlierSections) if (!state.details[s.id]) open(s.id);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [earlierKey, showEarlier, open]);
  const earlier = earlierSections.map((s) => ({
    section: s,
    messages: visibleMessages(state.details[s.id]?.messages ?? []),
  }));
  const history = wantBreak
    ? []
    : [...(showEarlier ? earlier.flatMap((e) => e.messages) : []), ...messages];
  const breaks = new Map<number, SectionBreak>();
  let offset = 0;
  (showEarlier ? earlier : []).forEach((e, i) => {
    const before = earlier[i - 1]?.section;
    if (before && e.messages.length > 0) {
      breaks.set(
        offset,
        breakBetween(offset, before, e.section.started_at, thread),
      );
    }
    offset += e.messages.length;
  });
  const lastEarlier = showEarlier ? earlier.at(-1)?.section : undefined;
  if (lastEarlier && offset > 0 && messages.length > 0) {
    breaks.set(
      offset,
      breakBetween(offset, lastEarlier, messages[0]?.created_at ?? "", thread),
    );
  }
  // sections cut inside one chat (chats from before sections were chats of their own)
  for (const b of sections?.enabled
    ? sectionBreaks(messages, sections.segments, sections.idle_seconds)
    : []) {
    breaks.set(b.index + offset, b);
  }
  // one answer per question: the hub's "looking now" bubble is not shown and the report card
  // follows the answer (folded); a section cut stays at its message, wherever it moves
  const cutAt = new Map<number, SectionBreak>();
  for (const [index, cut] of breaks) {
    const at = history[index];
    if (at) cutAt.set(at.id, cut);
  }
  const arranged = arrange(history);
  const lastShown = arranged.at(-1)?.message;
  // the chain of what the hub does for the answer that is made now (E7-01)
  const chain = stream?.activity ?? [];
  useEffect(() => {
    for (const item of stream?.activity ?? []) {
      if (item.status !== "ok" || !item.title.startsWith("repo.show")) continue;
      if (shown.current.has(item.id)) continue;
      shown.current.add(item.id);
      const request = showFromInput(item.data.input);
      if (request) requestShow(request);
    }
  }, [stream?.activity]);
  const pendingBreak = wantBreak;
  const { ref, onScroll, stick, unseen, jump } =
    useStickToBottom<HTMLDivElement>([messages.length, stream?.text]);

  /** Where the next message goes: this chat, or (after a rest, or when a new section was asked
   *  for) a new chat that continues it. */
  async function post(from: string, text: string, images: DraftImage[] = []) {
    let target = from;
    try {
      const next = await api.nextSection(from, wantBreak);
      if (next.created) {
        adopt(next.conversation);
        target = next.conversation.id;
      }
    } catch {
      // the message still goes to this chat
    }
    setWantBreak(false);
    send(target, text, "auto", images);
  }

  // D127 push to talk: the transcript is the person's message, sent like a typed one; an answer
  // that is still being written gets it once it is done (half duplex)
  // the answer to the spoken message is the first one after it: the stream while it is written,
  // then the stored answer; a stream that ends without one (an error) ends it with what there is
  // a text said in one chat goes into that chat only: when the person opened another chat while
  // it waited, it is dropped (and nothing is read out), never posted into the other one
  const voiceAsked = useRef<{ after: number; stale: unknown; chat: string } | null>(null);
  const spoken = useRef<{ text: string; chat: string } | null>(null);
  const sendSpoken = useRef<() => void>(() => undefined);
  const stopVoice = useRef<() => void>(() => undefined);
  useLayoutEffect(() => {
    sendSpoken.current = () => {
      const said = spoken.current;
      if (said === null || !detail) return;
      if (said.chat !== detail.id) {
        spoken.current = null;
        stopVoice.current();
        return;
      }
      if (running) return;
      spoken.current = null;
      voiceAsked.current = {
        after: detail.messages.at(-1)?.id ?? 0,
        stale: stream,
        chat: detail.id,
      };
      stick();
      void post(detail.id, said.text);
    };
  });
  const voice = usePushToTalk(
    api,
    { conversationId: detail?.id, language: (i18n.language || "auto").slice(0, 2) },
    (text, chat) => {
      spoken.current = { text, chat: chat ?? "" };
      sendSpoken.current();
    },
  );
  useLayoutEffect(() => {
    stopVoice.current = voice.cancel;
  });
  useEffect(() => {
    sendSpoken.current(); // what was said while an answer was still written (or in another chat)
  }, [running, detail?.id]);
  const storedAnswer = detail?.messages.findLast((m) => m.role === "assistant");
  const { answer: voiceAnswer } = voice;
  useEffect(() => {
    const waiting = voiceAsked.current;
    if (!waiting || !detail) return;
    if (detail.id !== waiting.chat && detail.continues_id !== waiting.chat) {
      voiceAsked.current = null; // another chat was opened: its answers are not read out
      voice.cancel();
      return;
    }
    if (running) {
      voiceAnswer(stream?.text ?? "", false);
    } else if (stream && stream !== waiting.stale) {
      voiceAsked.current = null;
      voiceAnswer(stream.text, true);
    } else if (storedAnswer && storedAnswer.id > waiting.after) {
      voiceAsked.current = null;
      voiceAnswer(storedAnswer.content, true);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [running, stream, storedAnswer?.id, detail?.id]);

  function submit() {
    const text = draft.trim();
    const images = pictures.images;
    if (!detail || (!text && images.length === 0) || running) return;
    setDraft("");
    pictures.clear();
    stick();
    void post(detail.id, text, images);
    input.current?.focus();
  }

  function onKey(e: KeyboardEvent<HTMLTextAreaElement>) {
    if (e.key === "Enter" && !e.shiftKey && !e.nativeEvent.isComposing) {
      e.preventDefault();
      submit();
    }
  }

  const showText = running && (stream?.text ?? "") !== "";
  // what can be done with the last answer: ask for it again, or let a cut-off one go on
  const lastAnswer = lastShown?.role === "assistant" ? lastShown : undefined;
  const cutOff =
    lastAnswer?.status === "truncated" || lastAnswer?.status === "interrupted";
  return (
    <section ref={room} className="talk" aria-label={t("talk.label")}>
      <div
        className="talk-stream"
        ref={ref}
        onScroll={onScroll}
        role="log"
        aria-live="polite"
      >
        {!detail && <p className="muted">{t("app.loading")}</p>}
        {detail && (
          <ChatOrigin
            application={ownApp}
            overview={overview}
            internet={internetOn(overview, detail.tool_scope, settings?.user.internet ?? true)}
            pinned={detail.pinned}
            onPin={(pinned) => void update(detail.id, { pinned })}
            onDone={() => void update(detail.id, { done: true })}
          />
        )}
        {detail && !wantBreak && !showEarlier && earlierSections.length > 0 && (
          <button
            type="button"
            className="talk-earlier"
            onClick={() => setShowEarlier(true)}
          >
            {t("talk.showEarlier")}
          </button>
        )}
        {detail && history.length === 0 && !running && (
          <div className="talk-empty">
            <IdleArt />
            <p className="muted">{t("talk.empty")}</p>
          </div>
        )}
        {arranged.map(({ message: m, report }, i) => {
          const day = dayKey(m.created_at);
          const first =
            day !== null &&
            (i === 0 || dayKey(arranged[i - 1]?.message.created_at) !== day);
          const cut = cutAt.get(m.id);
          return (
            <div key={m.id} className="talk-row">
              {cut && (
                <div
                  className="section-break"
                  role="separator"
                  data-testid="section-break"
                >
                  <span>
                    {t("talk.sectionBreak")}
                    {cut.pauseMinutes > 0 &&
                      ` · ${t("talk.sectionPause", { minutes: cut.pauseMinutes })}`}
                  </span>
                  {cut.summary && (
                    <details>
                      <summary>{t("talk.sectionSummary")}</summary>
                      <p>{cut.summary}</p>
                    </details>
                  )}
                </div>
              )}
              {first && m.created_at && (
                <div
                  className="day-separator"
                  role="separator"
                  data-testid="day-separator"
                >
                  <span>{dayLabel(m.created_at, i18n.language)}</span>
                </div>
              )}
              {m.role === "assistant" &&
                m.activity &&
                m.activity.some((a) => !isVisionNote(a)) && (
                  // a chain that only says what happened to the images is no chain of steps:
                  // the notice under the answer says it (D125)
                  <div className="talk-chain">
                    <ActivityTimeline
                      items={m.activity}
                      live={m.status === "running"}
                    />
                  </div>
                )}
              {m.kind === "handoff" ? (
                <HandoffCard message={m} api={api} />
              ) : m.kind === "reaction" ? (
                <div
                  className="talk-msg talk-assistant talk-reaction"
                  data-testid="message-reaction"
                  aria-label={t("chat.reaction")}
                >
                  {m.content}
                </div>
              ) : (
                <div
                  className={`talk-msg ${m.role === "user" ? "talk-user" : "talk-assistant"}`}
                >
                  {m.kind === "report" && m.report ? (
                    <details className="talk-report">
                      <summary>{t("talk.report")}</summary>
                      <ReportCardView card={m.report} api={api} />
                    </details>
                  ) : m.role === "assistant" ? (
                    <>
                      <ThemeCardContext.Provider value={api}><Markdown text={m.content} /></ThemeCardContext.Provider>
                      {m.report?.research_offer && api && id && (
                        <ResearchOffer api={api} conversationId={id} messageId={m.id} offer={m.report.research_offer} />
                      )}
                    </>
                  ) : (
                    <>
                      {m.images && m.images.length > 0 && (
                        <MessageImages images={m.images} load={loadImage} />
                      )}
                      {m.content}
                    </>
                  )}
                  {report?.report && (
                    // the check of this answer: at its end, folded, not a message of its own
                    <details className="talk-report">
                      <summary>{t("talk.report")}</summary>
                      <ReportCardView
                        card={report.report}
                        api={api}
                        withChanges={false}
                      />
                    </details>
                  )}
                  {report?.report?.changes_for && api && (
                    // what the check prepared is read at once, not behind the folded report
                    <PreparedChanges
                      api={api}
                      taskId={report.report.changes_for}
                    />
                  )}
                </div>
              )}
              {id && m.id > 0 && (
                <MessageLine api={api} conversationId={id} message={m}>
                  {m.role === "assistant" &&
                    m.status === "complete" &&
                    (m.kind ?? "say") === "say" &&
                    (reviews[m.id]?.reviewer_model ?? "hub") === "hub" && (
                      // in the answer's footer, a small button: as a bar of its own it read as a
                      // separator between messages (#77)
                      <button
                        type="button"
                        className="talk-review-ask"
                        disabled={asked.has(m.id)}
                        onClick={() => reviewAnswer(m.id)}
                        title={t("talk.reviewAskHint")}
                      >
                        {asked.has(m.id) ? t("talk.reviewAsked") : t("talk.reviewAsk")}
                      </button>
                    )}
                </MessageLine>
              )}
              {m.role === "assistant" && notes?.[m.id] && id && (
                <RememberedNotes
                  notes={notes[m.id]!}
                  onUndo={(noteId, kind) => void undoNote(id, noteId, kind)}
                />
              )}
              {reviews[m.id] !== undefined && (
                <ReviewNote review={reviews[m.id]!} api={api} />
              )}
              {m.role === "assistant" &&
                m.activity?.some((a) => a.data?.event === "vision_blind") && (
                  // D125: the model of this answer could not see the images it was given
                  <p className="talk-calm" role="status" data-testid="vision-blind">
                    {t("images.blind")}
                  </p>
                )}
              {m.role === "assistant" &&
                (m.status === "error" || m.status === "interrupted") && (
                  <p className="talk-calm" role="status">
                    {t("talk.failed")}
                  </p>
                )}
              {m.role === "assistant" && (
                <EvidenceMark message={m} light={lights[m.id] ?? null} />
              )}
            </div>
          );
        })}
        {!running && offerAt != null && lastShown?.id === offerAt && detail && (
          <p className="notice-line" data-testid="check-offer">
            <span>{t("check.offer")} </span>
            <button
              type="button"
              onClick={() => send(detail.id, t("check.offerAsk"), "on")}
            >
              {t("check.offerButton")}
            </button>
          </p>
        )}
        {!running &&
          lastShown?.role === "assistant" &&
          lastShown.activity?.some((i) => i.kind === "announce") && (
            <p
              className="notice-line"
              data-testid="announce-note"
              role="status"
            >
              <span>{t("chain.announce.note")} </span>
              <button
                type="button"
                onClick={() =>
                  detail && void post(detail.id, t("chain.announce.doIt"))
                }
              >
                {t("chain.announce.button")}
              </button>
            </p>
          )}
        {running && chain.some((a) => !isVisionNote(a)) && (
          <div className="talk-chain" data-testid="talk-chain">
            <ActivityTimeline items={chain} live />
            {stream?.meta?.message_id && stream.check && !stream.check.done && (
              <CheckControls
                api={api}
                messageId={stream.meta.message_id}
                items={chain}
              />
            )}
          </div>
        )}
        {showText && (
          <div className="talk-msg talk-assistant" data-testid="talk-live">
            <ThemeCardContext.Provider value={api}><Markdown text={stream?.text ?? ""} /></ThemeCardContext.Provider>
          </div>
        )}
        {pendingBreak && (
          <div
            className="section-break"
            role="separator"
            data-testid="section-pending"
          >
            <span>{t("talk.sectionBreakPending")}</span>
            <button
              type="button"
              className="tp-link"
              onClick={() => setWantBreak(false)}
            >
              {t("talk.sectionCancel")}
            </button>
          </div>
        )}
        {running &&
          !showText &&
          chain.length === 0 &&
          stream?.progress !== undefined && (
            <div className="talk-progress" data-testid="talk-progress">
              <DoneSteps
                steps={(stream.check?.steps ?? []).filter(
                  (x) => x.kind === "call",
                )}
              />
              <ProgressLine text={stream.progress} />
            </div>
          )}
        {running &&
          !showText &&
          chain.length === 0 &&
          stream?.progress === undefined && (
            <p className="muted talk-thinking" role="status">
              {t("talk.thinking")}
            </p>
          )}
        {!running && stream?.error && (
          <p className="talk-calm" role="alert">
            {t("talk.failed")}
          </p>
        )}
        {!running && work !== "idle" && (
          <p
            className="muted talk-thinking"
            role="status"
            data-testid="background-work"
          >
            {t("talk.background")}
          </p>
        )}
      </div>
      {unseen && (
        <button type="button" className="talk-jump" onClick={jump}>
          {t("talk.jump")}
        </button>
      )}
      <div className="talk-tools">
        {detail && !running && lastAnswer && (
          <>
            {cutOff && (
              <button
                type="button"
                title={t("talk.continueHint")}
                onClick={() => continueAnswer(detail.id)}
              >
                {t("talk.continue")}
              </button>
            )}
            <button type="button" onClick={() => regenerate(detail.id)}>
              {t("talk.regenerate")}
            </button>
          </>
        )}
        {detail && (
          <details className="talk-options" data-testid="talk-options">
            <summary>{t("chat.options")}</summary>
            <div className="talk-options-panel">
              <label className="picker quality-picker">
                <span className="fact-label">{t("quality.label")}</span>
                <select
                  value={detail.quality ?? settings?.user.quality ?? "balanced"}
                  aria-label={t("quality.label")}
                  onChange={(e) =>
                    void update(detail.id, {
                      quality: e.target.value as Quality,
                    })
                  }
                >
                  {(["fast", "balanced", "thorough"] as const).map((q) => (
                    <option key={q} value={q}>
                      {t(`quality.${q}`)}
                    </option>
                  ))}
                </select>
              </label>
              {overview && (
                <InternetSwitch
                  overview={overview}
                  scope={detail.tool_scope}
                  defaultOn={settings?.user.internet ?? true}
                  onChange={(scope) =>
                    void update(detail.id, { tool_scope: scope })
                  }
                />
              )}
              {overview && (
                <ScopeHint overview={overview} scope={detail.tool_scope} />
              )}
              <ExportButtons conversation={detail} />
            </div>
          </details>
        )}
        {(sections?.enabled || thread?.enabled) &&
          detail &&
          messages.length > 0 && (
            <button
              type="button"
              title={t("talk.newSectionHint")}
              disabled={running || pendingBreak}
              onClick={() => setWantBreak(true)}
            >
              {t("talk.newSection")}
            </button>
          )}
      </div>
      {clipper && detail && !inOverlay && (
        <Clipper
          mood={moodOf({
            running,
            checking: (stream?.activity?.length ?? 0) > 0,
            last: lastAnswer?.status,
          })}
          seed={messages.length}
        />
      )}
      <VoiceProblemLine voice={voice} />
      <ImageDraftStrip
        images={pictures.images}
        problem={pictures.problem}
        onRemove={pictures.remove}
      />
      <form
        className="talk-composer"
        onSubmit={(e) => {
          e.preventDefault();
          submit();
        }}
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
        <ImageAttachButton
          disabled={!detail}
          onFiles={(files) => void pictures.add(files)}
        />
        <textarea
          ref={input}
          rows={MIN_LINES}
          value={draft}
          placeholder={t("talk.placeholder")}
          aria-label={t("talk.placeholder")}
          onChange={(e) => setDraft(e.target.value)}
          onKeyDown={onKey}
          onPaste={(e) => {
            // a screenshot on the clipboard becomes an image of the message (D125)
            const files = pastedImages(e.clipboardData);
            if (files.length === 0) return;
            e.preventDefault();
            void pictures.add(files);
          }}
        />
        <PushToTalkButton voice={voice} scope={room} disabled={!detail} />
        {running ? (
          <button type="button" onClick={() => detail && stop(detail.id)}>
            {t("talk.stop")}
          </button>
        ) : (
          <button
            type="submit"
            className="primary"
            disabled={!detail || (draft.trim() === "" && pictures.images.length === 0)}
          >
            {t("talk.send")}
          </button>
        )}
      </form>
    </section>
  );
}
