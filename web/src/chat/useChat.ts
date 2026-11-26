import { announceConversations } from "./openChats";
import { useCallback, useEffect, useLayoutEffect, useReducer, useRef } from "react";
import {
  ApiError,
  UnauthorizedError,
  type ApiClient,
  type ChatEvent,
  type StreamTrack,
} from "../api/client";
import type { Conversation, Quality, ToolScope, ToolsMode } from "../types";
import { requestOpen } from "../layout/intent";
import { announceMemoryChange } from "../memory/useQuestions";
import {
  chatReducer,
  initialChatState,
  localMessage,
  type ChatState,
  type NoteKind,
  type StreamError,
} from "./state";
import type { DraftImage } from "./images";

export interface ChatActions {
  state: ChatState;
  refreshList: (q?: string) => void;
  open: (id: string) => void;
  create: (pick?: { model?: string | null; node?: string | null }) => Promise<Conversation | null>;
  update: (
    id: string,
    patch: {
      title?: string;
      model?: string | null;
      node?: string | null;
      quality?: Quality | null;
      tool_scope?: ToolScope | null;
      persona_ref?: string | null;
      pinned?: boolean;
      done?: boolean;
    },
  ) => Promise<void>;
  /** Take a chat the hub created (the next section) into the list and open it. */
  adopt: (conversation: Conversation) => void;
  remove: (id: string) => Promise<void>;
  send: (id: string, content: string, tools?: ToolsMode, images?: DraftImage[]) => void;
  regenerate: (id: string, tools?: ToolsMode) => void;
  /** Go on with the last answer when it was cut off. */
  continueAnswer: (id: string) => void;
  stop: (id: string) => void;
  /** Follow an answer that is being generated in the hub from its start. */
  attach: (id: string, messageId: number) => void;
  /** Take back what the assistant wrote into its memory: a new note is deleted (restorable),
   *  a correction is undone (the notes it replaced are active again), a withdrawn note is
   *  restored. */
  undoNote: (id: string, noteId: string, kind?: NoteKind) => Promise<void>;
}

/** An answer that is cut off from its reader (the connection dropped) is followed again this many
 *  times, a little later each time, before the page says the connection is gone. */
const REATTACH_ATTEMPTS = 5;
const REATTACH_DELAY_MS = 1000;

function toStreamError(err: unknown): StreamError {
  if (err instanceof ApiError) {
    return { code: err.code, message: err.message, retryAfter: err.retryAfter };
  }
  return { code: "unknown", message: err instanceof Error ? err.message : String(err) };
}

