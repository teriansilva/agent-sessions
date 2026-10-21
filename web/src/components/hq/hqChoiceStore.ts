/** The HQ contact choice in flight (#1525), app-wide, so it outlives any one gallery (Hermes on
 *  #1535):
 *
 *  - **One write queue for the whole app.** Picks are written one at a time, in the order they were
 *    made. A pick superseded before its turn is never written, so the latest pick is the one
 *    stored, even across leaving and reopening Settings. The queue holds only the writes: a stalled
 *    config read never delays the next pick's write.
 *  - **`persisted`** is a choice the server ACCEPTED that no config read has caught up with yet.
 *    Every consumer (`useHqContact`: the header, the dashboard card, a turn's `contact`) reads it
 *    ahead of the config, so all of them follow what is actually stored.
 *  - **`pending`** is the latest pick not written yet: only the gallery shows it.
 *
 *  **Ordered by the server, never by arrival.** Every write of the pref bumps `hq_contact_rev` on
 *  the server, which returns it with the write and with every `/api/config`. A config whose
 *  revision is at least the one a write returned is at least as new as that write: it retires
 *  `persisted` (`noteHqConfig`). A write whose revision a config the app already applied had
 *  reached never becomes `persisted` at all. So another device's newer choice always wins, and a
 *  read that predates a write never overrides it, however the responses interleave.
 */
import { useSyncExternalStore } from "react";

import { api } from "../../lib/api";
import type { HqChoice } from "./contacts";

export interface HqChoiceState {
  pending: HqChoice | null;
  persisted: HqChoice | null;
  problem: "save" | "refresh" | null;
}

const INITIAL: HqChoiceState = { pending: null, persisted: null, problem: null };
let state: HqChoiceState = INITIAL;
const listeners = new Set<() => void>();
let latest = 0;
let queue: Promise<void> = Promise.resolve();
/** The server revision `persisted` was written at, and the newest one a config showed. */
let persistedRev = 0;
let configRev = -1;

function set(patch: Partial<HqChoiceState>) {
  state = { ...state, ...patch };
  listeners.forEach((l) => l());
}

function subscribe(l: () => void) {
  listeners.add(l);
  return () => listeners.delete(l);
}

export function useHqChoiceState(): HqChoiceState {
  return useSyncExternalStore(subscribe, () => state);
}

/** Read the config again. What it shows is reconciled by `noteHqConfig`; a failed read says so. */
async function confirm(gen: number, reload: () => Promise<boolean>) {
  const applied = await reload();
  if (gen !== latest) return;
  set({ problem: applied ? null : "refresh" });
}

/** Pick `key`: shown at once, written in order, then confirmed by a config read. */
export function pickHqChoice(key: HqChoice, reload: () => Promise<boolean>): void {
  const gen = ++latest;
  set({ pending: key, problem: null });
  queue = queue.then(async () => {
    if (gen !== latest) return; // a newer pick is queued: only the latest is written
    let rev: unknown;
    try {
      rev = (await api.setPrefs({ hq_contact: key })).hq_contact_rev;
    } catch {
      if (gen === latest) set({ pending: null, problem: "save" });
      return;
    }
    // The server's order of this write; one it does not report is ordered after what was seen.
    const at = typeof rev === "number" ? rev : configRev + 1;
    const done = gen === latest ? { pending: null } : {};
    if (at <= configRev) {
      set(done); // the app already shows a config at least this new: nothing to hold
    } else {
      persistedRev = at;
      set({ ...done, persisted: key });
    }
    // Confirmed OUTSIDE the queue: a stalled read must not hold the next pick's write.
    void confirm(gen, reload);
  });
}

/** Every config the app applies: one at least as new as `persisted`'s write retires it. */
export function noteHqConfig(cfg: { hq_contact_rev?: unknown } | null): void {
  const rev = cfg?.hq_contact_rev;
  if (typeof rev !== "number") return;
  configRev = rev;
  if (state.persisted !== null && rev >= persistedRev) set({ persisted: null, problem: null });
}

/** Retry the config read after a failed one. */
export function retryHqChoiceRefresh(reload: () => Promise<boolean>): void {
  void confirm(latest, reload);
}

/** Tests only: the store is module state. */
export function resetHqChoiceStore(): void {
  state = INITIAL;
  latest = 0;
  queue = Promise.resolve();
  persistedRev = 0;
  configRev = -1;
  listeners.forEach((l) => l());
}
