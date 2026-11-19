/** What peers share with this instance (#2055): `GET /api/federation/remote`, held for the
 *  surfaces that list it (the session list's Shared tab, #2198; the map; Settings → Peers).
 *
 *  **This is deliberately not the session list.** A remote entry is never a `SessionEntry`, never
 *  enters `SessionsContext` or the map's retained list (`OverviewSessionsProvider`), and is never
 *  filtered, paged, selected or bulk-acted on with local sessions. It has its own read, its own
 *  rows, its own tab in the session list and its own route. The tab's search runs in the browser
 *  over this list (`searchRemoteShares`).
 *
 *  **Memory only.** The list (titles and names a peer chose) is never written to local or session
 *  storage, and no turn of a remote session ever reaches this module. It is read while something
 *  shows it and re-read on a slow poll; `refreshRemoteShares()` asks again at once (the pane
 *  calls it when its share ended, so the row leaves with the next read). */
import { useEffect, useSyncExternalStore } from "react";
import { api } from "../lib/api";
import { isEpochS, type RemoteShare } from "../lib/federation";

/** The mirror behind the read refreshes every 60 s; half of that is as fresh as a row gets. */
export const REMOTE_LIST_POLL_MS = 30_000;

const EMPTY: readonly RemoteShare[] = Object.freeze([]);
let rows: readonly RemoteShare[] = EMPTY;
let signature = "";
let watchers = 0;
let timer: ReturnType<typeof setTimeout> | null = null;
let inflight: AbortController | null = null;
/** What is known of the list itself: whether it was ever read, and how many reads have ended
 *  (either way). A fresh object whenever it changes, so it is a store snapshot. */
let status: RemoteSharesStatus = Object.freeze({ loaded: false, attempts: 0 });
const listeners = new Set<() => void>();

/** The kinds this build draws, each with its own pane: an API session's conversation
 *  (`RemotePane`) and a terminal's screen (`RemoteScreenPane`, #2060). `undefined` is an owner from
 *  before kinds, whose grants are all API grants. */
const KNOWN_KINDS: ReadonlySet<unknown> = new Set([undefined, "api", "terminal"]);

/** Whether a listed entry is one this build can show.
 *
 *  Besides the shape, the `kind`: only a kind this build has a pane for is listed. An entry of a
 *  kind it does not know (a later build's, or anything else a peer sends) is skipped, never shown
 *  as another kind: drawn through the wrong pane, every read of it would be refused by the owner
 *  and read as "no longer shared" when it is shared. The route picks the pane by this same field
 *  (`RemoteSessionView`). The expiry must be a date (`isEpochS`): finite, after 0, within what a
 *  `Date` holds. */
export function isRow(r: unknown): r is RemoteShare {
  if (!r || typeof r !== "object") return false;
  const x = r as Record<string, unknown>;
  return (
    KNOWN_KINDS.has(x.kind) &&
    typeof x.peer_id === "string" &&
    typeof x.grant_id === "string" &&
    typeof x.title === "string" &&
    typeof x.peer_name === "string" &&
    typeof x.level === "string" &&
    // An expiry this browser cannot make a date of is not a row (#2055 review): the server
    // refuses such a listing since, but a value must never reach a formatter that could throw.
    isEpochS(x.expires_at)
  );
}

function commit(next: readonly RemoteShare[] | null) {
  // Same content keeps the same array: the map must not rebuild its nodes on an idle poll
  // (docs/invariants/map-window-layer.md, the #936 rule). `null`: the read failed, the rows stay.
  const sig = next ? JSON.stringify(next) : signature;
  if (next && sig !== signature) {
    signature = sig;
    rows = next.length ? next : EMPTY;
  }
  status = Object.freeze({ loaded: status.loaded || next !== null, attempts: status.attempts + 1 });
  for (const fn of listeners) fn();
}

async function read() {
  inflight?.abort();
  const ctl = new AbortController();
  inflight = ctl;
  try {
    const answer = await api.federationRemote(ctl.signal);
    if (ctl.signal.aborted) return;
    const list: unknown = (answer as { remote?: unknown } | null)?.remote;
    commit(Array.isArray(list) ? list.filter(isRow) : []);
  } catch {
    // A failed read lists nothing new and removes nothing: the rows open a pane that asks the
    // owner anyway, and the next read corrects the list.
    if (!ctl.signal.aborted) commit(null);
  } finally {
    if (inflight === ctl) inflight = null;
  }
}

function schedule() {
  if (timer) clearTimeout(timer);
  timer = null;
  if (!watchers) return;
  timer = setTimeout(() => {
    const visible = typeof document === "undefined" || document.visibilityState !== "hidden";
    void (visible ? read() : Promise.resolve()).finally(schedule);
  }, REMOTE_LIST_POLL_MS);
}

/** Ask for the list again now. */
export function refreshRemoteShares(): Promise<void> {
  return read().finally(schedule);
}

function subscribe(fn: () => void) {
  listeners.add(fn);
  return () => {
    listeners.delete(fn);
  };
}
const snapshot = () => rows;

/** The sessions peers share with this instance. Empty until the first read, and on an instance
 *  with no peers: an empty array alone never says "nothing is shared" (`useRemoteSharesStatus`
 *  does, and the Shared tab's fallback waits for it, #2198). */
export function useRemoteShares(): readonly RemoteShare[] {
  useEffect(() => {
    watchers += 1;
    if (watchers === 1) void refreshRemoteShares();
    return () => {
      watchers -= 1;
      if (!watchers) {
        if (timer) clearTimeout(timer);
        timer = null;
        inflight?.abort();
      }
    };
  }, []);
  return useSyncExternalStore(subscribe, snapshot, snapshot);
}

export interface RemoteSharesStatus {
  /** A read of the list has succeeded: "not listed" now means the mirror has no such share. */
  loaded: boolean;
  /** Reads that have ended, successful or not. */
  attempts: number;
}
const statusSnapshot = () => status;

/** Whether the list was read yet (#2060). The route of a shared session picks its pane by the
 *  listed kind, so it must tell "not listed" from "not read yet". It does not start a read:
 *  `useRemoteShares` in the same component does. */
export function useRemoteSharesStatus(): RemoteSharesStatus {
  return useSyncExternalStore(subscribe, statusSnapshot, statusSnapshot);
}

/** Test seam: forget everything (the store is module state). */
export function resetRemoteSharesForTest() {
  status = Object.freeze({ loaded: false, attempts: 0 });
  rows = EMPTY;
  signature = "";
  watchers = 0;
  if (timer) clearTimeout(timer);
  timer = null;
  inflight?.abort();
  inflight = null;
  listeners.clear();
}
