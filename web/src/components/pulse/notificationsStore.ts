/** The bell's read of `/api/pulse/notifications`, shared (#1736).
 *
 *  It was `NotificationBell`'s local state. The favicon's unread dot needs the same number, and a
 *  second poll of the same endpoint would be a second answer that can disagree with the badge, so
 *  the read lives here once: the bell renders it, `useFavicon` reads `unread` from it.
 *
 *  **One poll for every reader.** The first subscriber starts it (an immediate read, then every
 *  `NOTIFICATIONS_POLL_MS`, plus `ACTION_RESOLVED_EVENT`); the last one to leave stops it and
 *  forgets what was read, so nothing outlives the readers and a later mount starts from empty, as
 *  the bell always did. A read or an action's answer that lands after its poll stopped is dropped
 *  (`mutateNotifications`), so nothing read for one session reaches the next one in the tab.
 *
 *  **Defensive by design**: this is top-bar chrome. A synchronous throw (an older server without
 *  the route, a partial test double) or a failed read keeps what is shown and never takes the app
 *  shell down over a notification count. */
import { useSyncExternalStore } from "react";

import { api } from "../../lib/api";
import { ACTION_RESOLVED_EVENT } from "../../lib/actionEvents";
import { reconcileDeviceNotifications } from "../../swPush";
import type { PulseNotification } from "../../types/api";

export const NOTIFICATIONS_POLL_MS = 60_000;

export interface NotificationsState {
  items: PulseNotification[];
  unread: number;
  /** Recently decided rows, as the SERVER bounds them (#852). */
  settled: PulseNotification[];
  /** Unread escalations whose ledger state could not be established (#852 rule 5). */
  uncertain: number;
}

const EMPTY: NotificationsState = { items: [], unread: 0, settled: [], uncertain: 0 };

/** The fields a server answer carries; a mutation's answer may omit the settled window. */
export interface NotificationsAnswer {
  notifications: PulseNotification[];
  unread: number;
  settled?: PulseNotification[];
  uncertain?: number;
}

let state: NotificationsState = EMPTY;
const listeners = new Set<() => void>();
/** Bumped when the poll stops: a read or an action's answer begun under an older generation is
 *  not applied. */
let gen = 0;
let timer: ReturnType<typeof setInterval> | null = null;

function emit(): void {
  for (const fn of listeners) fn();
}

/** Apply a server answer. `full` answers (a read, Clear all) replace the settled window too;
 *  the others (mark read, dismiss one) leave it, as the bell always did. */
function apply(r: NotificationsAnswer, full: boolean): void {
  state = full
    ? { items: r.notifications, unread: r.unread, settled: r.settled ?? [], uncertain: r.uncertain ?? 0 }
    : { ...state, items: r.notifications, unread: r.unread };
  emit();
}

/** A bell action (Clear all, dismiss, mark read) and its answer, applied ONLY to the poll it began
 *  under. An answer landing after the last reader left, or after a newer poll began (the relay
 *  shell signed out and mounted another session in the same tab), is dropped: it belongs to a
 *  session that is gone, and writing it would show that session's notifications to the next one.
 *  A failed request rejects, so the caller keeps what is shown. */
export async function mutateNotifications(
  request: () => Promise<NotificationsAnswer>,
  full: boolean,
): Promise<void> {
  const g = gen;
  const r = await request();
  if (g !== gen || listeners.size === 0) return;
  apply(r, full);
}

/** The app-open retraction (#1086 Phase 4): with the bell's read in hand, close on this device
 *  every notification the server says is still OWED a retraction (`close_tags` is absent when its
 *  store could not be read). Never fatal — this is chrome, and a browser without service workers or
 *  notifications simply has nothing to close. */
function reconcileDevice(closeTags: string[] | undefined) {
  if (!closeTags?.length || typeof navigator === "undefined" || !("serviceWorker" in navigator))
    return;
  navigator.serviceWorker
    .getRegistration()
    .then((reg) => (reg ? reconcileDeviceNotifications(reg, closeTags) : 0))
    .catch(() => undefined);
}

function read(poll: boolean): Promise<void> {
  const g = gen;
  return Promise.resolve()
    .then(() => api.notifications())
    .then((r) => {
      if (g !== gen || listeners.size === 0) return;
      apply(r, true);
      if (poll) reconcileDevice(r.close_tags);
    })
    .catch(() => undefined);
}

/** Read now (the bell opening, Clear settled). Resolves when the answer is applied or dropped;
 *  never rejects. */
export function refreshNotifications(): Promise<void> {
  return read(false);
}

/** A poll: the timer, the first subscriber, `ACTION_RESOLVED_EVENT`. */
const tick = () => void read(true);

function start(): void {
  tick();
  timer = setInterval(tick, NOTIFICATIONS_POLL_MS);
  // Resolving an action retires its alert server-side, but the badge would keep counting it until
  // the next poll — up to a minute, on the screen where the operator just decided it.
  window.addEventListener(ACTION_RESOLVED_EVENT, tick);
}

function stop(): void {
  gen += 1;
  if (timer !== null) clearInterval(timer);
  timer = null;
  window.removeEventListener(ACTION_RESOLVED_EVENT, tick);
  state = EMPTY;
}

export function subscribeNotifications(fn: () => void): () => void {
  listeners.add(fn);
  if (listeners.size === 1) start();
  return () => {
    if (!listeners.delete(fn)) return;
    if (listeners.size === 0) stop();
  };
}

export function getNotifications(): NotificationsState {
  return state;
}

export function useNotifications(): NotificationsState {
  return useSyncExternalStore(subscribeNotifications, getNotifications, getNotifications);
}

/** Whether the bell has anything unread — the favicon's half of the unread dot. */
export function useNotificationsUnread(): boolean {
  return useSyncExternalStore(
    subscribeNotifications,
    () => state.unread > 0,
    () => false,
  );
}
