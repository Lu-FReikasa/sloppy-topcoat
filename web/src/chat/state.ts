// State of the chat view: conversations, cached messages, and one running answer per
// conversation (several can stream at once while the user looks at another one).
import type { CheckDone, CheckStepEvent, ChatEvent } from "../api/client";
import type {
  ActivityItem,
  ChatMessage,
  Conversation,
  ConversationDetail,
  MemoryItem,
} from "../types";

export interface StreamError {
  code: string;
  message: string;
  retryAfter?: number;
}

export interface StreamState {
  running: boolean;
  /** Id of the truncated answer this stream continues (its text is appended to it). */
  continues: number | null;
  text: string;
  meta: Extract<ChatEvent, { type: "meta" }> | null;
  error: StreamError | null;
  /** Why the node holds the answer back (it froze its model for local use), else undefined. */
  pause?: string[];
  /** The server check running before the answer, if the hub started one (E3-01d). */
  check?: CheckState;
  /** E3-07 D3: the progress line while the check's tools run ("docker.ps · node-a — why"; an
   *  empty text while nothing is known yet); gone when the result is there. */
  progress?: string;
  /** The model that is reading the finished answer against the evidence (thorough level). */
  verifying?: string;
  /** E7-01: what the hub does for this answer so far, in order (thoughts, calls, checks …). */
  activity?: ActivityItem[];
}

/** Add an item of the activity chain, or replace the one with the same id. */
export function upsertActivity(items: ActivityItem[] | undefined, item: ActivityItem) {
  const list = items ?? [];
  const at = list.findIndex((i) => i.id === item.id);
  return at < 0 ? [...list, item] : list.map((i, n) => (n === at ? item : i));
}

/** A server check as the chat knows it: why it runs, its steps so far, how it ended. */
export interface CheckState {
  reason: "forced" | "detected" | "decided";
  steps: CheckStepEvent[];
  done: CheckDone | null;
}

/** What kind of change a note line reports: a new note, one confirmed again, a statement that
 *  corrected earlier notes, or a note withdrawn because the user called it wrong or a test. */
export type NoteKind = "remembered" | "confirmed" | "corrected" | "retracted";

/** What the assistant wrote into its memory because of one answer ("remembered: …"). */
export interface NoteItem {
  id: string;
  text: string;
  evidence: string;
  /** false: an existing note that was confirmed again (nothing to undo). */
  isNew: boolean;
  /** Absent means "remembered" / "confirmed" (by `isNew`). */
  kind?: "corrected" | "retracted";
  /** The earlier notes this one contradicts (the assistant marked both as disputed). */
  contradicts?: { id: string; text: string }[];
  /** For a correction: the notes it replaced (or withdrew, if `withdrawn`). */
  replaced?: { id: string; text: string }[];
  withdrawn?: boolean;
}

export interface MessageNotes {
  items: NoteItem[];
  blocked: number;
  /** Why nothing was noted although the user asked for it ("Nicht gemerkt, weil …"). */
  notNoted?: string;
}

export interface ChatState {
  conversations: Conversation[];
  listLoaded: boolean;
  details: Record<string, ConversationDetail | undefined>;
  streams: Record<string, StreamState | undefined>;
  /** conversation id -> message id -> notes written for that answer */
  notes: Record<string, Record<number, MessageNotes> | undefined>;
  /** conversation id -> message id -> the server check that answer is based on (this session) */
  checks: Record<string, Record<number, CheckState> | undefined>;
  /** conversation id -> the answer under which a server check is offered (the question may have
   *  been about the user's servers, the hub answered first). */
  offers: Record<string, number | undefined>;
  /** conversation id -> the model that is verifying the last answer right now (thorough level) */
  verifying: Record<string, string | undefined>;
}

export const initialChatState: ChatState = {
  conversations: [],
  listLoaded: false,
  details: {},
  streams: {},
  notes: {},
  checks: {},
  offers: {},
  verifying: {},
};

