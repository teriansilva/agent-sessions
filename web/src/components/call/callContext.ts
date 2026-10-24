/** The call to HQ, shared by the shell (#1397 P1). Its own module (no component) so the files that
 *  render components keep fast-refresh. */
import { createContext, useContext } from "react";

import type { VoiceCapabilities } from "../../types/voice";
import { IDLE, type CallSnapshot } from "./callClient";

export interface CallContextValue {
  snap: CallSnapshot;
  caps: VoiceCapabilities | null;
  refreshCaps: () => void;
  /** Start a call (the Call tap). `trigger` gets focus back when the call panel closes. */
  start: (opts?: { conversationId?: string | null; trigger?: HTMLElement | null }) => void;
  end: () => void;
  setMuted: (on: boolean) => void;
  /** A card was tapped (confirm or dismiss) during the call: its spoken window closes (#1397 P2). */
  cardDecided: (id: string) => void;
  /** "What needs me?" by tap: HQ reads the Needs-you list out, as if asked aloud. */
  askNeedsYou: () => void;
  /** Close the ended state and hand focus back to what started the call. */
  dismiss: () => void;
  minimized: boolean;
  setMinimized: (on: boolean) => void;
  captionsOn: boolean;
  setCaptionsOn: (on: boolean) => void;
  levels: () => { input: number; output: number };
}

const noop = () => undefined;

/** Outside the shell (a unit test rendering one component) there is no call. */
export const CallContext = createContext<CallContextValue>({
  snap: IDLE,
  caps: null,
  refreshCaps: noop,
  start: noop,
  end: noop,
  setMuted: noop,
  cardDecided: noop,
  askNeedsYou: noop,
  dismiss: noop,
  minimized: false,
  setMinimized: noop,
  captionsOn: true,
  setCaptionsOn: noop,
  levels: () => ({ input: 0, output: 0 }),
});

export function useCall(): CallContextValue {
  return useContext(CallContext);
}

/** Whether a call is in progress (starting, dialing or connected). */
export function callActive(snap: CallSnapshot): boolean {
  return snap.phase === "starting" || snap.phase === "dialing" || snap.phase === "connected";
}

/** Fired with a conversation id to open HQ on it ("Continue in text"); AskConsole listens. */
export const ASK_OPEN_CONVERSATION_EVENT = "agent-sessions:ask-open-conversation";

/** The conversation asked for, until HQ's console takes it. On the first open the console mounts
 *  only AFTER the request (#1640 review 6907), so an event alone would land on no listener and HQ
 *  would open on an empty thread: the console takes this when it mounts, as well as on the event. */
let pendingAskConversation: string | null = null;

/** Open HQ's console on `cid`, whether or not the console is mounted yet. */
export function requestAskConversation(cid: string): void {
  pendingAskConversation = cid;
  window.dispatchEvent(new CustomEvent(ASK_OPEN_CONVERSATION_EVENT, { detail: cid }));
}

/** The conversation asked for and not yet opened, once (null when none is waiting). */
export function takeAskConversation(): string | null {
  const cid = pendingAskConversation;
  pendingAskConversation = null;
  return cid;
}
