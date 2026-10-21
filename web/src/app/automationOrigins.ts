/** Which sessions and missions an automation started (#1201) — the origin badges' one source.
 *
 *  `GET /api/automations/origins` is a small map keyed by the engine-qualified session id or the
 *  mission id. It is fetched ONCE, and again only when something could have changed it: the
 *  window regains focus, the automations page acts (Run now), or a surface that lists sessions or
 *  missions sees a row it has not seen before — an automation's new session appears in the sidebar
 *  on the sidebar's own poll, and that is what asks for the badge. There is no poll of its own.
 *
 *  A module store, like the engine roster, so the sidebar, the mission rail and the map read the
 *  same answer without a provider. A failed read keeps the last good map: a badge is chrome. */
import { useSyncExternalStore } from "react";

import { api } from "../lib/api";
import type { AutomationOrigin } from "../types/automations";

type Origins = Readonly<Record<string, AutomationOrigin>>;

let origins: Origins = {};
let inflight: Promise<void> | null = null;
let lastFetch = 0;
const listeners = new Set<() => void>();

/** A refresh asked for within this long of the last one is folded into it. */
export const MIN_REFRESH_MS = 5_000;

function publish(next: Origins): void {
  origins = next;
  for (const fn of listeners) fn();
}

let trailing: ReturnType<typeof setTimeout> | null = null;

/** After a FAILED read with keys still unanswered, retry on this backoff — independent of the list
 *  changing, capped, and only while the tab is visible (#1252 review). A success resets it. */
export const RETRY_MS = [5_000, 30_000, 120_000] as const;
let retryStep = 0;
let retryTimer: ReturnType<typeof setTimeout> | null = null;
let retryOnVisible: (() => void) | null = null;

function scheduleRetry(): void {
  if (retryTimer || retryOnVisible || !unconfirmed.size) return;
  const delay = RETRY_MS[Math.min(retryStep, RETRY_MS.length - 1)];
  retryStep += 1;
  retryTimer = setTimeout(() => {
    retryTimer = null;
    if (typeof document !== "undefined" && document.hidden) {
      // Hidden: no background traffic. Retry once, when the operator looks again.
      retryOnVisible = () => {
        if (document.hidden) return;
        document.removeEventListener("visibilitychange", retryOnVisible!);
        retryOnVisible = null;
        void refreshOrigins(true);
      };
      document.addEventListener("visibilitychange", retryOnVisible);
      return;
    }
    void refreshOrigins(true);
  }, delay);
}

function clearRetry(): void {
  retryStep = 0;
  if (retryTimer) clearTimeout(retryTimer);
  retryTimer = null;
  if (retryOnVisible) document.removeEventListener("visibilitychange", retryOnVisible);
  retryOnVisible = null;
}

/** Keys a list surface has reported and a response requested AFTER they appeared has answered —
 *  a steady list never refetches for these. */
const seen = new Set<string>();
/** Keys reported but not yet answered by a request that started after they appeared. */
const unconfirmed = new Set<string>();
/** A refresh was asked for while one was in flight: exactly ONE more runs when it completes. */
let again = false;

/** Fetch the map now. A request inside `MIN_REFRESH_MS` of the last fetch is not dropped — it is
 *  deferred to the end of that window, so a row that appears just after a fetch still gets its
 *  badge (`force` skips the wait). A request while one is IN FLIGHT is coalesced into one trailing
 *  refresh after it (#1252 review): the in-flight read was asked before the new row existed, so its
 *  answer cannot be the new row's. */
export function refreshOrigins(force = false): Promise<void> {
  if (inflight) {
    again = true;
    return inflight;
  }
  const wait = MIN_REFRESH_MS - (Date.now() - lastFetch);
  if (!force && wait > 0) {
    if (!trailing)
      trailing = setTimeout(() => {
        trailing = null;
        void refreshOrigins(true);
      }, wait);
    return Promise.resolve();
  }
  lastFetch = Date.now();
  // This read covers every refresh already waiting (a trailing one, a retry): cancel them, so one
  // report never becomes two reads.
  if (trailing) clearTimeout(trailing);
  trailing = null;
  if (retryTimer) clearTimeout(retryTimer);
  retryTimer = null;
  // The keys THIS request can answer for: the ones reported before it started.
  const covers = [...unconfirmed];
  // Started from a resolved promise, so even a synchronous throw (a partial test double of the API
  // client) lands in the catch: a badge is chrome, and must never take a list down with it.
  inflight = Promise.resolve()
    .then(() => api.automationOrigins())
    .then((r) => {
      publish(r && typeof r.origins === "object" && r.origins ? r.origins : {});
      for (const k of covers) {
        unconfirmed.delete(k);
        seen.add(k);
      }
      clearRetry();
    })
    .catch(() => scheduleRetry())
    .finally(() => {
      inflight = null;
      if (again) {
        again = false;
        void refreshOrigins(true);
      }
    });
  return inflight;
}

let focusBound = false;
function bindFocus(): void {
  if (focusBound || typeof window === "undefined") return;
  focusBound = true;
  window.addEventListener("focus", () => void refreshOrigins());
}

function subscribe(fn: () => void): () => void {
  listeners.add(fn);
  bindFocus();
  if (lastFetch === 0) void refreshOrigins();
  return () => listeners.delete(fn);
}

export function useAutomationOrigins(): Origins {
  return useSyncExternalStore(subscribe, () => origins, () => origins);
}

/** The key the server records a MISSION's origin under (`automations_store.link`): missions and
 *  sessions share one map, so a mission id is qualified. One helper, used by every consumer. */
export function missionOriginKey(missionId: string): string {
  return `mission:${missionId}`;
}

/** The origin of one session or mission key, or undefined. */
export function originOf(map: Origins, key: string): AutomationOrigin | undefined {
  return Object.prototype.hasOwnProperty.call(map, key) ? map[key] : undefined;
}

/** A list surface reports the keys it renders; any key not seen before asks for a refresh. */
export function noteKeys(keys: readonly string[]): void {
  let fresh = false;
  for (const k of keys) {
    // Not yet answered by a read that started after it — including one whose read FAILED — asks
    // again on the next report, not only on focus. Throttled and coalesced by `refreshOrigins`.
    if (!seen.has(k)) {
      unconfirmed.add(k);
      fresh = true;
    }
  }
  if (fresh) void refreshOrigins();
}

/** Tests only. */
export function getOriginsForTest(): Origins {
  return origins;
}

/** Tests only. */
export function resetOriginsForTest(): void {
  origins = {};
  inflight = null;
  if (trailing) clearTimeout(trailing);
  trailing = null;
  lastFetch = 0;
  seen.clear();
  unconfirmed.clear();
  again = false;
  clearRetry();
  listeners.clear();
}