export type ChatAction =
  | { type: "list_loaded"; conversations: Conversation[] }
  | { type: "conversation_upserted"; conversation: Conversation }
  | { type: "conversation_removed"; id: string }
  | { type: "detail_loaded"; detail: ConversationDetail }
  | { type: "stream_start"; id: string; userMessage: ChatMessage | null; continues?: number }
  | { type: "stream_event"; id: string; event: ChatEvent }
  | { type: "stream_failed"; id: string; error: StreamError }
  | { type: "stream_stopped"; id: string }
  | { type: "drop_last_answer"; id: string }
  | { type: "notes_loaded"; id: string; items: MemoryItem[] }
  | { type: "note_removed"; id: string; noteId: string };

let localId = 0;
/** Ids below zero mark messages the server has not confirmed yet. */
export function nextLocalId(): number {
  localId -= 1;
  return localId;
}

export function localMessage(role: "user" | "assistant", content: string): ChatMessage {
  return {
    id: nextLocalId(),
    role,
    content,
    model: null,
    node: null,
    fallback_from: null,
    status: "complete",
    error: null,
    prompt_tokens: null,
    completion_tokens: null,
    first_token_ms: null,
    duration_ms: null,
    context_trimmed: 0,
    created_at: new Date().toISOString(),
  };
}

function withMessages(
  state: ChatState,
  id: string,
  update: (messages: ChatMessage[]) => ChatMessage[],
): ChatState {
  const detail = state.details[id];
  if (!detail) return state;
  return {
    ...state,
    details: { ...state.details, [id]: { ...detail, messages: update(detail.messages) } },
  };
}

/** Replace the message with the same id (a continued answer), else append. */
function upsert(messages: ChatMessage[], message: ChatMessage): ChatMessage[] {
  return messages.some((m) => m.id === message.id)
    ? messages.map((m) => (m.id === message.id ? message : m))
    : [...messages, message];
}

/** A placeholder that was stored as "running" and ended with nothing said is gone. */
function dropRunning(state: ChatState, id: string): ChatState {
  return withMessages(state, id, (m) => m.filter((x) => x.status !== "running"));
}

function setStream(state: ChatState, id: string, stream: StreamState | undefined): ChatState {
  return { ...state, streams: { ...state.streams, [id]: stream } };
}

