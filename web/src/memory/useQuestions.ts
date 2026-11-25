import { useCallback, useEffect, useState } from "react";
import type { ApiClient } from "../api/client";
import type { MemoryQuestion } from "../types";

/** Fired whenever something changed the memory (a note written or corrected in a chat, a
 *  question answered, an edit on the memory page): the open questions are looked at again. */
export const MEMORY_CHANGED = "memory-changed";

export function announceMemoryChange(): void {
  window.dispatchEvent(new Event(MEMORY_CHANGED));
}

const POLL_MS = 60_000;

/** The open clarification questions of the signed-in user (a badge in the navigation, a card
 *  on the chat and memory pages). Refreshed by `memory-changed` and once a minute, because
 *  the tidy-up can open one while nobody is looking. */
export function useOpenQuestions(api: ApiClient): {
  questions: MemoryQuestion[];
  reload: () => void;
} {
  const [questions, setQuestions] = useState<MemoryQuestion[]>([]);
  const reload = useCallback(() => {
    api.memoryQuestions().then(setQuestions, () => undefined);
  }, [api]);

  useEffect(() => {
    reload();
    const timer = setInterval(reload, POLL_MS);
    window.addEventListener(MEMORY_CHANGED, reload);
    return () => {
      clearInterval(timer);
      window.removeEventListener(MEMORY_CHANGED, reload);
    };
  }, [reload]);

  return { questions, reload };
}
