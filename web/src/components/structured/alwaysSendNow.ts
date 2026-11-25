/** Always send now (#1447): a device-local preference (localStorage, like the other pane prefs).
 *
 * When on, the pane presses Send now for the first queued message as soon as Send now is
 * offered for it — the same control, fences and recovery as the button, never a new submit. */
import { useCallback, useEffect, useState } from "react";
import type { StructuredSnapshot } from "../../types/api";
import { operatorTurns, sendNowTarget } from "./structuredView";

export const ALWAYS_SEND_NOW_KEY = "agent-sessions.always-send-now";
const EVENT = "agent-sessions:always-send-now";

/** The choice made on this page when storage refused to keep it (private mode, quota): it
 *  holds until the page goes, instead of a re-read of storage silently undoing it. */
let pageOnly: boolean | null = null;

export function readAlwaysSendNow(): boolean {
  if (pageOnly !== null) return pageOnly;
  try {
    return localStorage.getItem(ALWAYS_SEND_NOW_KEY) === "1";
  } catch {
    return false;
  }
}

export function writeAlwaysSendNow(on: boolean): void {
  try {
    if (on) localStorage.setItem(ALWAYS_SEND_NOW_KEY, "1");
    else localStorage.removeItem(ALWAYS_SEND_NOW_KEY);
    pageOnly = null;
  } catch {
    pageOnly = on;
  }
  window.dispatchEvent(new Event(EVENT));
}

/** The preference, kept in step across panes and tabs. */
export function useAlwaysSendNow(): [boolean, (on: boolean) => void] {
  const [on, setOn] = useState(readAlwaysSendNow);
  useEffect(() => {
    const refresh = () => setOn(readAlwaysSendNow());
    const onStorage = (e: StorageEvent) => {
      if (e.key === null || e.key === ALWAYS_SEND_NOW_KEY) refresh();
    };
    window.addEventListener("storage", onStorage);
    window.addEventListener(EVENT, refresh);
    return () => {
      window.removeEventListener("storage", onStorage);
      window.removeEventListener(EVENT, refresh);
    };
  }, []);
  const set = useCallback((value: boolean) => {
    setOn(value);
    writeAlwaysSendNow(value);
  }, []);
  return [on, set];
}

/** The queued message Always send now would send next, or null. Only the first queued message,
 *  and only while Send now is offered for it: nothing else is sending or stopping a reply, and
 *  a refused selection (`interrupt_failed`) waits for a deliberate click instead of looping. */
export function autoSendNowHead(snap: StructuredSnapshot | null): string | null {
  if (!snap || !sendNowTarget(snap)) return null;
  const turns = operatorTurns(snap);
  const head = turns.find((t) => t.state === "queued");
  if (!head || head.delivery) return null;
  const inFlight = (t: (typeof turns)[number]) =>
    t.state === "delivering" || (t.state === "queued" && !!t.delivery && t.delivery !== "interrupt_failed");
  if (turns.some(inFlight)) return null;
  return head.turn_id;
}