export function chatReducer(state: ChatState, action: ChatAction): ChatState {
  switch (action.type) {
    case "list_loaded":
      return { ...state, conversations: action.conversations, listLoaded: true };
    case "conversation_upserted": {
      const others = state.conversations.filter((c) => c.id !== action.conversation.id);
      const detail = state.details[action.conversation.id];
      return {
        ...state,
        conversations: [action.conversation, ...others],
        details: detail
          ? { ...state.details, [detail.id]: { ...detail, ...action.conversation } }
          : state.details,
      };
    }
    case "conversation_removed": {
      const { [action.id]: _detail, ...details } = state.details;
      const { [action.id]: _stream, ...streams } = state.streams;
      void _detail;
      void _stream;
      return {
        ...state,
        conversations: state.conversations.filter((c) => c.id !== action.id),
        details,
        streams,
      };
    }
    case "detail_loaded":
      // A running answer owns the message list; the server copy would lose the temporary parts.
      if (state.streams[action.detail.id]?.running) return state;
      return { ...state, details: { ...state.details, [action.detail.id]: action.detail } };
    case "stream_start": {
      const next = setStream(state, action.id, {
        running: true,
        continues: action.continues ?? null,
        text: "",
        meta: null,
        error: null,
      });
      const user = action.userMessage;
      const { [action.id]: _offer, ...offers } = next.offers;
      void _offer;
      const fresh = { ...next, offers };
      return user ? withMessages(fresh, action.id, (m) => [...m, user]) : fresh;
    }
    case "stream_event": {
      const ev = action.event;
      if (ev.type === "verification" || ev.type === "verifying") {
        // arrives after `done`, when the stream is already gone: it belongs to the stored answer
        if (ev.type === "verifying") {
          return { ...state, verifying: { ...state.verifying, [action.id]: ev.model } };
        }
        const { [action.id]: _v, ...verifying } = state.verifying;
        void _v;
        const done = { ...state, verifying };
        if (!ev.available) return done;
        const result = {
          ok: ev.ok ?? false,
          issues: ev.issues ?? [],
          model: ev.model ?? "",
        };
        return withMessages(done, action.id, (messages) =>
          messages.map((m) => (m.id === ev.message_id ? { ...m, verification: result } : m)),
        );
      }
      if (ev.type === "memory") {
        // arrives after `done`, when the stream is already gone
        const items: NoteItem[] = [
          ...(ev.corrected ?? []).map((n) => ({
            id: n.id,
            text: n.text,
            evidence: n.evidence,
            isNew: true,
            kind: "corrected" as const,
            replaced: n.replaced,
            withdrawn: n.withdrawn,
          })),
          ...(ev.retracted ?? []).map((n) => ({
            id: n.id,
            text: n.text,
            evidence: "doc",
            isNew: false,
            kind: "retracted" as const,
          })),
          ...ev.remembered.map((n) => ({ ...n, isNew: true })),
          ...ev.confirmed.map((n) => ({ ...n, isNew: false })),
        ];
        const byMessage = {
          ...state.notes[action.id],
          [ev.message_id]: {
            items,
            blocked: ev.blocked,
            ...(ev.not_noted ? { notNoted: ev.not_noted } : {}),
          },
        };
        return { ...state, notes: { ...state.notes, [action.id]: byMessage } };
      }
      const stream = state.streams[action.id];
      if (!stream) return state;
      switch (ev.type) {
        case "tools_start":
        case "tools_wait":
          return state;
        case "bubble":
        case "report":
          // the hub's confirmation bubble and the report card are messages of their own
          return withMessages(state, action.id, (m) => upsert(m, ev.message));
        case "progress":
          return setStream(state, action.id, { ...stream, progress: ev.text });
        case "progress_done": {
          const { progress: _gone, ...rest } = stream;
          void _gone;
          return setStream(state, action.id, rest);
        }
        case "tool_step":
          return stream.check
            ? setStream(state, action.id, {
                ...stream,
                check: { ...stream.check, steps: [...stream.check.steps, ev] },
              })
            : state;
        case "tools_done":
          return stream.check
            ? setStream(state, action.id, { ...stream, check: { ...stream.check, done: ev } })
            : state;
        case "meta": {
          const next = setStream(state, action.id, {
            ...stream,
            meta: ev,
            ...(ev.checking ? { check: { reason: ev.checking, steps: [], done: null } } : {}),
          });
          if (!ev.user_message) return next;
          const confirmed = ev.user_message;
          return withMessages(next, action.id, (messages) => {
            // attached to a run that already had its question stored: nothing to add
            if (messages.some((m) => m.id === confirmed.id)) return messages;
            const at = messages.findLastIndex((m) => m.role === "user" && m.id < 0);
            if (at < 0) return [...messages, confirmed];
            return messages.map((m, i) => (i === at ? confirmed : m));
          });
        }
        case "delta":
          return setStream(state, action.id, { ...stream, text: stream.text + ev.text });
        case "discard":
          return setStream(state, action.id, { ...stream, text: "" });
        case "activity":
          return setStream(state, action.id, {
            ...stream,
            activity: upsertActivity(stream.activity, ev.item),
          });
        case "status":
          if (ev.state === "paused") {
            return setStream(state, action.id, { ...stream, pause: ev.reasons ?? [] });
          }
          if (ev.state === "running") {
            return setStream(state, action.id, { ...stream, pause: undefined });
          }
          return state; // "interrupted": the message follows with status truncated
        case "done": {
          const settled = setStream(
            withMessages(state, action.id, (m) => upsert(m, ev.message)),
            action.id,
            undefined,
          );
          const next = stream.meta?.check_offer
            ? { ...settled, offers: { ...settled.offers, [action.id]: ev.message.id } }
            : settled;
          if (!stream.check) return next;
          const byMessage = { ...state.checks[action.id], [ev.message.id]: stream.check };
          return { ...next, checks: { ...next.checks, [action.id]: byMessage } };
        }
        case "stopped":
        case "snapshot": {
          // the run ended (a stop call, the hub going down) or is over and gone: the stored
          // message is the answer
          const settled = setStream(state, action.id, undefined);
          const message = ev.message;
          return message
            ? withMessages(settled, action.id, (m) => upsert(m, message))
            : dropRunning(settled, action.id);
        }
        case "error": {
          const next = ev.message_row
            ? withMessages(state, action.id, (m) => upsert(m, ev.message_row as ChatMessage))
            : state;
          return setStream(next, action.id, {
            running: false,
            continues: null,
            text: "",
            meta: stream.meta,
            ...(stream.check ? { check: stream.check } : {}),
            ...(stream.activity ? { activity: stream.activity } : {}),
            error: {
              code: ev.code,
              message: ev.message,
              ...(ev.retry_after !== undefined ? { retryAfter: ev.retry_after } : {}),
            },
          });
        }
      }
      return state;
    }
    case "stream_failed": {
      const stream = state.streams[action.id];
      return setStream(state, action.id, {
        running: false,
        continues: stream?.continues ?? null,
        text: stream?.text ?? "",
        meta: stream?.meta ?? null,
        error: action.error,
      });
    }
    case "stream_stopped": {
      const stream = state.streams[action.id];
      if (!stream) return state;
      const next = setStream(state, action.id, undefined);
      if (!stream.text) return next;
      if (stream.continues !== null) {
        const id = stream.continues;
        return withMessages(next, action.id, (m) =>
          m.map((x) =>
            x.id === id ? { ...x, content: x.content + stream.text, status: "stopped" } : x,
          ),
        );
      }
      const partial: ChatMessage = {
        ...localMessage("assistant", stream.text),
        status: "stopped",
        node: stream.meta?.node ?? null,
        model: stream.meta?.model ?? null,
        fallback_from: stream.meta?.fallback_from ?? null,
        context_trimmed: stream.meta?.context.trimmed ?? 0,
      };
      return withMessages(next, action.id, (m) => [...m, partial]);
    }
    case "notes_loaded": {
      const byMessage: Record<number, MessageNotes> = {};
      for (const item of action.items) {
        const messageId = item.source_ref.message_id;
        if (messageId === undefined) continue;
        const entry = byMessage[messageId] ?? { items: [], blocked: 0 };
        entry.items.push({
          id: item.id,
          text: item.text,
          evidence: item.evidence,
          isNew: true,
          ...(item.source_ref.correction ? { kind: "corrected" as const } : {}),
        });
        byMessage[messageId] = entry;
      }
      // what the running session already knows (confirmed / blocked) is kept
      const known = state.notes[action.id] ?? {};
      for (const [key, value] of Object.entries(known)) {
        const messageId = Number(key);
        if (!value) continue;
        const loaded = byMessage[messageId];
        const extra = value.items.filter((n) => !n.isNew);
        if (loaded || extra.length > 0 || value.blocked > 0) {
          byMessage[messageId] = {
            items: [...(loaded?.items ?? []), ...extra],
            blocked: value.blocked,
          };
        }
      }
      return { ...state, notes: { ...state.notes, [action.id]: byMessage } };
    }
    case "note_removed": {
      const byMessage = { ...state.notes[action.id] };
      for (const [key, value] of Object.entries(byMessage)) {
        if (value)
          byMessage[Number(key)] = {
            ...value,
            items: value.items.filter((n) => n.id !== action.noteId),
          };
      }
      return { ...state, notes: { ...state.notes, [action.id]: byMessage } };
    }
    case "drop_last_answer":
      // what the last question got: bubbles, the report card, the closing bubble (E3-07 D3)
      return withMessages(state, action.id, (m) => {
        let end = m.length;
        while (end > 0 && m[end - 1]?.role === "assistant") end -= 1;
        return m.slice(0, end);
      });
  }
}