/** Conversations and running answers. Stopping and leaving a conversation are independent. */
export function useChat(
  api: ApiClient,
  newChatScope?: ToolScope | null,
  application?: string,
): ChatActions {
  const [state, dispatch] = useReducer(chatReducer, initialChatState);
  const stateRef = useRef(initialChatState);
  // a layout effect, not a passive one: a click on "Stop" a moment after the first token must find
  // the id of the answer (a passive effect runs after the paint, and the click would see old state
  // and close the connection instead, which stops nothing)
  useLayoutEffect(() => {
    stateRef.current = state;
  }, [state]);
  const controllers = useRef(new Map<string, AbortController>());
  const search = useRef<string | undefined>(undefined);

  const refreshList = useCallback(
    (q?: string) => {
      if (q !== undefined) search.current = q;
      api.listConversations(search.current).then(
        (conversations) => dispatch({ type: "list_loaded", conversations }),
        () => undefined,
      );
    },
    [api],
  );

  const open = useCallback(
    (id: string) => {
      api.getConversation(id).then(
        (detail) => dispatch({ type: "detail_loaded", detail }),
        () => undefined,
      );
      api.conversationMemory(id).then(
        (items) => dispatch({ type: "notes_loaded", id, items }),
        () => undefined,
      );
    },
    [api],
  );

  useEffect(() => {
    refreshList();
    const running = controllers.current;
    return () => {
      for (const c of running.values()) c.abort();
    };
  }, [refreshList]);

  const run = useCallback(
    (
      id: string,
      events: (signal: AbortSignal, track: StreamTrack) => AsyncGenerator<ChatEvent>,
    ) => {
      const ctrl = new AbortController();
      controllers.current.set(id, ctrl);
      const track: StreamTrack = { offset: 0 };
      void (async () => {
        let messageId: number | null = null;
        let settled = false;
        let source = events(ctrl.signal, track);
        // The answer runs in the hub, this is only one reader of it: when the connection drops
        // (or the hub closes the stream without an end) the reader attaches again from where it
        // got, a few times, instead of giving up on an answer that is still being written.
        for (let attempt = 0; ; attempt++) {
          let failure: unknown = null;
          try {
            for await (const event of source) {
              if (event.type === "meta" && event.message_id) messageId = event.message_id;
              if (
                event.type === "done" ||
                event.type === "error" ||
                event.type === "stopped" ||
                event.type === "snapshot"
              ) {
                settled = true;
              }
              dispatch({ type: "stream_event", id, event });
              // a note may have been written, corrected or questioned: look at the questions again
              if (event.type === "memory") announceMemoryChange();
              // the assistant prepared a hand-over: the inbox shows the dossier to send
              if (event.type === "handover")
                requestOpen({ module: "page.handovers", props: { id: event.id } });
            }
          } catch (err) {
            if (err instanceof UnauthorizedError) return;
            failure = err;
          }
          if (settled || ctrl.signal.aborted) break;
          if (messageId === null || attempt >= REATTACH_ATTEMPTS) {
            dispatch({
              type: "stream_failed",
              id,
              error: toStreamError(failure ?? new ApiError(0, "network", "the connection ended")),
            });
            break;
          }
          await new Promise((resolve) => setTimeout(resolve, REATTACH_DELAY_MS * (attempt + 1)));
          if (ctrl.signal.aborted) break;
          source = api.attachAnswer(messageId, track.offset, ctrl.signal, track);
        }
        if (controllers.current.get(id) === ctrl) controllers.current.delete(id);
        refreshList();
      })();
    },
    [api, refreshList],
  );

  /** An answer that is still being generated (another tab, a reload, a page that was left):
   *  follow it from its start; it goes on whatever this page does (E3-07). */
  const attach = useCallback(
    (id: string, messageId: number) => {
      if (controllers.current.has(id)) return;
      dispatch({ type: "stream_start", id, userMessage: null });
      run(id, (signal, track) => api.attachAnswer(messageId, 0, signal, track));
    },
    [api, run],
  );

  const send = useCallback(
    (id: string, content: string, tools: ToolsMode = "auto", images: DraftImage[] = []) => {
      const asked = localMessage("user", content);
      if (images.length) {
        // shown from this browser until the hub confirms the message with its own ids (D125)
        asked.images = images.map((i) => ({ id: i.key, width: 0, height: 0, src: i.preview }));
      }
      dispatch({ type: "stream_start", id, userMessage: asked });
      run(id, (signal, track) =>
        api.sendMessage(id, content, signal, tools, track, images.map((i) => i.data)),
      );
    },
    [api, run],
  );

  const regenerate = useCallback(
    (id: string, tools: ToolsMode = "auto") => {
      dispatch({ type: "drop_last_answer", id });
      dispatch({ type: "stream_start", id, userMessage: null });
      run(id, (signal, track) => api.regenerate(id, signal, tools, track));
    },
    [api, run],
  );

  const continueAnswer = useCallback(
    (id: string) => {
      const last = state.details[id]?.messages.at(-1);
      if (
        !last ||
        last.role !== "assistant" ||
        (last.status !== "truncated" && last.status !== "interrupted")
      ) {
        return;
      }
      dispatch({ type: "stream_start", id, userMessage: null, continues: last.id });
      run(id, (signal, track) => api.continueAnswer(id, signal, track));
    },
    [api, run, state.details],
  );

  const undoNote = useCallback(
    async (id: string, noteId: string, kind: NoteKind = "remembered") => {
      try {
        if (kind === "corrected") await api.undoMemory(noteId);
        else if (kind === "retracted") await api.restoreMemory(noteId);
        else await api.deleteMemory(noteId);
        dispatch({ type: "note_removed", id, noteId });
        announceMemoryChange();
      } catch {
        /* the note stays; the memory page shows what is there */
      }
    },
    [api],
  );

  /** "Stop" is a call of its own: the answer is generated by the hub, closing the stream stops
   *  nothing. The stream ends with a `stopped` event that carries what was stored. */
  const stop = useCallback(
    (id: string) => {
      const messageId = stateRef.current.streams[id]?.meta?.message_id;
      if (messageId) void api.stopAnswer(messageId).catch(() => undefined);
      else controllers.current.get(id)?.abort();
    },
    [api],
  );

  const create = useCallback(
    async (pick: { model?: string | null; node?: string | null } = {}) => {
      try {
        const conversation = await api.createConversation({
          ...pick,
          ...(newChatScope ? { tool_scope: newChatScope } : {}),
          application: application || "chat",
        });
        dispatch({ type: "conversation_upserted", conversation });
        dispatch({ type: "detail_loaded", detail: { ...conversation, messages: [] } });
        return conversation;
      } catch {
        return null;
      }
    },
    [api, newChatScope, application],
  );

  const adopt = useCallback((conversation: Conversation) => {
    dispatch({ type: "conversation_upserted", conversation });
    dispatch({ type: "detail_loaded", detail: { ...conversation, messages: [] } });
  }, []);

  const update = useCallback(
    async (
      id: string,
      patch: {
        title?: string;
        model?: string | null;
        node?: string | null;
        quality?: Quality | null;
        tool_scope?: ToolScope | null;
        persona_ref?: string | null;
        pinned?: boolean;
        done?: boolean;
      },
    ) => {
      const conversation = await api.updateConversation(id, patch);
      dispatch({ type: "conversation_upserted", conversation });
      if ("pinned" in patch || "done" in patch) announceConversations();
    },
    [api],
  );

  const remove = useCallback(
    async (id: string) => {
      controllers.current.get(id)?.abort();
      await api.deleteConversation(id);
      dispatch({ type: "conversation_removed", id });
    },
    [api],
  );

  return {
    state,
    refreshList,
    open,
    create,
    update,
    adopt,
    remove,
    send,
    regenerate,
    continueAnswer,
    stop,
    attach,
    undoNote,
  };
}
