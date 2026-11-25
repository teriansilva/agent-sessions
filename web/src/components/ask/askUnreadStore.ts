/** Whether any HQ conversation has an unseen update (#1443 Phase 4), shared (#1736).
 *
 *  It was `AskSidebar`'s private `useAskUnread`. The favicon's unread dot needs the same answer the
 *  HQ icon's dot shows, so the read lives here once: `/api/ask/unread` on the first subscription,
 *  on a timer while the tab is visible, when the tab comes back, and at once when a conversation
 *  is marked seen (`ASK_SEEN_EVENT`).
 *
 *  **One poll for every reader**, at the shortest interval any reader asked for. The last reader
 *  to leave stops it and forgets the answer, so a later mount starts from "nothing unread", as the
 *  icon always did. Only the NEWEST read's answer applies: an older "unread" resolving after a
 *  newer "seen" never brings the dot back, and a read that lands after the poll stopped is
 *  dropped. A failed read keeps what is shown. */
import { useCallback, useSyncExternalStore } from "react";

import { api } from "../../lib/api";
import { ASK_SEEN_EVENT } from "./askUpdates";

/** How often the icon asks whether a followed session has an unseen update (#1443 Phase 4). */
export const UNREAD_POLL_MS = 60_000;

let unread = false;
/** Each reader and the interval it asked for. */
const readers = new Map<() => void, number>();
let gen = 0;
let timer: number | null = null;
let timerMs = 0;

function emit(): void {
  for (const fn of readers.keys()) fn();
}

function read(): void {
  if (document.visibilityState !== "visible") return;
  const g = ++gen;
  Promise.resolve()
    .then(() => api.askUnread())
    .then((r) => {
      const next = Boolean(r.unread);
      if (g !== gen || readers.size === 0 || next === unread) return;
      unread = next;
      emit();
    })
    .catch(() => {
      /* keep what is shown */
    });
}

/** (Re)arm the timer at the shortest interval any reader wants; none left → stop. */
function arm(): void {
  const ms = readers.size ? Math.min(...readers.values()) : 0;
  if (ms === timerMs) return;
  if (timer !== null) window.clearInterval(timer);
  timer = ms ? window.setInterval(read, ms) : null;
  timerMs = ms;
}

function subscribe(fn: () => void, pollMs: number): () => void {
  const first = readers.size === 0;
  readers.set(fn, pollMs);
  arm();
  if (first) {
    document.addEventListener("visibilitychange", read);
    window.addEventListener(ASK_SEEN_EVENT, read);
    read();
  }
  return () => {
    if (!readers.delete(fn)) return;
    arm();
    if (readers.size === 0) {
      gen += 1;
      unread = false;
      document.removeEventListener("visibilitychange", read);
      window.removeEventListener(ASK_SEEN_EVENT, read);
    }
  };
}

function getUnread(): boolean {
  return unread;
}

/** Whether any conversation has an unseen update, polled every `pollMs` while anything reads it. */
export function useAskUnread(pollMs: number = UNREAD_POLL_MS): boolean {
  const sub = useCallback((fn: () => void) => subscribe(fn, pollMs), [pollMs]);
  return useSyncExternalStore(sub, getUnread, () => false);
}
