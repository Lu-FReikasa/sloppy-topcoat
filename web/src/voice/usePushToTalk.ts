import { createContext, useContext, useEffect, useLayoutEffect, useRef, useState, useSyncExternalStore } from "react";
import type { ApiClient } from "../api/client";
import { WebMicrophone, WebPlayer } from "./audio";
import { PushToTalk, type TalkTarget, type VoiceDeps, type VoiceSnapshot } from "./controller";
import { WsVoiceLink } from "./link";

/** The voice parts a page uses; tests provide fakes, the app the real ones (made from the api). */
export const VoiceDepsContext = createContext<((api: ApiClient) => VoiceDeps) | null>(null);

function realDeps(api: ApiClient): VoiceDeps {
  const { url, token } = api.voiceSocket();
  return { link: new WsVoiceLink(url, token), mic: new WebMicrophone(), player: new WebPlayer() };
}

interface Availability {
  transcribe: boolean;
  speak: boolean;
}

const IDLE: VoiceSnapshot = { state: "idle", problem: null };
const NONE: Availability = { transcribe: false, speak: false };

export interface PushToTalkHandle extends VoiceSnapshot {
  /** A node can listen for this person: the key is offered only then. */
  available: boolean;
  press: () => void;
  release: () => void;
  cancel: () => void;
  /** Hand the answer as it is written (see PushToTalk.answer). */
  answer: (text: string, done: boolean) => void;
}

/** Push to talk for one page: `send` posts a transcript as the person's message. */
export function usePushToTalk(
  api: ApiClient,
  target: Omit<TalkTarget, "speak">,
  send: (text: string) => void,
): PushToTalkHandle {
  const makeDeps = useContext(VoiceDepsContext) ?? realDeps;
  const [can, setCan] = useState<Availability>(NONE);
  useEffect(() => {
    let live = true;
    const ask = async () => {
      try {
        const status = await api.voiceStatus();
        if (live) setCan(status);
      } catch {
        if (live) setCan(NONE);
      }
    };
    void ask();
    return () => {
      live = false;
    };
  }, [api]);

  // the latest send and target, without making a new controller for every render
  const sendRef = useRef(send);
  const full: TalkTarget = { ...target, speak: can.speak };
  const targetRef = useRef(full);
  useLayoutEffect(() => {
    sendRef.current = send;
    targetRef.current = full;
  });

  const [ptt, setPtt] = useState<PushToTalk | null>(null);
  useEffect(() => {
    if (!can.transcribe) return;
    const made = new PushToTalk(makeDeps(api), targetRef.current, (text) => sendRef.current(text));
    setPtt(made);
    return () => {
      made.dispose();
      setPtt(null);
    };
  }, [api, can.transcribe, makeDeps]);
  useEffect(() => {
    ptt?.retarget(targetRef.current);
  });

  const snapshot = useSyncExternalStore(
    ptt?.subscribe ?? noSubscribe,
    () => ptt?.state ?? IDLE,
  );
  return {
    ...snapshot,
    available: ptt !== null,
    press: () => ptt?.press(),
    release: () => void ptt?.release(),
    cancel: () => ptt?.cancel(),
    answer: (text, done) => ptt?.answer(text, done),
  };
}

const noSubscribe = () => () => undefined;
